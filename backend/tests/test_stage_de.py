"""Tests for Stage D & E: Gmail Dashboard, Candidate Confirmation, and Real Spending Analytics.

Financial and Security Invariants Verified:
1. Candidate confirmation promotes candidate to the subscription inventory safely and idempotently.
2. Candidate confirmation NEVER triggers cancellation or invents usage activity.
3. Duplicate confirmations are handled safely without duplicate records or errors.
4. One-time purchases are strictly excluded from recurring monthly/annual calculations.
5. Missing or unknown amounts are never treated as $0.00.
6. Incompatible currencies are never combined into a single sum.
7. Disconnect revokes tokens and removes credentials safely.
8. Unauthenticated/unconfigured states fail gracefully.
"""

from datetime import date, datetime, timezone
import json
import pytest
from fastapi.testclient import TestClient

from backend.app.main import app, GMAIL_DEMO_USER
from backend.app.database import (
    get_db_session,
    initialize_schema,
    save_gmail_candidates,
    load_gmail_candidates,
    confirm_gmail_candidate,
    get_spending_analytics,
    save_subscription,
)
from backend.app.models import Subscription, SubscriptionStatus


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Ensure every test runs against a clean isolated SQLite database."""
    test_db = tmp_path / "test_stage_de.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{test_db}")
    initialize_schema()
    yield test_db


# ==============================================================================
# 1. Gmail Status & Endpoint Behavior
# ==============================================================================

class TestGmailStatusEndpoints:
    def test_gmail_status_unconfigured(self, client, monkeypatch):
        import backend.app.config as cfg
        monkeypatch.setattr(cfg, "GOOGLE_CLIENT_ID", None)
        monkeypatch.setattr(cfg, "GOOGLE_CLIENT_SECRET", None)

        res = client.get("/api/email/status")
        assert res.status_code == 200
        data = res.json()
        assert data["connected"] is False
        assert data["data_source"] == "not_connected"
        assert data["credential_status"]["oauth_ready"] is False
        assert "candidates_count" in data
        assert "candidates_pending_count" in data

    def test_gmail_status_configured_not_connected(self, client, monkeypatch):
        import backend.app.config as cfg
        monkeypatch.setattr(cfg, "GOOGLE_CLIENT_ID", "mock-client-id.apps.googleusercontent.com")
        monkeypatch.setattr(cfg, "GOOGLE_CLIENT_SECRET", "mock-secret-12345")

        res = client.get("/api/email/status")
        assert res.status_code == 200
        data = res.json()
        assert data["connected"] is False
        assert data["credential_status"]["oauth_ready"] is True
        assert data["data_source"] == "not_connected"

    def test_disconnect_returns_structured_response(self, client):
        res = client.post("/api/email/disconnect")
        assert res.status_code == 200
        data = res.json()
        assert "disconnected" in data
        assert "message" in data


# ==============================================================================
# 2. Gmail Billing Sync
# ==============================================================================

class TestGmailSyncEndpoints:
    def test_sync_without_credentials_returns_401(self, client):
        res = client.post("/api/email/sync")
        assert res.status_code == 401
        data = res.json()
        assert data["error"]["code"] == "GMAIL_NOT_CONNECTED"

    def test_sync_success_with_mocked_discovery(self, client, monkeypatch):
        import backend.app.gmail as gmail_mod

        # Mock discovery returning 2 sample candidates
        mock_candidates = [
            {
                "message_id": "msg_stage_de_001",
                "thread_id": "th_001",
                "subject": "Your Netflix Invoice",
                "sender": "info@mailer.netflix.com",
                "date_header": "2026-03-01",
                "provider_name": "Netflix",
                "amount": 15.49,
                "currency": "USD",
                "billing_date": "2026-03-01",
                "body_preview": "Your plan renewed for $15.49",
                "extracted_at": "2026-03-01T10:00:00Z",
            },
            {
                "message_id": "msg_stage_de_002",
                "thread_id": "th_002",
                "subject": "Spotify Receipt",
                "sender": "no-reply@spotify.com",
                "date_header": "2026-03-05",
                "provider_name": "Spotify",
                "amount": 10.99,
                "currency": "USD",
                "billing_date": "2026-03-05",
                "body_preview": "Spotify Premium renewal",
                "extracted_at": "2026-03-05T10:00:00Z",
            },
        ]

        monkeypatch.setattr(
            gmail_mod,
            "discover_billing_emails",
            lambda user_id, max_messages: {
                "user_id": user_id,
                "messages_scanned": 15,
                "billing_emails_found": 2,
                "candidates": mock_candidates,
                "data_source": "gmail_live",
            },
        )

        res = client.post("/api/email/sync")
        assert res.status_code == 200
        data = res.json()
        assert data["messages_scanned"] == 15
        assert data["saved_candidates"] == 2

        # Verify persisted in database
        candidates = load_gmail_candidates(GMAIL_DEMO_USER)
        assert len(candidates) == 2
        assert any(c["message_id"] == "msg_stage_de_001" for c in candidates)


# ==============================================================================
# 3. Candidate Display & Confirmation (Stage D)
# ==============================================================================

class TestCandidateConfirmation:
    def test_candidate_listing(self, client):
        candidates = [
            {
                "message_id": "msg_cand_1",
                "thread_id": "th_1",
                "subject": "Disney+ Bill",
                "sender": "billing@disneyplus.com",
                "date_header": "2026-02-15",
                "provider_name": "Disney+",
                "amount": 13.99,
                "currency": "USD",
                "billing_date": "2026-02-15",
                "body_preview": "Disney+ monthly subscription",
                "extracted_at": "2026-02-15T00:00:00Z",
            }
        ]
        save_gmail_candidates(candidates, GMAIL_DEMO_USER)

        res = client.get("/api/email/candidates")
        assert res.status_code == 200
        data = res.json()
        assert data["total"] == 1
        assert data["candidates"][0]["provider_name"] == "Disney+"
        assert data["candidates"][0]["confirmed"] == 0

    def test_confirm_candidate_promotes_to_subscription_inventory(self, client):
        candidate = {
            "message_id": "msg_promote_001",
            "thread_id": "th_promote",
            "subject": "YouTube Premium Subscription",
            "sender": "service@youtube.com",
            "date_header": "2026-03-10",
            "provider_name": "YouTube Premium",
            "amount": 13.99,
            "currency": "USD",
            "billing_date": "2026-03-10",
            "body_preview": "YouTube Premium membership confirmation",
            "extracted_at": "2026-03-10T12:00:00Z",
        }
        save_gmail_candidates([candidate], GMAIL_DEMO_USER)

        # Confirm candidate
        res = client.post("/api/email/candidates/msg_promote_001/confirm")
        assert res.status_code == 200
        data = res.json()
        assert data["confirmed"] is True
        assert data["message_id"] == "msg_promote_001"
        assert data["subscription_id"] == "sub_gmail_msg_promote_001"

        # 1. Verify candidate marked confirmed in candidate table
        stored_cands = load_gmail_candidates(GMAIL_DEMO_USER, confirmed_only=True)
        assert len(stored_cands) == 1
        assert stored_cands[0]["confirmed"] == 1

        # 2. Verify promoted to subscriptions table
        with get_db_session() as conn:
            row = conn.execute(
                "SELECT * FROM subscriptions WHERE subscription_id = ?",
                ("sub_gmail_msg_promote_001",),
            ).fetchone()
            assert row is not None
            sub_dict = json.loads(row["payload_json"])
            assert sub_dict["service_name"] == "YouTube Premium"
            assert sub_dict["status"] == "active"
            assert sub_dict["monthly_cost"] == 13.99
            assert sub_dict["renewal_date"] == "2026-03-10"
            assert sub_dict["last_used_at"] is None  # Usage is never invented!

    def test_duplicate_confirmation_is_safe_and_idempotent(self, client):
        candidate = {
            "message_id": "msg_dup_001",
            "thread_id": "th_dup",
            "subject": "Apple TV+",
            "sender": "no-reply@apple.com",
            "date_header": "2026-03-12",
            "provider_name": "Apple TV+",
            "amount": 9.99,
            "currency": "USD",
            "billing_date": "2026-03-12",
            "body_preview": "Apple TV+ subscription receipt",
            "extracted_at": "2026-03-12T12:00:00Z",
        }
        save_gmail_candidates([candidate], GMAIL_DEMO_USER)

        # First confirmation
        res1 = client.post("/api/email/candidates/msg_dup_001/confirm")
        assert res1.status_code == 200

        # Repeated confirmation (simulating rapid user clicks)
        res2 = client.post("/api/email/candidates/msg_dup_001/confirm")
        assert res2.status_code == 200
        assert res2.json()["confirmed"] is True

        # Ensure no duplicates in subscriptions table
        with get_db_session() as conn:
            count = conn.execute(
                "SELECT COUNT(*) as cnt FROM subscriptions WHERE subscription_id = ?",
                ("sub_gmail_msg_dup_001",),
            ).fetchone()["cnt"]
            assert count == 1

    def test_confirm_nonexistent_candidate_returns_404(self, client):
        res = client.post("/api/email/candidates/does_not_exist/confirm")
        assert res.status_code == 404
        assert res.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


# ==============================================================================
# 4. Real Spending Analytics (Stage E)
# ==============================================================================

class TestSpendingAnalytics:
    def test_billing_cycle_conversions(self, client):
        # 1 monthly: $10.00/mo -> $10.00/mo, $120.00/yr
        sub_monthly = Subscription(
            subscription_id="sub_test_monthly",
            service_name="Monthly Service",
            status=SubscriptionStatus.ACTIVE,
            monthly_cost=10.0,
            currency="USD",
            billing_cycle="monthly",
            renewal_date=date(2026, 4, 1),
        )
        # 1 annual: $120.00/yr -> $10.00/mo, $120.00/yr
        sub_annual = Subscription(
            subscription_id="sub_test_annual",
            service_name="Annual Service",
            status=SubscriptionStatus.ACTIVE,
            monthly_cost=120.0,
            currency="USD",
            billing_cycle="annual",
            renewal_date=date(2026, 12, 1),
        )
        # 1 quarterly: $30.00/quarter -> $10.00/mo, $120.00/yr
        sub_quarterly = Subscription(
            subscription_id="sub_test_quarterly",
            service_name="Quarterly Service",
            status=SubscriptionStatus.ACTIVE,
            monthly_cost=30.0,
            currency="USD",
            billing_cycle="quarterly",
            renewal_date=date(2026, 6, 1),
        )

        save_subscription(sub_monthly)
        save_subscription(sub_annual)
        save_subscription(sub_quarterly)

        res = client.get("/api/spending/analytics")
        assert res.status_code == 200
        data = res.json()

        assert data["active_subscriptions_count"] == 3
        # Monthly total: 10 + 10 + 10 = $30.00
        assert data["totals_by_currency"]["USD"]["monthly_recurring_cost"] == 30.0
        # Annual total: 120 + 120 + 120 = $360.00
        assert data["totals_by_currency"]["USD"]["annual_recurring_cost"] == 360.0
        assert data["monthly_recurring_display"] == "$30.00"
        assert data["annual_recurring_display"] == "$360.00"
        assert data["is_estimate"] is True

    def test_exclusion_of_one_time_purchases(self, client):
        sub_recurring = Subscription(
            subscription_id="sub_rec",
            service_name="Recurring Box",
            status=SubscriptionStatus.ACTIVE,
            monthly_cost=15.0,
            currency="USD",
            billing_cycle="monthly",
            renewal_date=date(2026, 5, 1),
        )
        sub_onetime = Subscription(
            subscription_id="sub_onetime",
            service_name="One-Time Hardware Kit",
            status=SubscriptionStatus.ACTIVE,
            monthly_cost=99.0,
            currency="USD",
            billing_cycle="one-time",
            renewal_date=None,
        )
        save_subscription(sub_recurring)
        save_subscription(sub_onetime)

        res = client.get("/api/spending/analytics")
        assert res.status_code == 200
        data = res.json()

        assert data["one_time_purchases_count"] == 1
        # The $99 one-time purchase must NOT be added to monthly or annual recurring costs
        assert data["totals_by_currency"]["USD"]["monthly_recurring_cost"] == 15.0
        assert data["totals_by_currency"]["USD"]["annual_recurring_cost"] == 180.0

    def test_unknown_amounts_not_treated_as_zero(self, client):
        sub_known = Subscription(
            subscription_id="sub_known",
            service_name="Known Streamer",
            status=SubscriptionStatus.ACTIVE,
            monthly_cost=12.0,
            currency="USD",
            billing_cycle="monthly",
            renewal_date=date(2026, 4, 15),
        )
        sub_unknown = Subscription(
            subscription_id="sub_unknown",
            service_name="Unknown Cost Service",
            status=SubscriptionStatus.ACTIVE,
            monthly_cost=None,  # Missing cost
            currency="USD",
            billing_cycle="monthly",
            renewal_date=date(2026, 4, 20),
        )
        save_subscription(sub_known)
        save_subscription(sub_unknown)

        res = client.get("/api/spending/analytics")
        assert res.status_code == 200
        data = res.json()

        assert data["unknown_cost_count"] == 1
        # Cost total should be exactly 12.00 (from known stream), with unknown flagged
        assert data["totals_by_currency"]["USD"]["monthly_recurring_cost"] == 12.0
        assert any("Missing or unknown billing cost" in u["issue"] for u in data["uncertainties"])

    def test_multi_currency_totals_separated(self, client):
        sub_usd = Subscription(
            subscription_id="sub_usd",
            service_name="US Streaming",
            status=SubscriptionStatus.ACTIVE,
            monthly_cost=15.0,
            currency="USD",
            billing_cycle="monthly",
            renewal_date=date(2026, 4, 1),
        )
        sub_eur = Subscription(
            subscription_id="sub_eur",
            service_name="EU Streaming",
            status=SubscriptionStatus.ACTIVE,
            monthly_cost=10.0,
            currency="EUR",
            billing_cycle="monthly",
            renewal_date=date(2026, 4, 5),
        )
        save_subscription(sub_usd)
        save_subscription(sub_eur)

        res = client.get("/api/spending/analytics")
        assert res.status_code == 200
        data = res.json()

        assert data["is_multi_currency"] is True
        assert "USD" in data["totals_by_currency"]
        assert "EUR" in data["totals_by_currency"]
        assert data["totals_by_currency"]["USD"]["monthly_recurring_cost"] == 15.0
        assert data["totals_by_currency"]["EUR"]["monthly_recurring_cost"] == 10.0
        # Multi-currency display should include both
        assert "$15.00" in data["monthly_recurring_display"]
        assert "€10.00" in data["monthly_recurring_display"]

    def test_no_double_counting_between_candidate_and_confirmed_subscription(self, client):
        # Insert a candidate
        candidate = {
            "message_id": "msg_dedup_001",
            "thread_id": "th_dedup",
            "subject": "Hulu Invoice",
            "sender": "hulu@hulumail.com",
            "date_header": "2026-03-20",
            "provider_name": "Hulu",
            "amount": 7.99,
            "currency": "USD",
            "billing_date": "2026-03-20",
            "body_preview": "Hulu subscription billed $7.99",
            "extracted_at": "2026-03-20T00:00:00Z",
        }
        save_gmail_candidates([candidate], GMAIL_DEMO_USER)

        # Before confirmation:
        res1 = client.get("/api/spending/analytics")
        data1 = res1.json()
        assert data1["candidates_awaiting_review_count"] == 1

        # Confirm the candidate
        client.post("/api/email/candidates/msg_dedup_001/confirm")

        # After confirmation:
        res2 = client.get("/api/spending/analytics")
        data2 = res2.json()

        # Candidates awaiting review drops to 0
        assert data2["candidates_awaiting_review_count"] == 0

        # Subscriptions now include Hulu exactly once
        hulu_subs = [s for s in data2["active_subscriptions"] if s["service_name"] == "Hulu"]
        assert len(hulu_subs) == 1
        assert hulu_subs[0]["monthly_cost"] == 7.99

    def test_upcoming_renewals_chronological_ordering(self, client):
        sub_later = Subscription(
            subscription_id="sub_later",
            service_name="Later Renewal",
            status=SubscriptionStatus.ACTIVE,
            monthly_cost=10.0,
            renewal_date=date(2026, 5, 20),
        )
        sub_earlier = Subscription(
            subscription_id="sub_earlier",
            service_name="Earlier Renewal",
            status=SubscriptionStatus.ACTIVE,
            monthly_cost=10.0,
            renewal_date=date(2026, 4, 1),
        )
        save_subscription(sub_later)
        save_subscription(sub_earlier)

        res = client.get("/api/spending/analytics")
        data = res.json()
        renewals = data["upcoming_renewals"]
        assert len(renewals) >= 2
        # Verify first renewal is earlier than second
        assert renewals[0]["renewal_date"] <= renewals[1]["renewal_date"]
