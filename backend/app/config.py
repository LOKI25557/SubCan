"""SubCan centralized application configuration and environment management.

Ensures the project-level .env is loaded reliably before any downstream
module evaluates configuration or initializes services.
"""

from pathlib import Path
import os
from typing import Optional, Tuple
from dotenv import load_dotenv

# Project root resolution (always relative to backend/app/config.py)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = PROJECT_ROOT / ".env"

# Explicitly load root .env into os.environ if present; do not override existing process env vars
if ENV_FILE.exists():
    load_dotenv(dotenv_path=ENV_FILE, override=False)
else:
    # Also attempt standard discovery in case the current working directory is different
    load_dotenv(override=False)


def get_google_client_id() -> str:
    """Retrieve stripped Google OAuth Client ID."""
    return os.getenv("GOOGLE_CLIENT_ID", "").strip()


def get_google_client_secret() -> str:
    """Retrieve stripped Google OAuth Client Secret (never log or expose)."""
    return os.getenv("GOOGLE_CLIENT_SECRET", "").strip()


def get_google_redirect_uri() -> str:
    """Retrieve configured OAuth redirect URI."""
    return os.getenv(
        "GOOGLE_REDIRECT_URI",
        "http://127.0.0.1:8000/api/email/oauth/callback",
    ).strip()


def is_live_oauth_configured() -> bool:
    """Determine whether valid, non-placeholder Google OAuth credentials exist.

    Returns True only when both GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are
    provided, non-empty, and not marked as synthetic or placeholder values.
    """
    client_id = get_google_client_id()
    client_secret = get_google_client_secret()

    if not client_id or not client_secret:
        return False

    # Guard against default example or test tokens
    lowered_id = client_id.lower()
    lowered_secret = client_secret.lower()
    if (
        lowered_id.startswith("mock_")
        or lowered_id.startswith("test_")
        or "placeholder" in lowered_id
        or lowered_id == "your_google_client_id_here"
    ):
        return False

    if (
        lowered_secret.startswith("mock_")
        or lowered_secret.startswith("test_")
        or "placeholder" in lowered_secret
        or lowered_secret == "your_google_client_secret_here"
    ):
        return False

    return True


def validate_oauth_config() -> Tuple[bool, Optional[str]]:
    """Validate Google OAuth configuration without exposing sensitive secret values.

    Returns:
        (is_valid, error_description)
    """
    client_id = get_google_client_id()
    client_secret = get_google_client_secret()
    redirect_uri = get_google_redirect_uri()

    if not client_id:
        return False, "Missing GOOGLE_CLIENT_ID"
    if not client_secret:
        return False, "Missing GOOGLE_CLIENT_SECRET"
    if not redirect_uri:
        return False, "Missing GOOGLE_REDIRECT_URI"
    if not (redirect_uri.startswith("http://") or redirect_uri.startswith("https://")):
        return False, "Invalid GOOGLE_REDIRECT_URI: must begin with http:// or https://"

    return True, None
