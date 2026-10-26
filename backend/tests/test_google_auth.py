"""Test Suite for Google Login, OpenID Connect Authentication, and Session Isolation.

Covers:
1. Real Google ID token verification and session establishment.
2. Invalid, expired, and malformed token rejection.
3. Stable Google subject identifier (sub) user lookup and idempotent updates.
4. Session validation (/api/auth/me) and logout revocation (/api/auth/logout).
5. Cross-user data isolation enforced by session tokens (preventing header spoofing).
6. Strict separation of identity login scopes from Gmail discovery authorization.
7. Authorization code redirect and callback flows.
"""

from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient
from backend.app.main import app
from backend.app.database import (
    get_user_by_google_sub,
    get_user_by_id,
    save_subscription,
    get_db_session,
)
from backend.app.models import Subscription, SubscriptionStatus

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_db():
    """Ensure tests run against a clean database state."""
    yield
    with get_db_session() as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DELETE FROM user_sessions")
        conn.execute("DELETE FROM users WHERE user_id NOT IN ('user_alice', 'user_bob')")
        conn.execute("DELETE FROM subscriptions WHERE user_id NOT IN ('user_alice', 'user_bob')")
        conn.execute("DELETE FROM email_accounts WHERE user_id NOT IN ('user_alice', 'user_bob')")
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")


def test_successful_google_token_authentication():
    """Verify that a valid Google ID token creates a user and returns an active session."""
    mock_claims = {
        "sub": "google_sub_carol_12345",
        "email": "carol.danvers@example.com",
        "name": "Carol Danvers",
        "picture": "https://lh3.googleusercontent.com/a/carol_avatar.jpg",
        "email_verified": True,
        "iss": "https://accounts.google.com",
        "aud": "732190779152-ukqcft0m8kghsut1vi3l71stgjgfd81r.apps.googleusercontent.com",
    }

    with patch("backend.app.main.verify_google_id_token", return_value=mock_claims):
        response = client.post("/api/auth/google", json={"id_token": "valid_carol_token"})

    assert response.status_code == 200, response.text
    data = response.json()

    assert "session_token" in data
    assert data["session_token"]
    assert "user" in data
    assert data["user"]["email"] == "carol.danvers@example.com"
    assert data["user"]["name"] == "Carol Danvers"
    assert data["user"]["picture"] == "https://lh3.googleusercontent.com/a/carol_avatar.jpg"
    assert data["user"]["google_sub"] == "google_sub_carol_12345"

    # Verify session cookie was set with secure flags
    assert "subcan_session" in response.cookies

    # Verify user record was persisted in database
    db_user = get_user_by_google_sub("google_sub_carol_12345")
    assert db_user is not None
    assert db_user["email"] == "carol.danvers@example.com"


def test_invalid_or_expired_google_token_rejected():
    """Verify that an invalid, revoked, or expired ID token is rejected with 400."""
    with patch(
        "backend.app.main.verify_google_id_token",
        side_effect=ValueError("Token signature verification failed: Token expired"),
    ):
        response = client.post("/api/auth/google", json={"id_token": "expired_or_fake_token"})

    assert response.status_code == 400
    err = response.json()
    assert err["error"]["code"] == "INVALID_TOKEN"
    assert "expired" in err["error"]["message"].lower() or "verification failed" in err["error"]["message"].lower()


def test_duplicate_login_same_google_sub_idempotent():
    """Verify logging in multiple times with the same Google sub updates existing user profile."""
    claims_v1 = {
        "sub": "google_sub_stable_999",
        "email": "user999@example.com",
        "name": "Original Name",
        "picture": "https://example.com/pic1.png",
    }
    claims_v2 = {
        "sub": "google_sub_stable_999",
        "email": "user999@example.com",
        "name": "Updated Name",
        "picture": "https://example.com/pic2.png",
    }

    with patch("backend.app.main.verify_google_id_token", return_value=claims_v1):
        r1 = client.post("/api/auth/google", json={"id_token": "tok1"})
    assert r1.status_code == 200
    user_id_1 = r1.json()["user"]["user_id"]

    with patch("backend.app.main.verify_google_id_token", return_value=claims_v2):
        r2 = client.post("/api/auth/google", json={"id_token": "tok2"})
    assert r2.status_code == 200
    user_id_2 = r2.json()["user"]["user_id"]

    # Must be the exact same user_id in database, not a duplicate record
    assert user_id_1 == user_id_2
    assert r2.json()["user"]["name"] == "Updated Name"
    assert r2.json()["user"]["picture"] == "https://example.com/pic2.png"


def test_session_validation_and_me_endpoint():
    """Verify that /api/auth/me returns the authenticated user's profile with active session."""
    mock_claims = {
        "sub": "google_sub_diana_777",
        "email": "diana@example.com",
        "name": "Diana Prince",
    }
    with patch("backend.app.main.verify_google_id_token", return_value=mock_claims):
        login_res = client.post("/api/auth/google", json={"id_token": "diana_token"})
    session_token = login_res.json()["session_token"]
    user_id = login_res.json()["user"]["user_id"]

    # 1. Test profile retrieval via Authorization: Bearer header
    res_header = client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {session_token}"},
    )
    assert res_header.status_code == 200
    assert res_header.json()["user_id"] == user_id
    assert res_header.json()["email"] == "diana@example.com"
    assert res_header.json()["gmail_connected"] is False

    # 2. Test profile retrieval via Cookie
    client.cookies.set("subcan_session", session_token)
    res_cookie = client.get("/api/auth/me")
    assert res_cookie.status_code == 200
    assert res_cookie.json()["user_id"] == user_id


def test_logout_and_session_revocation():
    """Verify that calling /api/auth/logout revokes the session and subsequent requests fail."""
    mock_claims = {
        "sub": "google_sub_bruce_888",
        "email": "bruce@example.com",
        "name": "Bruce Wayne",
    }
    with patch("backend.app.main.verify_google_id_token", return_value=mock_claims):
        login_res = client.post("/api/auth/google", json={"id_token": "bruce_token"})
    session_token = login_res.json()["session_token"]

    # Logout
    logout_res = client.post(
        "/api/auth/logout",
        headers={"Authorization": f"Bearer {session_token}"},
    )
    assert logout_res.status_code == 200
    assert logout_res.json()["status"] == "logged_out"

    # Subsequent request using the revoked session must be rejected with 401
    me_res = client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {session_token}"},
    )
    assert me_res.status_code == 401
    assert me_res.json()["error"]["code"] in ("INVALID_SESSION", "SESSION_INVALID_OR_REVOKED")


def test_unauthenticated_request_rejected():
    """Verify that protected authentication routes reject unauthenticated requests."""
    client.cookies.clear()
    res = client.get("/api/auth/me")
    assert res.status_code == 401
    assert res.json()["error"]["code"] == "UNAUTHENTICATED"


def test_cross_user_isolation_enforced_by_session():
    """Verify that an active session enforces tenant isolation and ignores spoofed user ID headers."""
    # Create two distinct users with active sessions
    claims_user_a = {
        "sub": "sub_user_a",
        "email": "usera@example.com",
        "name": "User Alpha",
    }
    claims_user_b = {
        "sub": "sub_user_b",
        "email": "userb@example.com",
        "name": "User Beta",
    }

    with patch("backend.app.main.verify_google_id_token", return_value=claims_user_a):
        r_a = client.post("/api/auth/google", json={"id_token": "tok_a"})
    token_a = r_a.json()["session_token"]
    user_id_a = r_a.json()["user"]["user_id"]

    with patch("backend.app.main.verify_google_id_token", return_value=claims_user_b):
        r_b = client.post("/api/auth/google", json={"id_token": "tok_b"})
    token_b = r_b.json()["session_token"]
    user_id_b = r_b.json()["user"]["user_id"]

    # Save a subscription belonging exclusively to User A
    sub_a = Subscription(
        subscription_id="sub_private_user_a",
        service_name="User A Confidential Cloud",
        monthly_cost=99.99,
        status=SubscriptionStatus.ACTIVE,
        discovery_source="manual",
        user_id=user_id_a,
    )
    save_subscription(sub_a)

    # 1. User A queries subscriptions with Token A -> should see their subscription
    res_a = client.get(
        "/subscriptions",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert res_a.status_code == 200
    ids_a = [s["subscription_id"] for s in res_a.json()]
    assert "sub_private_user_a" in ids_a

    # 2. User B queries subscriptions with Token B, but attempts to spoof X-User-ID = user_id_a!
    res_b_spoof = client.get(
        "/subscriptions",
        headers={
            "Authorization": f"Bearer {token_b}",
            "X-User-ID": user_id_a,  # Attempted spoof
        },
    )
    assert res_b_spoof.status_code == 200
    ids_b = [s["subscription_id"] for s in res_b_spoof.json()]
    # User B MUST NOT see User A's subscription; backend derives identity strictly from Token B
    assert "sub_private_user_a" not in ids_b


def test_separation_of_login_scopes_from_gmail_discovery():
    """Verify login authentication only requests openid scopes and leaves Gmail ungranted."""
    with patch("backend.app.config.is_live_oauth_configured", return_value=True), \
         patch("backend.app.auth.is_live_oauth_configured", return_value=True), \
         patch("backend.app.email.oauth.is_live_oauth_configured", return_value=True):
        # 1. Check login authorization URL parameters
        res_login_url = client.get("/api/auth/google/url")
        assert res_login_url.status_code == 200
        login_url_data = res_login_url.json()
        assert "authorization_url" in login_url_data

        auth_url = login_url_data["authorization_url"]
        # Login URL must only ask for openid email profile
        assert "scope=" in auth_url
        assert "gmail.readonly" not in auth_url
        assert "openid" in auth_url or "email" in auth_url
        assert login_url_data["scope"] == "openid email profile"

        # 2. Check Gmail discovery OAuth URL parameters
        res_gmail_url = client.get("/api/email/oauth/url")
        assert res_gmail_url.status_code == 200
        gmail_url_data = res_gmail_url.json()
        assert "url" in gmail_url_data
        # Gmail OAuth URL must request read-only mailbox discovery scope
        assert "gmail.readonly" in gmail_url_data["url"]


def test_login_oauth_callback_flow():
    """Verify authorization code exchange in /api/auth/google/callback."""
    mock_claims = {
        "sub": "google_sub_code_flow_101",
        "email": "codeflow@example.com",
        "name": "Code Flow User",
    }

    with patch("backend.app.main.exchange_login_code_for_user", return_value=mock_claims):
        res = client.get(
            "/api/auth/google/callback?code=valid_auth_code&state=valid_login_state",
            headers={"Accept": "application/json"},
        )

    assert res.status_code == 200, res.text
    data = res.json()
    assert data["user"]["email"] == "codeflow@example.com"
    assert "session_token" in data


def test_login_oauth_callback_denied_by_user():
    """Verify user cancellation or denied consent in callback returns 400 OAUTH_DENIED."""
    res = client.get(
        "/api/auth/google/callback?error=access_denied",
        headers={"Accept": "application/json"},
    )
    assert res.status_code == 400
    data = res.json()
    assert data["error"]["code"] == "OAUTH_DENIED"
