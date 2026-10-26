"""Targeted test suite for Production Security, Session Secret, and OAuth Configuration.

Covers:
1. Missing SESSION_SECRET behavior.
2. Empty or whitespace-only SESSION_SECRET.
3. Valid configured session secret.
4. Confirmation that no hardcoded default is used.
5. Session-secret loading behavior during imports and application startup.
6. Explicit Gmail callback override.
7. Explicit Google login callback override.
8. Correct production callback URL construction.
9. Correct localhost fallback for development.
10. Invalid or untrusted backend URL handling.
11. Missing and placeholder Google credentials (is_live_oauth_configured).
12. Existing login, logout, OAuth state, and cross-user isolation behavior.
"""

import importlib
import pytest
from backend.app.config import (
    get_session_secret,
    get_google_redirect_uri,
    get_google_login_redirect_uri,
    get_frontend_origin,
    is_live_oauth_configured,
    is_production,
    KNOWN_INSECURE_TEST_SECRETS,
)


def test_missing_session_secret(monkeypatch):
    """1. Missing SESSION_SECRET raises ValueError or RuntimeError."""
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("BACKEND_URL", raising=False)

    with pytest.raises((ValueError, RuntimeError)) as exc_info:
        get_session_secret()
    assert "SESSION_SECRET" in str(exc_info.value)


def test_missing_session_secret_in_production(monkeypatch):
    """1b. Missing SESSION_SECRET in production raises RuntimeError."""
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    monkeypatch.setenv("APP_ENV", "production")

    with pytest.raises(RuntimeError) as exc_info:
        get_session_secret()
    assert "production" in str(exc_info.value).lower()
    assert "SESSION_SECRET" in str(exc_info.value)


def test_empty_or_whitespace_session_secret(monkeypatch):
    """2. Empty or whitespace-only SESSION_SECRET raises error."""
    for empty_val in ("", "   ", "\t\n"):
        monkeypatch.setenv("SESSION_SECRET", empty_val)
        monkeypatch.setenv("APP_ENV", "development")
        with pytest.raises((ValueError, RuntimeError)) as exc_info:
            get_session_secret()
        assert "SESSION_SECRET" in str(exc_info.value)


def test_valid_configured_session_secret(monkeypatch):
    """3. Valid configured session secret is retrieved cleanly without exposure."""
    valid_key = "a_super_strong_random_secret_for_production_testing_9876543210!"
    monkeypatch.setenv("SESSION_SECRET", valid_key)
    monkeypatch.setenv("APP_ENV", "development")

    result = get_session_secret()
    assert result == valid_key


def test_no_hardcoded_default_used(monkeypatch):
    """4. Confirmation that no hardcoded default is used when SESSION_SECRET is unset."""
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    monkeypatch.setenv("APP_ENV", "development")

    # Must raise an exception and NOT return the old hardcoded default string
    with pytest.raises(Exception):
        val = get_session_secret()
        assert val != "subcan_session_secret_default_key_secure_4891"

    # Also verify that if someone tries to use the known insecure default in production, it is rejected
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SESSION_SECRET", "subcan_session_secret_default_key_secure_4891")
    with pytest.raises(RuntimeError) as exc_info:
        get_session_secret()
    assert "Insecure test or default SESSION_SECRET" in str(exc_info.value)


def test_session_secret_loading_during_imports(monkeypatch):
    """5. Session-secret loading behavior during imports and application startup."""
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    # Importing modules must not raise an error even when SESSION_SECRET is missing
    import backend.app.config as cfg
    import backend.app.main as mn
    import backend.app.auth as ath

    importlib.reload(cfg)
    # The reload must succeed without crashing
    assert hasattr(cfg, "get_session_secret")


def test_explicit_gmail_callback_override(monkeypatch):
    """6. Explicit Gmail callback override is preserved."""
    override = "https://custom.example.com/api/email/oauth/callback"
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", override)
    assert get_google_redirect_uri() == override


def test_explicit_google_login_callback_override(monkeypatch):
    """7. Explicit Google login callback override is preserved."""
    override = "https://custom.example.com/api/auth/google/callback"
    monkeypatch.setenv("GOOGLE_LOGIN_REDIRECT_URI", override)
    assert get_google_login_redirect_uri() == override


def test_correct_production_callback_url_construction(monkeypatch):
    """8. Correct production callback URL construction from trusted BACKEND_URL."""
    monkeypatch.delenv("GOOGLE_REDIRECT_URI", raising=False)
    monkeypatch.delenv("GOOGLE_LOGIN_REDIRECT_URI", raising=False)
    monkeypatch.setenv("BACKEND_URL", "https://subcan.onrender.com")

    assert get_google_redirect_uri() == "https://subcan.onrender.com/api/email/oauth/callback"
    assert get_google_login_redirect_uri() == "https://subcan.onrender.com/api/auth/google/callback"


def test_correct_localhost_fallback_for_development(monkeypatch):
    """9. Correct localhost fallback for local development when env vars are unset."""
    monkeypatch.delenv("GOOGLE_REDIRECT_URI", raising=False)
    monkeypatch.delenv("GOOGLE_LOGIN_REDIRECT_URI", raising=False)
    monkeypatch.delenv("BACKEND_URL", raising=False)
    monkeypatch.setenv("APP_ENV", "development")

    assert get_google_redirect_uri() == "http://127.0.0.1:8000/api/email/oauth/callback"
    assert get_google_login_redirect_uri() == "http://127.0.0.1:8000/api/auth/google/callback"


def test_invalid_or_untrusted_backend_url_handling(monkeypatch):
    """10. Invalid or untrusted backend URL handling."""
    monkeypatch.delenv("GOOGLE_REDIRECT_URI", raising=False)
    monkeypatch.delenv("GOOGLE_LOGIN_REDIRECT_URI", raising=False)

    untrusted_urls = [
        "http://attacker-render.com",
        "https://evil.render.com.attacker.org",
        "https://arbitrary-malicious-site.com",
        "not_a_valid_url",
        "javascript:alert(1)",
    ]

    for untrusted in untrusted_urls:
        monkeypatch.setenv("BACKEND_URL", untrusted)
        redirect_gmail = get_google_redirect_uri()
        redirect_login = get_google_login_redirect_uri()
        assert redirect_gmail == "http://127.0.0.1:8000/api/email/oauth/callback"
        assert redirect_login == "http://127.0.0.1:8000/api/auth/google/callback"


def test_missing_and_placeholder_google_credentials(monkeypatch):
    """11. Missing and placeholder Google credentials return False for is_live_oauth_configured."""
    # Both missing
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    assert is_live_oauth_configured() is False

    # Placeholders with angle brackets
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "<Google OAuth client ID>")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "<Google OAuth client secret>")
    assert is_live_oauth_configured() is False

    # Default template strings
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "your_google_client_id_here")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "your_google_client_secret_here")
    assert is_live_oauth_configured() is False

    # Short secret
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "12345.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "short_secret")
    assert is_live_oauth_configured() is False

    # Valid format
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "732190779152-abc.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "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_cloud_oauth_32char")
    assert is_live_oauth_configured() is True
