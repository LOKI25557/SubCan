"""Comprehensive Test Suite for Live Gmail Evidence Parsing, Lifecycle Reconciliation, and Database Audit.

Tests:
1. McAfee protection ended notice: cost is None, lifecycle is expired, $1,471 survey quote is rejected.
2. Udemy tracking URL: skipped as promotional marketing (not extracted as ₹2.00).
3. Unacademy newsletter: skipped as promotional marketing.
4. Google One refund message: reconciles candidate to 'refunded' lifecycle with cancellation_signal=True.
5. Spotify tier downgrade ("You're no longer on Standard"): reconciles candidate to 'payment_failed' lifecycle with cancellation_signal=True.
6. Microsoft 365 Personal trial email: classified as 'trial' lifecycle with trial_end_date and recurring cost ₹689.00 INR.
7. Deduplication idempotency: repeated sync does not duplicate candidates or corrupt existing user review statuses.
8. Database audit: dry_run=True reports changes without modifying DB; dry_run=False applies cleanups safely.
9. Database audit API endpoint: POST /api/database/audit handles requests with dry_run flag.
10. Candidate confirmation into inventory maps lifecycle_status and trial_end_date to Subscription.
"""

from datetime import date, datetime, timezone
import pytest
from fastapi.testclient import TestClient

from backend.app.database import (
    audit_and_reconcile_database,
    delete_user_data,
    get_subscription_candidate,
    get_subscription_from_db,
    initialize_schema,
    list_subscription_candidates,
    list_subscriptions_from_db,
    save_subscription_candidate,
    save_subscription,
)
from backend.app.email.parser import (
    evaluate_billing_email,
    extract_dates,
    extract_monetary_amount,
    parse_billing_email,
)
from backend.app.email.service import (
    confirm_candidate_into_inventory,
    group_and_deduplicate_candidates,
)
from backend.app.main import app
from backend.app.models import (
    CandidateReviewStatus,
    DiscoverySource,
    EmailEvidenceCategory,
    Subscription,
    SubscriptionCandidate,
    SubscriptionStatus,
)

client = TestClient(app)
TEST_USER = "test_user_live_evidence_suite"


@pytest.fixture(autouse=True)
def setup_teardown_test_user():
    initialize_schema()
    delete_user_data(TEST_USER)
    yield
    delete_user_data(TEST_USER)


# ==============================================================================
# 1. McAfee Protection Ended Notice & Survey Quote Masking
# ==============================================================================
def test_mcafee_survey_quote_masked_and_marked_expired():
    """McAfee email with $1,471 survey statistic must return cost=None and lifecycle=expired."""
    mcafee_body = (
        "Your McAfee protection has ended! Protect yourself before it's too late.\n"
        "According to a recent study by McAfee, identity theft victims lost on average $1,471 per incident.\n"
        "Renew your protection today to stay secure online."
    )
    result = evaluate_billing_email(
        message_id="msg_mcafee_test_01",
        subject="Your protection has ended!",
        sender="McAfee <info@mcafee.com>",
        raw_content=mcafee_body,
    )
    assert result.status == "candidate"
    cand = result.candidate
    assert cand is not None
    assert cand.merchant_name == "McAfee"
    # Cost MUST be None: $1,471 is an industry study statistic, not a billing receipt
    assert cand.estimated_cost is None
    assert cand.currency is None
    assert cand.cancellation_signal is True
    assert cand.lifecycle_status == "expired"


# ==============================================================================
# 2. Udemy Promotional Marketing Skipped
# ==============================================================================
def test_udemy_marketing_link_skipped():
    """Udemy sales email with tracking URL must be skipped, not extracted as ₹2.00."""
    udemy_body = (
        "Don't miss these top rated courses!\n"
        "Save up to 85% on tech courses today.\n"
        "Click here: https://click.e.udemymail.com/u/?qs=43936034d6199a0995168baad20e542\n"
        "Learn Python, AI, and Web Development."
    )
    result = evaluate_billing_email(
        message_id="msg_udemy_test_01",
        subject="Top courses just for you, Lokesh",
        sender="Udemy <udemy@email.udemy.com>",
        raw_content=udemy_body,
    )
    assert result.status == "skipped"
    assert result.reason in ("not_billing_related", "marketing_promotional")


# ==============================================================================
# 3. Unacademy Newsletter Skipped
# ==============================================================================
def test_unacademy_newsletter_skipped():
    """Unacademy weekly newsletter must be skipped as marketing."""
    unacad_body = (
        "Here is your Unacademy Weekly Round-up!\n"
        "Join free live classes and test your preparation.\n"
        "Unacademy Plus learners scored top ranks in recent national exams."
    )
    result = evaluate_billing_email(
        message_id="msg_unacad_test_01",
        subject="Unacademy Weekly - Stay ahead in your preparation",
        sender="Unacademy <digest@unacademy.com>",
        raw_content=unacad_body,
    )
    assert result.status == "skipped"


# ==============================================================================
# 4. Google One Refund Reconciles to Refunded Lifecycle
# ==============================================================================
def test_google_one_refund_reconciles_candidate_to_refunded():
    """Google Play Order followed by Refund Approved reconciles to 'refunded' lifecycle."""
    msg_order = {
        "id": "msg_g1_order",
        "subject": "Your Google Play Order Receipt from Oct 17, 2026",
        "sender": "Google Play <googleplay-noreply@google.com>",
        "body": "Google AI Plus (400 GB) (Google One)\nMonthly subscription: ₹399.00/month\nAuto-renews on Oct 17, 2027.",
        "internalDate": "1729150000000",
    }
    msg_refund = {
        "id": "msg_g1_refund",
        "subject": "Your Google Play refund has been approved",
        "sender": "Google Play <googleplay-noreply@google.com>",
        "body": "We have approved your refund for Google AI Plus (400 GB) (Google One). Your subscription has been cancelled and refunded.",
        "internalDate": "1729151000000",
    }

    res_order = evaluate_billing_email(msg_order["id"], msg_order["subject"], msg_order["sender"], msg_order["body"])
    res_refund = evaluate_billing_email(msg_refund["id"], msg_refund["subject"], msg_refund["sender"], msg_refund["body"])

    assert res_order.status == "candidate"
    assert res_refund.status == "candidate"

    c1 = res_order.candidate
    c1.source_date = datetime(2026, 10, 17, 10, 0, tzinfo=timezone.utc)

    c2 = res_refund.candidate
    c2.source_date = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)

    grouped = group_and_deduplicate_candidates([c1, c2])
    assert len(grouped) == 1
    reconciled = grouped[0]
    assert reconciled.merchant_name == "Google One"
    assert reconciled.lifecycle_status == "refunded"
    assert reconciled.cancellation_signal is True


# ==============================================================================
# 5. Spotify Downgrade Reconciles to Payment Failed
# ==============================================================================
def test_spotify_downgrade_reconciles_to_payment_failed():
    """Spotify signup followed by 'You're no longer on Standard' reconciles to payment_failed."""
    msg_active = {
        "id": "msg_spot_order",
        "subject": "We hope you're enjoying Premium Standard",
        "sender": "Spotify <no-reply@spotify.com>",
        "body": "Your subscription: Spotify Premium Standard at ₹199.00/month. Auto-renews on April 4, 2026.",
    }
    msg_downgrade = {
        "id": "msg_spot_down",
        "subject": "You're no longer on Standard.",
        "sender": "Spotify <no-reply@spotify.com>",
        "body": "We were unable to process your recent payment for Spotify Premium. You have been downgraded to Spotify Free.",
    }

    r1 = evaluate_billing_email(msg_active["id"], msg_active["subject"], msg_active["sender"], msg_active["body"])
    r2 = evaluate_billing_email(msg_downgrade["id"], msg_downgrade["subject"], msg_downgrade["sender"], msg_downgrade["body"])

    assert r1.status == "candidate"
    assert r2.status == "candidate"

    c1 = r1.candidate
    c1.source_date = datetime(2026, 3, 4, 10, 0, tzinfo=timezone.utc)

    c2 = r2.candidate
    c2.source_date = datetime(2026, 3, 5, 10, 0, tzinfo=timezone.utc)

    grouped = group_and_deduplicate_candidates([c1, c2])
    assert len(grouped) == 1
    reconciled = grouped[0]
    assert reconciled.merchant_name == "Spotify"
    assert reconciled.lifecycle_status == "payment_failed"
    assert reconciled.cancellation_signal is True


# ==============================================================================
# 6. Microsoft 365 Personal Classified as Trial with ₹689.00 INR
# ==============================================================================
def test_microsoft_365_trial_classification_and_price():
    """Microsoft 365 order receipt with trial period must be classified as 'trial' with ₹689.00 INR."""
    ms_body = (
        "Your Google Play Order Receipt from 18 Oct 2026\n"
        "Microsoft 365 Personal with trial (Microsoft PowerPoint)\n"
        "Trial period: 1 month free trial ending on 17 Nov 2026.\n"
        "Starting 17 Nov 2026, you will be charged ₹689.00 / month automatically.\n"
        "Learn more about Google Play refund policies."
    )
    result = evaluate_billing_email(
        message_id="msg_ms365_test",
        subject="Your Google Play Order Receipt from 18 Oct 2026",
        sender="Google Play <googleplay-noreply@google.com>",
        raw_content=ms_body,
    )
    assert result.status == "candidate"
    cand = result.candidate
    assert cand is not None
    assert cand.merchant_name == "Microsoft 365"
    assert cand.estimated_cost == 689.0
    assert cand.currency == "INR"
    assert cand.lifecycle_status == "trial"
    assert cand.trial_end_date == date(2026, 11, 17)
    assert cand.detected_renewal_date == date(2026, 11, 17)
    # Important: Receipt footer mentioning 'refund policies' must NOT mark subscription as cancelled
    assert cand.cancellation_signal is False


# ==============================================================================
# 7. Deduplication Idempotency Across Multiple Syncs
# ==============================================================================
def test_deduplication_idempotency():
    """Multiple candidate deduplication passes must produce identical IDs and counts."""
    c1 = SubscriptionCandidate(
        candidate_id="cand_test_dupe_01",
        user_id=TEST_USER,
        merchant_name="Netflix",
        product_name="Standard",
        estimated_cost=649.0,
        currency="INR",
        lifecycle_status="active",
        source_message_id="msg_net_01",
        source_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    c2 = SubscriptionCandidate(
        candidate_id="cand_test_dupe_02",
        user_id=TEST_USER,
        merchant_name="Netflix",
        product_name="Standard HD",
        estimated_cost=649.0,
        currency="INR",
        lifecycle_status="active",
        source_message_id="msg_net_02",
        source_date=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )

    pass1 = group_and_deduplicate_candidates([c1, c2])
    assert len(pass1) == 1
    id1 = pass1[0].candidate_id

    # Second pass with same inputs
    pass2 = group_and_deduplicate_candidates([c1, c2])
    assert len(pass2) == 1
    assert pass2[0].candidate_id == id1


# ==============================================================================
# 8. Database Audit Dry-Run vs Apply Behavior
# ==============================================================================
def test_database_audit_dry_run_vs_apply():
    """audit_and_reconcile_database dry_run=True reports without deleting; dry_run=False applies."""
    # Seed a test mock subscription and a legitimate manual subscription
    mock_sub = Subscription(
        subscription_id="sub_tc1_cinevault",
        service_name="CineVault",
        status=SubscriptionStatus.ACTIVE,
        monthly_cost=15.99,
        currency="USD",
        user_id=TEST_USER,
        discovery_source="connector",
    )
    # Directly save mock fixture
    with pytest.MonkeyPatch.context() as mp:
        # Bypass connector block in save_subscription
        from backend.app.database import get_db_session
        with get_db_session() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO subscriptions(subscription_id, service_name, status, payload_json, user_id, discovery_source) VALUES (?, ?, ?, ?, ?, ?)",
                (mock_sub.subscription_id, mock_sub.service_name, mock_sub.status.value, mock_sub.model_dump_json(), TEST_USER, "connector"),
            )

    real_manual = Subscription(
        subscription_id="sub_man_myvpn_12345",
        service_name="My Private VPN",
        status=SubscriptionStatus.ACTIVE,
        monthly_cost=499.0,
        currency="INR",
        user_id=TEST_USER,
        discovery_source="manual",
    )
    save_subscription(real_manual)

    # 1. Dry run
    report_dry = audit_and_reconcile_database(user_id=TEST_USER, dry_run=True)
    assert report_dry["dry_run"] is True
    assert any(s["subscription_id"] == "sub_tc1_cinevault" for s in report_dry["mock_subscriptions_to_remove"])
    assert any(s["subscription_id"] == "sub_man_myvpn_12345" for s in report_dry["legitimate_subscriptions_preserved"])

    # Verify mock still exists in DB
    assert get_subscription_from_db("sub_tc1_cinevault", user_id=TEST_USER) is not None

    # 2. Apply reconciliation
    report_apply = audit_and_reconcile_database(user_id=TEST_USER, dry_run=False)
    assert report_apply["dry_run"] is False

    # Verify mock is removed, real manual is preserved
    assert get_subscription_from_db("sub_tc1_cinevault", user_id=TEST_USER) is None
    assert get_subscription_from_db("sub_man_myvpn_12345", user_id=TEST_USER) is not None


# ==============================================================================
# 9. Database Audit API Endpoint
# ==============================================================================
def test_database_audit_api_endpoint():
    """POST /api/database/audit returns valid structured report."""
    resp = client.post(
        "/api/database/audit",
        headers={"X-User-ID": TEST_USER},
        json={"dry_run": True},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["user_id"] == TEST_USER
    assert data["dry_run"] is True
    assert "mock_subscriptions_to_remove" in data
    assert "legitimate_subscriptions_preserved" in data


# ==============================================================================
# 10. Candidate Confirmation Preserves Lifecycle Metadata
# ==============================================================================
def test_confirm_candidate_copies_lifecycle_metadata():
    """confirm_candidate_into_inventory copies lifecycle_status and trial_end_date to Subscription."""
    cand = SubscriptionCandidate(
        candidate_id="cand_test_ms365_confirm",
        user_id=TEST_USER,
        merchant_name="Microsoft 365",
        product_name="Personal",
        estimated_cost=689.0,
        currency="INR",
        lifecycle_status="trial",
        trial_end_date=date(2026, 11, 17),
        detected_renewal_date=date(2026, 11, 17),
        evidence_category="trial_notice",
        confidence=0.95,
        source_message_id="msg_ms_01",
        source_date=datetime(2026, 10, 18, tzinfo=timezone.utc),
    )
    save_subscription_candidate(cand)

    sub, _ = confirm_candidate_into_inventory(cand.candidate_id, user_id=TEST_USER)
    assert sub.subscription_id.startswith("sub_email_")
    assert sub.status == SubscriptionStatus.TRIAL
    assert sub.lifecycle_status == "trial"
    assert sub.trial_end_date == date(2026, 11, 17)
    assert sub.monthly_cost == 689.0
    assert sub.currency == "INR"
    assert sub.evidence_category == "trial_notice"
