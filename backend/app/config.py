"""SubCan application configuration loader.

Reads settings from environment variables (populated from .env via python-dotenv
at startup in main.py). Provides typed, validated access to all settings with
safe defaults for development.

SECURITY RULES:
- Never log or print secret values.
- Never expose credentials in error messages.
- Missing required credentials produce a clear status, not a crash.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional


def _load_dotenv_if_present() -> None:
    """Load .env into os.environ if the file exists; silently skip if absent."""
    try:
        from dotenv import load_dotenv  # python-dotenv
        env_path = Path(__file__).resolve().parents[2] / ".env"
        if env_path.exists():
            load_dotenv(dotenv_path=env_path, override=False)
    except ImportError:
        pass  # python-dotenv not installed — rely on shell environment


_load_dotenv_if_present()


# ==============================================================================
# Application Settings
# ==============================================================================

APP_ENV: str = os.getenv("APP_ENV", "development")
APP_HOST: str = os.getenv("APP_HOST", "127.0.0.1")
APP_PORT: int = int(os.getenv("APP_PORT", "8000"))
DEBUG: bool = os.getenv("DEBUG", "true").lower() in ("1", "true", "yes")

# In production, DEBUG must be explicitly disabled.
if APP_ENV == "production" and DEBUG:
    import warnings
    warnings.warn(
        "DEBUG=True is set but APP_ENV=production. Set DEBUG=False for production.",
        stacklevel=1,
    )

# ==============================================================================
# Database
# ==============================================================================

DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./reports/subcan.db")

# ==============================================================================
# Agent / Decision Rules
# ==============================================================================

DECISION_INACTIVITY_THRESHOLD_DAYS: int = int(
    os.getenv("DECISION_INACTIVITY_THRESHOLD_DAYS", "90")
)
REQUIRE_EXPLICIT_APPROVAL: bool = os.getenv(
    "REQUIRE_EXPLICIT_APPROVAL", "true"
).lower() in ("1", "true", "yes")

# ==============================================================================
# Mock Site Connector
# ==============================================================================

MOCK_SITE_BASE_URL: str = os.getenv(
    "MOCK_SITE_BASE_URL", "https://mock.streaming.test"
)
MOCK_SITE_TIMEOUT_SECONDS: float = float(
    os.getenv("MOCK_SITE_TIMEOUT_SECONDS", "5.0")
)

# ==============================================================================
# Google OAuth 2.0
# ==============================================================================

GOOGLE_CLIENT_ID: Optional[str] = os.getenv("GOOGLE_CLIENT_ID") or None
GOOGLE_CLIENT_SECRET: Optional[str] = os.getenv("GOOGLE_CLIENT_SECRET") or None
GOOGLE_REDIRECT_URI: str = os.getenv(
    "GOOGLE_REDIRECT_URI",
    f"http://{APP_HOST}:{APP_PORT}/api/email/oauth/callback",
)

# Minimum Gmail scopes required — read-only metadata + message bodies for billing discovery.
# Do NOT add gmail.modify, gmail.send, or broader scopes.
GOOGLE_OAUTH_SCOPES: List[str] = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]

# ==============================================================================
# CORS
# ==============================================================================

_cors_raw: str = os.getenv(
    "CORS_ORIGINS",
    f"http://localhost:{APP_PORT},http://127.0.0.1:{APP_PORT},http://localhost:3000",
)
CORS_ORIGINS: List[str] = [o.strip() for o in _cors_raw.split(",") if o.strip()]

# ==============================================================================
# OAuth credential status helper (never prints secrets)
# ==============================================================================

def oauth_credentials_configured() -> bool:
    """Return True only when both client ID and secret are non-empty."""
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


def get_credential_status() -> dict:
    """Return a safe status dict — never includes actual secret values."""
    return {
        "client_id_configured": bool(GOOGLE_CLIENT_ID),
        "client_secret_configured": bool(GOOGLE_CLIENT_SECRET),
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "scopes": GOOGLE_OAUTH_SCOPES,
        "oauth_ready": oauth_credentials_configured(),
    }
