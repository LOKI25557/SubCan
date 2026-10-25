"""Comprehensive Test Suite for Real-Data-Only SubCan Pipeline and INR Localization.

Covers all 17 requirements:
1. No automatic demo seeding on startup.
2. Empty inventory when no real or manually entered subscriptions exist.
3. Only genuine, user-confirmed subscriptions appear in monitored inventory.
4. Gmail candidates remain pending until confirmed.
5. Manual entries persist and remain distinguishable from Gmail data.
6. Gmail sync failure does not generate fallback data.
7. Repeated sync does not duplicate candidates or subscriptions.
8. Confirmed and dismissed states survive repeated sync.
9. False-positive email classification.
10. Real billing receipts and renewal notices.
11. Missing prices and currencies remain unknown.
12. INR formatting uses '₹' for INR transactions.
13. USD transactions remain USD unless documented conversion is performed.
14. Currency metadata survives database persistence and export.
15. Database cleanup preserves legitimate user data.
16. Dashboard and exports contain no seeded demo records.
17. Existing cancellation safety and approval tests continue passing.
"""

from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient

from backend.app.database import (
    cleanup_demo_and_mock_data,
    delete_user_data,
    get_subscription_candidate as get_candidate_by_id,
    get_subscription_from_db,
    initialize_schema,
    list_subscription_candidates as list_candidates,
    list_subscriptions_from_db,
    save_subscription_candidate as save_candidate,
    save_subscription,
)
from backend.app.email.parser import (
    evaluate_billing_email,
    extract_monetary_amount,
)
from backend.app.email.service import (
    confirm_candidate_into_inventory,
    dismiss_candidate,
    sync_user_mailbox,
)
from backend.app.main import app
from backend.app.models import (
    CandidateReviewStatus,
    DiscoverySource,
    EmailAccountStatus,
    Subscription,
    SubscriptionCandidate,
    SubscriptionStatus,
)

client = TestClient(app)

TEST_USER = "test_user_real_data_suite"


@pytest.fixture(autouse=True)
def setup_teardown_test_user():
    initialize_schema()
    delete_user_data(TEST_USER)
    yield
    delete_user_data(TEST_USER)


# ==============================================================================
# 1. No automatic demo seeding on startup
# ==============================================================================
def test_no_automatic_demo_seeding_on_startup():
    """Verify clean user query does not seed CineVault, AudioCloud, FitStream, etc."""
    resp = client.get("/subscriptions", headers={"X-User-ID": TEST_USER})
    assert resp.status_code == 200
    subs = resp.json()
    assert subs == []

    # Check direct database store
    db_subs = list_subscriptions_from_db(user_id=TEST_USER)
    assert len(db_subs) == 0
    demo_names = ["CineVault", "AudioCloud", "FitStream", "MovieMax", "GameStream"]
    for s in db_subs:
        assert not any(demo in s.service_name for demo in demo_names)


# ==============================================================================
# 2. Empty inventory when no real or manually entered subscriptions exist
# ==============================================================================
def test_empty_inventory_when_no_real_or_manual_subscriptions_exist():
    """Verify truthful empty inventory state without fallback fixtures."""
    resp = client.get("/subscriptions", headers={"X-User-ID": TEST_USER})
    assert resp.status_code == 200
    assert resp.json() == []

    diag_resp = client.get("/api/email/diagnostics", headers={"X-User-ID": TEST_USER})
    assert diag_resp.status_code == 200
    inv = diag_resp.json()["inventory_summary"]
    assert inv["total_monitored_count"] == 0
    assert inv["email_discovered_count"] == 0
    assert inv["manual_count"] == 0


# ==============================================================================
# 3 & 4. Only genuine, user-confirmed subscriptions appear; candidates remain pending
# ==============================================================================
def test_candidates_remain_pending_until_explicitly_confirmed():
    """Verify Gmail candidate is separate from monitored inventory until confirmed."""
    cand = SubscriptionCandidate(
        candidate_id="cand_test_spotify_001",
        user_id=TEST_USER,
        merchant_name="Spotify India",
        product_name="Spotify Premium Individual",
        estimated_cost=119.0,
        currency="INR",
        billing_interval="monthly",
        review_status=CandidateReviewStatus.PENDING,
        confidence=0.92,
        source_message_id="msg_real_spotify_123",
        email_snippet="Your Spotify Premium renewal for Rs. 119.00 was successful.",
    )
    save_candidate(cand)

    # 1. Monitored inventory must remain empty
    resp = client.get("/subscriptions", headers={"X-User-ID": TEST_USER})
    assert resp.status_code == 200
    assert resp.json() == []

    # 2. Candidate endpoint lists it as pending
    c_resp = client.get("/api/email/candidates", headers={"X-User-ID": TEST_USER})
    assert c_resp.status_code == 200
    candidates = c_resp.json()
    assert len(candidates) == 1
    assert candidates[0]["candidate_id"] == "cand_test_spotify_001"
    assert candidates[0]["review_status"] == "pending"

    # 3. Explicitly confirm candidate
    conf_resp = client.post(
        f"/api/email/candidates/{cand.candidate_id}/confirm",
        headers={"X-User-ID": TEST_USER},
    )
    assert conf_resp.status_code == 200
    sub_data = conf_resp.json()
    assert sub_data["service_name"] == "Spotify India"
    assert sub_data["currency"] == "INR"
    assert sub_data["discovery_source"] == "email"

    # 4. Now appears in monitored inventory
    inv_resp = client.get("/subscriptions", headers={"X-User-ID": TEST_USER})
    assert inv_resp.status_code == 200
    items = inv_resp.json()
    assert len(items) == 1
    assert items[0]["service_name"] == "Spotify India"
    assert items[0]["currency"] == "INR"


# ==============================================================================
# 5. Manual entries persist and remain distinguishable from Gmail data
# ==============================================================================
def test_manual_entries_persist_with_currency_and_provenance():
    """Verify manual entry has discovery_source='manual' and stores selected currency."""
    payload = {
        "service_name": "Hotstar Super",
        "monthly_cost": 299.0,
        "currency": "INR",
        "billing_cycle": "monthly",
        "renewal_date": "2026-11-20",
        "status": "active",
    }
    resp = client.post("/subscriptions", json=payload, headers={"X-User-ID": TEST_USER})
    assert resp.status_code == 201
    data = resp.json()
    assert data["discovery_source"] == "manual"
    assert data["currency"] == "INR"
    assert data["monthly_cost"] == 299.0
    assert data["original_currency"] == "INR"

    # Query inventory
    inv = client.get("/subscriptions", headers={"X-User-ID": TEST_USER}).json()
    assert len(inv) == 1
    assert inv[0]["discovery_source"] == "manual"


# ==============================================================================
# 6. Gmail sync failure does not generate fallback data
# ==============================================================================
def test_gmail_sync_failure_does_not_generate_fallback_data(monkeypatch):
    """Verify that when Gmail API retrieval fails, no fallback mock data is injected."""
    from backend.app.email import service
    from backend.app.database import save_email_account
    monkeypatch.setattr(service, "is_live_oauth_configured", lambda: True)
    monkeypatch.setattr(service, "get_valid_access_token", lambda *args, **kwargs: "live_real_token_12345")

    def mock_fetch_fail(*args, **kwargs):
        raise RuntimeError("Simulated Gmail API Connection Refused")

    monkeypatch.setattr(service, "_fetch_live_gmail_candidates", mock_fetch_fail)

    user = "live_user_sync_fail_test"
    delete_user_data(user)
    try:
        acc = EmailAccountStatus(
            user_id=user,
            email_address="lokeshgowdads88@gmail.com",
            is_connected=True,
            provider="gmail",
        )
        save_email_account(acc, refresh_token="live_refresh_12345", access_token="live_real_token_12345")

        with pytest.raises(RuntimeError):
            sync_user_mailbox(user_id=user, max_messages=10)

        # Monitored inventory must have 0 records
        subs = list_subscriptions_from_db(user_id=user)
        assert len(subs) == 0

        # Candidates must have 0 records
        cands = list_candidates(user_id=user)
        assert len(cands) == 0
    finally:
        delete_user_data(user)


# ==============================================================================
# 7 & 8. Repeated sync does not duplicate records; review states survive
# ==============================================================================
def test_repeated_sync_deduplication_and_review_state_preservation():
    """Verify stable candidate IDs prevent duplicate candidates and preserve decisions."""
    res1 = evaluate_billing_email(
        message_id="msg_repeat_sync_001",
        subject="Your Netflix membership renewal",
        sender="billing@netflix.com",
        raw_content="Thanks for being a Netflix member. We have billed Rs. 649.00 to your card for the upcoming month.",
        user_id=TEST_USER,
    )
    assert res1.status == "candidate"
    cand1 = res1.candidate
    assert cand1 is not None
    save_candidate(cand1)

    # Dismiss the candidate
    dismiss_candidate(cand1.candidate_id, user_id=TEST_USER)
    dismissed = get_candidate_by_id(cand1.candidate_id, user_id=TEST_USER)
    assert dismissed.review_status == CandidateReviewStatus.DISMISSED

    # Run evaluate and save again with same message_id
    res2 = evaluate_billing_email(
        message_id="msg_repeat_sync_001",
        subject="Your Netflix membership renewal",
        sender="billing@netflix.com",
        raw_content="Thanks for being a Netflix member. We have billed Rs. 649.00 to your card for the upcoming month.",
        user_id=TEST_USER,
    )
    assert res2.status == "candidate"
    cand2 = res2.candidate
    assert cand2.candidate_id == cand1.candidate_id
    save_candidate(cand2)

    # Must NOT have created a duplicate candidate
    all_cands = list_candidates(user_id=TEST_USER)
    assert len(all_cands) == 1
    # Must preserve the DISMISSED review status
    assert all_cands[0].review_status == CandidateReviewStatus.DISMISSED


# ==============================================================================
# 9. False-positive email classification
# ==============================================================================
def test_false_positive_email_classification():
    """Verify newsletters, shipping notices, security alerts, and brand mentions are skipped."""
    false_positives = [
        {
            "message_id": "fp_01",
            "sender": "notifications@github.com",
            "subject": "[GitHub] Security alert for repository Loki25557/SubCan",
            "plain_text": "Dependabot found 1 high severity vulnerability in requirements.txt. Please update dependencies.",
        },
        {
            "message_id": "fp_02",
            "sender": "shipment-tracking@amazon.in",
            "subject": "Your Amazon.in package has shipped!",
            "plain_text": "Your package with order #402-1234567 is out for delivery today. Total: ₹899.00.",
        },
        {
            "message_id": "fp_03",
            "sender": "newsletter@morningbrew.com",
            "subject": "Morning Brew: What's next for AI and markets?",
            "plain_text": "Here are today's top stories in tech and finance. No cost, 100% free daily email.",
        },
        {
            "message_id": "fp_04",
            "sender": "promo@dominos.co.in",
            "subject": "Get 50% OFF your next pizza tonight!",
            "plain_text": "Use coupon code PIZZA50 at checkout. Offer valid for 2 hours only.",
        },
    ]

    for fp in false_positives:
        parsed = evaluate_billing_email(
            message_id=fp["message_id"],
            subject=fp["subject"],
            sender=fp["sender"],
            raw_content=fp["plain_text"],
            user_id=TEST_USER,
        )
        assert parsed.status == "skipped", f"Expected {fp['subject']} to be skipped as irrelevant, but got {parsed.status}."


# ==============================================================================
# 10. Real billing receipts and renewal notices
# ==============================================================================
def test_real_billing_receipts_extraction():
    """Verify recurring receipts extract merchant, amount, currency, and renewal date."""
    res = evaluate_billing_email(
        message_id="real_rec_01",
        subject="Your Google Play Order Receipt from 18 Oct 2026",
        sender="googleplay-noreply@google.com",
        raw_content=(
            "Google Play\nThank you.\nOrder number: GPA.3341-2894-1120-49210\n"
            "Order date: 18 Oct 2026\n"
            "Item: Google Workspace Individual (Monthly Subscription)\n"
            "Price: ₹1,499.00/month\n"
            "Auto-renewing on 18 Nov 2026."
        ),
        user_id=TEST_USER,
    )
    assert res.status == "candidate"
    cand = res.candidate
    assert cand is not None
    assert "Google Workspace" in cand.merchant_name or "Google Play" in cand.merchant_name
    assert cand.estimated_cost == 1499.00
    assert cand.currency == "INR"
    assert cand.billing_interval == "monthly"
    assert str(cand.detected_renewal_date) == "2026-11-18"


# ==============================================================================
# 11. Missing prices and currencies remain unknown
# ==============================================================================
def test_missing_prices_and_currencies_remain_unknown():
    """Verify unknown amounts/currencies are not fabricated."""
    res = evaluate_billing_email(
        message_id="missing_price_01",
        subject="Your Canva Pro subscription was renewed",
        sender="support@canva.com",
        raw_content="Hi Loki, your Canva Pro team plan has successfully renewed. Thank you for creating with Canva!",
        user_id=TEST_USER,
    )
    assert res.status == "candidate"
    cand = res.candidate
    assert cand is not None
    assert cand.estimated_cost is None
    assert cand.currency is None


# ==============================================================================
# 12. INR formatting uses '₹' for INR transactions
# ==============================================================================
def test_inr_currency_parsing_and_symbols():
    """Verify all Indian Rupee formats parse correctly with currency='INR'."""
    test_cases = [
        ("₹1,499.00", 1499.0, "INR"),
        ("Rs. 649", 649.0, "INR"),
        ("Rs 199.00", 199.0, "INR"),
        ("INR 2,999.00", 2999.0, "INR"),
        ("₹499/-", 499.0, "INR"),
    ]
    for text, exp_amount, exp_curr in test_cases:
        amt, curr = extract_monetary_amount(text)
        assert amt == exp_amount, f"Failed amount for {text}"
        assert curr == exp_curr, f"Failed currency for {text}"


# ==============================================================================
# 13. USD transactions remain USD unless documented conversion is performed
# ==============================================================================
def test_usd_transactions_remain_usd():
    """Verify USD transactions retain USD currency and are not falsely relabeled INR."""
    usd_cases = [
        ("$19.99", 19.99, "USD"),
        ("$20.00 / month", 20.00, "USD"),
        ("USD 14.99", 14.99, "USD"),
    ]
    for text, exp_amount, exp_curr in usd_cases:
        amt, curr = extract_monetary_amount(text)
        assert amt == exp_amount
        assert curr == exp_curr

    # In database persistence
    sub = Subscription(
        subscription_id="sub_test_usd_openai",
        user_id=TEST_USER,
        service_name="OpenAI ChatGPT Plus",
        status=SubscriptionStatus.ACTIVE,
        monthly_cost=20.0,
        currency="USD",
        original_amount=20.0,
        original_currency="USD",
        discovery_source=DiscoverySource.EMAIL,
    )
    save_subscription(sub)
    loaded = get_subscription_from_db(sub.subscription_id, user_id=TEST_USER)
    assert loaded.currency == "USD"
    assert loaded.original_currency == "USD"
    assert loaded.converted_amount is None


# ==============================================================================
# 14. Currency metadata survives database persistence and export
# ==============================================================================
def test_currency_metadata_survives_persistence_and_export():
    """Verify INR and USD metadata persist and appear in /api/user/export."""
    sub_inr = Subscription(
        subscription_id="sub_test_inr_spotify",
        user_id=TEST_USER,
        service_name="Spotify India",
        status=SubscriptionStatus.ACTIVE,
        monthly_cost=119.0,
        currency="INR",
        original_amount=119.0,
        original_currency="INR",
        discovery_source=DiscoverySource.MANUAL,
    )
    save_subscription(sub_inr)

    exp_resp = client.get("/api/user/export", headers={"X-User-ID": TEST_USER})
    assert exp_resp.status_code == 200
    export_data = exp_resp.json()

    exported_subs = export_data["subscriptions"]
    assert len(exported_subs) == 1
    s = exported_subs[0]
    assert s["service_name"] == "Spotify India"
    assert s["currency"] == "INR"
    assert s["original_currency"] == "INR"
    assert s["monthly_cost"] == 119.0


# ==============================================================================
# 15. Database cleanup preserves legitimate user data
# ==============================================================================
def test_database_cleanup_preserves_legitimate_user_data():
    """Verify cleanup_demo_and_mock_data removes mock fixtures but preserves user records."""
    # Create legitimate manual subscription
    manual_sub = Subscription(
        subscription_id="sub_man_my_vpn_service_123",
        user_id=TEST_USER,
        service_name="Mullvad VPN",
        status=SubscriptionStatus.ACTIVE,
        monthly_cost=5.0,
        currency="EUR",
        discovery_source=DiscoverySource.MANUAL,
    )
    save_subscription(manual_sub)

    # Create genuine Gmail candidate
    cand = SubscriptionCandidate(
        candidate_id="cand_test_legit_gmail_candidate",
        user_id=TEST_USER,
        merchant_name="Google One",
        estimated_cost=130.0,
        currency="INR",
        review_status=CandidateReviewStatus.PENDING,
        source_message_id="msg_google_one_real_id",
    )
    save_candidate(cand)

    # Run cleanup
    cleanup_res = cleanup_demo_and_mock_data(user_id=TEST_USER)
    assert cleanup_res["success"] is True

    # Legitimate manual subscription must still exist
    loaded_sub = get_subscription_from_db(manual_sub.subscription_id, user_id=TEST_USER)
    assert loaded_sub is not None
    assert loaded_sub.service_name == "Mullvad VPN"

    # Legitimate candidate must still exist
    loaded_cand = get_candidate_by_id(cand.candidate_id, user_id=TEST_USER)
    assert loaded_cand is not None
    assert loaded_cand.merchant_name == "Google One"


# ==============================================================================
# 16. Dashboard and exports contain no seeded demo records
# ==============================================================================
def test_dashboard_and_exports_contain_no_seeded_demo_records():
    """Verify /api/user/export contains 0 seeded connector demo records."""
    exp_resp = client.get("/api/user/export", headers={"X-User-ID": TEST_USER})
    assert exp_resp.status_code == 200
    export_data = exp_resp.json()

    demo_names = ["CineVault", "AudioCloud", "FitStream", "MovieMax", "GameStream"]
    for sub in export_data.get("subscriptions", []):
        assert not any(d in sub.get("service_name", "") for d in demo_names)


# ==============================================================================
# 17. Existing cancellation safety and approval tests continue passing
# ==============================================================================
def test_cancellation_safety_invariants_preserved():
    """Verify that runs require human approval and never auto-cancel."""
    from backend.app.agent import initialize_orchestration_run, load_fixture_data

    subs, evidence_map = load_fixture_data(["sub_tc1_cinevault"])
    as_of = datetime(2026, 10, 23, 8, 0, tzinfo=timezone.utc)

    run, context = initialize_orchestration_run(
        subscriptions=subs,
        evidence_map=evidence_map,
        as_of=as_of,
    )

    # Run created but NO subscription is cancelled
    for sub_id, state in context.items():
        assert state.plan is not None
        assert state.plan.status.value == "awaiting_approval"
        assert state.action_result is None
        assert state.verification_result is None
        assert state.final_status.value == "pending"
