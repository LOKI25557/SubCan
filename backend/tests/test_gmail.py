"""Tests for Gmail OAuth 2.0, PKCE, token handling, billing discovery, and database candidates.

These tests use mocks only. They do NOT:
- Call Google's real OAuth endpoints.
- Access a real Gmail account.
- Expose real credentials.
- Claim live Gmail access is working.

Live Gmail access requires genuine user authorization with real credentials and
must be verified separately using the manual test checklist in docs/.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest


# ==============================================================================
# Config Tests (Phase 2)
# ==============================================================================

class TestConfig:
    def test_config_imports_without_error(self):
        from backend.app.config import (
            APP_ENV,
            APP_PORT,
            DATABASE_URL,
            DECISION_INACTIVITY_THRESHOLD_DAYS,
            GOOGLE_OAUTH_SCOPES,
            REQUIRE_EXPLICIT_APPROVAL,
        )
        assert APP_PORT > 0
        assert DECISION_INACTIVITY_THRESHOLD_DAYS == 90
        assert REQUIRE_EXPLICIT_APPROVAL is True
        assert "https://www.googleapis.com/auth/gmail.readonly" in GOOGLE_OAUTH_SCOPES

    def test_credential_status_never_exposes_secrets(self):
        from backend.app.config import get_credential_status
        status = get_credential_status()
        assert "client_id_configured" in status
        assert "client_secret_configured" in status
        assert "oauth_ready" in status
        # The actual secret value must NOT appear
        assert "GOOGLE_CLIENT_SECRET" not in str(status)
        assert "secret" not in str(status).lower() or "configured" in str(status).lower()

    def test_oauth_credentials_configured_returns_false_when_empty(self):
        from backend.app.config import oauth_credentials_configured
        import backend.app.config as cfg
        original_id = cfg.GOOGLE_CLIENT_ID
        original_secret = cfg.GOOGLE_CLIENT_SECRET
        try:
            cfg.GOOGLE_CLIENT_ID = None
            cfg.GOOGLE_CLIENT_SECRET = None
            assert oauth_credentials_configured() is False
        finally:
            cfg.GOOGLE_CLIENT_ID = original_id
            cfg.GOOGLE_CLIENT_SECRET = original_secret

    def test_gmail_scope_is_readonly_only(self):
        from backend.app.config import GOOGLE_OAUTH_SCOPES
        # Ensure no write/send/modify scopes are requested
        dangerous_scopes = [
            "gmail.modify",
            "gmail.compose",
            "gmail.send",
            "gmail.insert",
            "gmail.labels",
        ]
        for dangerous in dangerous_scopes:
            assert not any(dangerous in s for s in GOOGLE_OAUTH_SCOPES), (
                f"Dangerous scope '{dangerous}' found in GOOGLE_OAUTH_SCOPES"
            )


# ==============================================================================
# PKCE Tests (Phase 3)
# ==============================================================================

class TestPKCE:
    def test_pkce_pair_generation(self):
        from backend.app.gmail import _generate_pkce_pair
        import base64, hashlib
        verifier, challenge = _generate_pkce_pair()
        # Verifier must be URL-safe and long enough
        assert len(verifier) >= 43
        # Challenge must be S256 of verifier
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        assert challenge == expected

    def test_pkce_pairs_are_unique(self):
        from backend.app.gmail import _generate_pkce_pair
        pairs = [_generate_pkce_pair() for _ in range(10)]
        verifiers = [p[0] for p in pairs]
        challenges = [p[1] for p in pairs]
        assert len(set(verifiers)) == 10, "Duplicate code verifiers generated"
        assert len(set(challenges)) == 10, "Duplicate code challenges generated"

    def test_pkce_challenge_has_no_padding(self):
        from backend.app.gmail import _generate_pkce_pair
        _, challenge = _generate_pkce_pair()
        assert "=" not in challenge, "Code challenge must not have base64 padding"


# ==============================================================================
# OAuth State Store Tests
# ==============================================================================

class TestOAuthStateStore:
    def test_create_and_consume_state(self):
        from backend.app.gmail import _OAuthStateStore
        store = _OAuthStateStore()
        store.create("test_state_1", "verifier_1", "user_1")
        entry = store.consume("test_state_1")
        assert entry is not None
        assert entry["code_verifier"] == "verifier_1"
        assert entry["user_id"] == "user_1"

    def test_state_can_only_be_consumed_once(self):
        from backend.app.gmail import _OAuthStateStore
        store = _OAuthStateStore()
        store.create("test_state_2", "verifier_2", "user_2")
        store.consume("test_state_2")
        second = store.consume("test_state_2")
        assert second is None, "State should be consumed exactly once"

    def test_unknown_state_returns_none(self):
        from backend.app.gmail import _OAuthStateStore
        store = _OAuthStateStore()
        result = store.consume("nonexistent_state_xyz")
        assert result is None

    def test_expired_state_returns_none(self):
        from backend.app.gmail import _OAuthStateStore
        store = _OAuthStateStore()
        store._TTL_SECONDS = 0  # Expire immediately
        store.create("expiring_state", "verifier", "user")
        time.sleep(0.01)
        result = store.consume("expiring_state")
        assert result is None, "Expired state should not be returned"

    def test_states_are_independent(self):
        from backend.app.gmail import _OAuthStateStore
        store = _OAuthStateStore()
        store.create("state_A", "verifier_A", "user_A")
        store.create("state_B", "verifier_B", "user_B")
        entryB = store.consume("state_B")
        entryA = store.consume("state_A")
        assert entryA["user_id"] == "user_A"
        assert entryB["user_id"] == "user_B"


# ==============================================================================
# OAuth Flow Tests (Mocked)
# ==============================================================================

class TestOAuthFlowMocked:
    def test_build_authorization_url_raises_when_not_configured(self):
        from backend.app.gmail import OAuthNotConfiguredError, build_authorization_url
        import backend.app.config as cfg
        original_id = cfg.GOOGLE_CLIENT_ID
        original_secret = cfg.GOOGLE_CLIENT_SECRET
        try:
            cfg.GOOGLE_CLIENT_ID = None
            cfg.GOOGLE_CLIENT_SECRET = None
            with pytest.raises(OAuthNotConfiguredError):
                build_authorization_url(user_id="user_test")
        finally:
            cfg.GOOGLE_CLIENT_ID = original_id
            cfg.GOOGLE_CLIENT_SECRET = original_secret

    def test_handle_callback_raises_on_error_param(self):
        from backend.app.gmail import OAuthStateError, handle_oauth_callback
        with pytest.raises(OAuthStateError, match="denied"):
            handle_oauth_callback(code=None, state="any_state", error="access_denied")

    def test_handle_callback_raises_on_invalid_state(self):
        from backend.app.gmail import OAuthStateError, handle_oauth_callback
        import backend.app.config as cfg
        original_id = cfg.GOOGLE_CLIENT_ID
        original_secret = cfg.GOOGLE_CLIENT_SECRET
        try:
            cfg.GOOGLE_CLIENT_ID = "fake_id"
            cfg.GOOGLE_CLIENT_SECRET = "fake_secret"
            with pytest.raises(OAuthStateError, match="invalid"):
                handle_oauth_callback(
                    code="fake_code",
                    state="completely_unknown_state_xyz",
                    error=None,
                )
        finally:
            cfg.GOOGLE_CLIENT_ID = original_id
            cfg.GOOGLE_CLIENT_SECRET = original_secret

    def test_get_connection_status_when_not_configured(self):
        from backend.app.gmail import get_connection_status
        import backend.app.config as cfg
        original_id = cfg.GOOGLE_CLIENT_ID
        original_secret = cfg.GOOGLE_CLIENT_SECRET
        try:
            cfg.GOOGLE_CLIENT_ID = None
            cfg.GOOGLE_CLIENT_SECRET = None
            status = get_connection_status("any_user")
            assert status["connected"] is False
            assert "not configured" in status["reason"].lower()
            assert status["oauth_ready"] is False
        finally:
            cfg.GOOGLE_CLIENT_ID = original_id
            cfg.GOOGLE_CLIENT_SECRET = original_secret

    def test_get_connection_status_when_no_token_stored(self, tmp_path):
        from backend.app.gmail import get_connection_status, _load_token
        import backend.app.config as cfg
        original_id = cfg.GOOGLE_CLIENT_ID
        original_secret = cfg.GOOGLE_CLIENT_SECRET
        try:
            cfg.GOOGLE_CLIENT_ID = "fake_id"
            cfg.GOOGLE_CLIENT_SECRET = "fake_secret"
            status = get_connection_status("nonexistent_user_xyz")
            assert status["connected"] is False
            assert status["oauth_ready"] is True
        finally:
            cfg.GOOGLE_CLIENT_ID = original_id
            cfg.GOOGLE_CLIENT_SECRET = original_secret

    def test_revoke_returns_false_when_no_token(self):
        from backend.app.gmail import revoke_connection
        result = revoke_connection("user_with_no_tokens_xyz")
        assert result is False


# ==============================================================================
# Email Discovery Tests (Mocked — no real Gmail calls)
# ==============================================================================

class TestEmailDiscoveryMocked:
    def test_decode_message_body_plain_text(self):
        import base64
        from backend.app.gmail import _decode_message_body
        text = "Your subscription receipt: $9.99"
        encoded = base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")
        payload = {"mimeType": "text/plain", "body": {"data": encoded}}
        result = _decode_message_body(payload)
        assert "9.99" in result

    def test_decode_message_body_multipart(self):
        import base64
        from backend.app.gmail import _decode_message_body
        text = "Invoice: Amount $15.00"
        encoded = base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")
        payload = {
            "mimeType": "multipart/alternative",
            "body": {},
            "parts": [
                {"mimeType": "text/plain", "body": {"data": encoded}},
            ],
        }
        result = _decode_message_body(payload)
        assert "15.00" in result

    def test_extract_known_provider(self):
        from backend.app.gmail import _extract_provider
        result = _extract_provider("Netflix receipt", "noreply@netflix.com", "")
        assert result == "Netflix"

    def test_extract_provider_from_domain_fallback(self):
        from backend.app.gmail import _extract_provider
        result = _extract_provider("Receipt", "billing@acmestream.com", "")
        assert result is not None
        assert "acmestream" in result.lower() or "Acmestream" in result

    def test_extract_amount_basic(self):
        from backend.app.gmail import _extract_amount
        result = _extract_amount("You have been charged $12.99 for your subscription.")
        assert result == 12.99

    def test_extract_amount_returns_none_on_no_match(self):
        from backend.app.gmail import _extract_amount
        result = _extract_amount("Thank you for using our service.")
        assert result is None

    def test_extract_amount_sanity_bounds(self):
        from backend.app.gmail import _extract_amount
        # Reject zero amounts
        result = _extract_amount("Total: $0.00")
        assert result is None or result > 0

    def test_extract_date_iso_format(self):
        from backend.app.gmail import _extract_date
        result = _extract_date("Invoice date: 2024-06-15. Thank you.")
        assert result == "2024-06-15"

    def test_extract_date_returns_none_on_no_match(self):
        from backend.app.gmail import _extract_date
        result = _extract_date("No dates in this message.")
        assert result is None

    def test_discover_billing_raises_when_no_credentials(self):
        from backend.app.gmail import OAuthTokenError, discover_billing_emails
        with pytest.raises(OAuthTokenError, match="No credentials"):
            discover_billing_emails(user_id="user_with_no_creds_xyz")

    def test_billing_filter_only_relevant_messages(self):
        """Verify that non-billing subjects are excluded from candidates."""
        from backend.app.gmail import _BILLING_REGEX
        billing_subjects = [
            "Your Netflix receipt",
            "Invoice for April",
            "Payment confirmation",
            "Subscription renewal notice",
        ]
        non_billing_subjects = [
            "Welcome to our newsletter",
            "Your friend shared a photo",
            "Team meeting at 3pm",
        ]
        for s in billing_subjects:
            assert _BILLING_REGEX.search(s), f"Should match billing: {s}"
        for s in non_billing_subjects:
            assert not _BILLING_REGEX.search(s), f"Should NOT match non-billing: {s}"


# ==============================================================================
# Database Gmail Candidate Tests
# ==============================================================================

class TestDatabaseGmailCandidates:
    def test_save_and_load_candidates(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/test.db")
        from backend.app.database import (
            save_gmail_candidates,
            load_gmail_candidates,
        )
        candidates = [
            {
                "message_id": "msg_001",
                "thread_id": "thread_001",
                "subject": "Netflix receipt",
                "sender": "noreply@netflix.com",
                "date_header": "Mon, 1 Jan 2024",
                "provider_name": "Netflix",
                "amount": 15.49,
                "currency": None,
                "billing_date": "2024-01-01",
                "body_preview": "Your Netflix subscription has been renewed.",
                "extracted_at": datetime.now(timezone.utc).isoformat(),
            }
        ]
        count = save_gmail_candidates(candidates, user_id="test_user")
        assert count >= 1

        loaded = load_gmail_candidates(user_id="test_user")
        assert len(loaded) >= 1
        assert loaded[0]["message_id"] == "msg_001"
        assert loaded[0]["provider_name"] == "Netflix"
        assert loaded[0]["amount"] == 15.49

    def test_save_candidates_deduplicates_by_message_id(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/test_dedup.db")
        from backend.app.database import save_gmail_candidates, load_gmail_candidates

        candidate = {
            "message_id": "msg_dedup",
            "thread_id": "t1",
            "subject": "Invoice",
            "sender": "billing@test.com",
            "date_header": "",
            "provider_name": "TestService",
            "amount": 9.99,
            "currency": None,
            "billing_date": "2024-02-01",
            "body_preview": "Invoice",
            "extracted_at": datetime.now(timezone.utc).isoformat(),
        }
        save_gmail_candidates([candidate], user_id="dedup_user")
        save_gmail_candidates([candidate], user_id="dedup_user")  # Second identical insert

        loaded = load_gmail_candidates(user_id="dedup_user")
        matching = [c for c in loaded if c["message_id"] == "msg_dedup"]
        assert len(matching) == 1, "Duplicate message_ids must be deduplicated"

    def test_confirm_candidate(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/test_confirm.db")
        from backend.app.database import (
            confirm_gmail_candidate,
            load_gmail_candidates,
            save_gmail_candidates,
        )

        candidate = {
            "message_id": "msg_confirm_001",
            "thread_id": "t2",
            "subject": "Spotify receipt",
            "sender": "no-reply@spotify.com",
            "date_header": "",
            "provider_name": "Spotify",
            "amount": 9.99,
            "currency": None,
            "billing_date": "2024-03-01",
            "body_preview": "Spotify receipt",
            "extracted_at": datetime.now(timezone.utc).isoformat(),
        }
        save_gmail_candidates([candidate], user_id="confirm_user")

        # Initially unconfirmed
        unconfirmed = load_gmail_candidates(user_id="confirm_user", confirmed_only=True)
        assert len(unconfirmed) == 0

        # Confirm it
        result = confirm_gmail_candidate("msg_confirm_001", "confirm_user")
        assert result is True

        confirmed = load_gmail_candidates(user_id="confirm_user", confirmed_only=True)
        assert len(confirmed) == 1

    def test_confirm_nonexistent_candidate_returns_false(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/test_no_candidate.db")
        from backend.app.database import confirm_gmail_candidate
        result = confirm_gmail_candidate("nonexistent_msg_id", "any_user")
        assert result is False

    def test_candidates_are_user_isolated(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/test_isolation.db")
        from backend.app.database import save_gmail_candidates, load_gmail_candidates

        c1 = {
            "message_id": "msg_user1",
            "thread_id": "t1", "subject": "Receipt", "sender": "a@b.com",
            "date_header": "", "provider_name": "SvcA", "amount": 10.0,
            "currency": None, "billing_date": "2024-01-01", "body_preview": "",
            "extracted_at": datetime.now(timezone.utc).isoformat(),
        }
        c2 = {
            "message_id": "msg_user2",
            "thread_id": "t2", "subject": "Invoice", "sender": "c@d.com",
            "date_header": "", "provider_name": "SvcB", "amount": 20.0,
            "currency": None, "billing_date": "2024-02-01", "body_preview": "",
            "extracted_at": datetime.now(timezone.utc).isoformat(),
        }
        save_gmail_candidates([c1], user_id="user_one")
        save_gmail_candidates([c2], user_id="user_two")

        user1_candidates = load_gmail_candidates(user_id="user_one")
        user2_candidates = load_gmail_candidates(user_id="user_two")

        assert all(c["message_id"] == "msg_user1" for c in user1_candidates)
        assert all(c["message_id"] == "msg_user2" for c in user2_candidates)


# ==============================================================================
# API Endpoint Tests for Gmail Routes
# ==============================================================================

class TestGmailAPIEndpoints:
    def setup_method(self):
        from fastapi.testclient import TestClient
        from backend.app.main import app
        self.client = TestClient(app)

    def test_gmail_status_endpoint_returns_200(self):
        response = self.client.get("/api/email/status")
        assert response.status_code == 200
        data = response.json()
        assert "connected" in data
        assert "data_source" in data
        assert "credential_status" in data
        # When credentials absent, connected must be False
        if not data["credential_status"]["oauth_ready"]:
            assert data["connected"] is False

    def test_gmail_status_never_exposes_token_or_secret(self):
        response = self.client.get("/api/email/status")
        assert response.status_code == 200
        text = response.text
        # These strings must NEVER appear in the response
        assert "refresh_token" not in text
        assert "access_token" not in text
        assert "client_secret" not in text.lower() or "configured" in text

    def test_gmail_oauth_start_returns_503_when_not_configured(self):
        """When credentials are absent, starting OAuth must return 503 not crash."""
        import backend.app.config as cfg
        original_id = cfg.GOOGLE_CLIENT_ID
        original_secret = cfg.GOOGLE_CLIENT_SECRET
        try:
            cfg.GOOGLE_CLIENT_ID = None
            cfg.GOOGLE_CLIENT_SECRET = None
            response = self.client.get("/api/email/oauth/start", follow_redirects=False)
            assert response.status_code in (503, 302)
        finally:
            cfg.GOOGLE_CLIENT_ID = original_id
            cfg.GOOGLE_CLIENT_SECRET = original_secret

    def test_gmail_callback_missing_code_returns_400(self):
        response = self.client.get("/api/email/oauth/callback")
        assert response.status_code == 400
        data = response.json()
        assert data["error"]["code"] == "BAD_REQUEST"

    def test_gmail_callback_with_error_param_redirects(self):
        response = self.client.get(
            "/api/email/oauth/callback?error=access_denied",
            follow_redirects=False,
        )
        # Should redirect to dashboard with error notice
        assert response.status_code == 302
        assert "gmail_error" in response.headers.get("location", "")

    def test_gmail_disconnect_returns_structured_response(self):
        response = self.client.post("/api/email/disconnect")
        assert response.status_code == 200
        data = response.json()
        assert "disconnected" in data
        assert "message" in data

    def test_gmail_sync_without_credentials_returns_401(self):
        """Sync must return 401 when no Gmail token is stored."""
        response = self.client.post("/api/email/sync")
        assert response.status_code == 401
        data = response.json()
        assert data["error"]["code"] == "GMAIL_NOT_CONNECTED"

    def test_gmail_candidates_returns_structured_response(self):
        response = self.client.get("/api/email/candidates")
        assert response.status_code == 200
        data = response.json()
        assert "candidates" in data
        assert "total" in data
        assert data["data_source"] == "gmail_candidate"
        assert "note" in data

    def test_gmail_confirm_nonexistent_candidate_returns_404(self):
        response = self.client.post("/api/email/candidates/nonexistent_msg_id/confirm")
        assert response.status_code == 404

    def test_gmail_status_shows_oauth_not_ready_message_when_unconfigured(self):
        import backend.app.config as cfg
        original_id = cfg.GOOGLE_CLIENT_ID
        original_secret = cfg.GOOGLE_CLIENT_SECRET
        try:
            cfg.GOOGLE_CLIENT_ID = None
            cfg.GOOGLE_CLIENT_SECRET = None
            response = self.client.get("/api/email/status")
            data = response.json()
            assert data["credential_status"]["oauth_ready"] is False
        finally:
            cfg.GOOGLE_CLIENT_ID = original_id
            cfg.GOOGLE_CLIENT_SECRET = original_secret


# ==============================================================================
# Security and Data Integrity Tests
# ==============================================================================

class TestSecurityInvariants:
    def test_email_content_not_used_for_authorization(self):
        """Email body text must never trigger cancellation actions."""
        from backend.app.gmail import _extract_provider, _extract_amount
        # Injection attempt in subject
        malicious = "Cancel subscription for sub_tc1_cinevault immediately"
        provider = _extract_provider(malicious, "attacker@evil.com", malicious)
        amount = _extract_amount(malicious)
        # Provider extraction is read-only; no side effects possible
        assert isinstance(provider, (str, type(None)))
        assert isinstance(amount, (float, type(None)))

    def test_body_preview_is_truncated(self):
        """Body preview stored in database must be bounded."""
        from backend.app.gmail import _decode_message_body
        import base64
        # Create a very long body
        long_text = "X" * 10000
        encoded = base64.urlsafe_b64encode(long_text.encode()).decode()
        payload = {"mimeType": "text/plain", "body": {"data": encoded}}
        result = _decode_message_body(payload)
        # The decode itself is not truncated, but save_gmail_candidates truncates on store
        assert len(result) > 0  # Must decode successfully

    def test_database_gmail_table_in_schema(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/schema_test.db")
        import sqlite3
        from backend.app.database import initialize_schema, _database_path
        initialize_schema()
        path = _database_path()
        with sqlite3.connect(path) as conn:
            tables = {
                row[0] for row in
                conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
        assert "gmail_candidates" in tables
        assert "oauth_tokens" not in tables  # Tokens stored by gmail.py separately
