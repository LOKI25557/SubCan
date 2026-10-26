"""SubCan centralized application configuration and environment management.

Ensures the project-level .env is loaded reliably before any downstream
module evaluates configuration or initializes services.
"""

from pathlib import Path
import os
from typing import Optional, Tuple
import urllib.parse
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


def is_production() -> bool:
    """Determine whether the application is running in a production environment."""
    env = (os.getenv("APP_ENV") or os.getenv("ENVIRONMENT") or os.getenv("ENV") or "").lower().strip()
    if env in ("development", "dev", "test", "testing", "local"):
        return False
    if env in ("production", "prod"):
        return True
    if os.getenv("RENDER", "").lower().strip() in ("true", "1"):
        return True
    backend_url = os.getenv("BACKEND_URL", "").lower().strip()
    # Check if backend_url is an external HTTPS production URL on Render or custom domain
    if backend_url.startswith("https://") and "localhost" not in backend_url and "127.0.0.1" not in backend_url:
        return True
    return False


def get_google_client_id() -> str:
    """Retrieve stripped Google OAuth Client ID."""
    return os.getenv("GOOGLE_CLIENT_ID", "").strip()


def get_google_client_secret() -> str:
    """Retrieve stripped Google OAuth Client Secret (never log or expose)."""
    return os.getenv("GOOGLE_CLIENT_SECRET", "").strip()


def _is_trusted_production_backend(parsed: urllib.parse.ParseResult) -> bool:
    """Validate whether the parsed URL belongs to a trusted production backend."""
    if parsed.scheme != "https":
        return False
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return False
    # Trusted production domains for SubCan
    if hostname == "subcan.onrender.com" or hostname.endswith(".onrender.com"):
        return True
    # If explicitly allowed in FRONTEND_ORIGIN or CORS_ORIGINS
    cors = os.getenv("CORS_ORIGINS", "")
    for origin in cors.split(","):
        try:
            o_parsed = urllib.parse.urlparse(origin.strip())
            if o_parsed.hostname and o_parsed.hostname.lower() == hostname:
                return True
        except Exception:
            pass
    return False


def get_google_redirect_uri() -> str:
    """Retrieve configured OAuth redirect URI for Gmail discovery.

    Order of precedence:
    1. Explicit GOOGLE_REDIRECT_URI environment variable (preserved as configured).
    2. Derived from trusted BACKEND_URL (HTTPS in production).
    3. Safe localhost fallback for local development.
    """
    explicit = os.getenv("GOOGLE_REDIRECT_URI", "").strip()
    if explicit:
        return explicit

    backend_url = os.getenv("BACKEND_URL", "").strip().rstrip("/")
    if backend_url:
        try:
            parsed = urllib.parse.urlparse(backend_url)
            if _is_trusted_production_backend(parsed):
                path = parsed.path.rstrip("/")
                return f"{parsed.scheme}://{parsed.netloc}{path}/api/email/oauth/callback"
        except Exception:
            pass

    return "http://127.0.0.1:8000/api/email/oauth/callback"


def get_google_login_redirect_uri() -> str:
    """Retrieve configured OAuth redirect URI for user authentication/login.

    Order of precedence:
    1. Explicit GOOGLE_LOGIN_REDIRECT_URI environment variable (preserved as configured).
    2. Derived from trusted BACKEND_URL (HTTPS in production).
    3. Safe localhost fallback for local development.
    """
    explicit = os.getenv("GOOGLE_LOGIN_REDIRECT_URI", "").strip()
    if explicit:
        return explicit

    backend_url = os.getenv("BACKEND_URL", "").strip().rstrip("/")
    if backend_url:
        try:
            parsed = urllib.parse.urlparse(backend_url)
            if _is_trusted_production_backend(parsed):
                path = parsed.path.rstrip("/")
                return f"{parsed.scheme}://{parsed.netloc}{path}/api/auth/google/callback"
        except Exception:
            pass

    return "http://127.0.0.1:8000/api/auth/google/callback"


KNOWN_INSECURE_TEST_SECRETS = {
    "subcan_session_secret_default_key_secure_4891",
    "test-session-secret-for-pytest-only-32bytes!",
    "subcan_local_dev_session_secret_insecure_only_for_dev_32b",
    "secret",
    "changeme",
    "placeholder",
}


def get_session_secret() -> str:
    """Retrieve session signing key.

    In production, a non-empty SESSION_SECRET is strictly required and cannot be a
    known test/placeholder value.

    Raises:
        ValueError: If SESSION_SECRET is missing, empty, or whitespace-only.
        RuntimeError: If running in production with a missing, empty, or test-only secret.
    """
    secret = os.getenv("SESSION_SECRET", "").strip()

    if not secret:
        if is_production():
            raise RuntimeError(
                "SESSION_SECRET must be configured as a non-empty environment variable in production."
            )
        raise ValueError(
            "SESSION_SECRET is not configured or is empty. Please set a non-empty SESSION_SECRET."
        )

    if is_production() and secret.lower() in KNOWN_INSECURE_TEST_SECRETS:
        raise RuntimeError(
            "Insecure test or default SESSION_SECRET cannot be used in production. "
            "Please configure a unique, high-entropy secret in your production environment."
        )

    return secret


def get_frontend_origin() -> str:
    """Retrieve configured frontend origin, e.g. https://subcan-frontend.onrender.com."""
    explicit = os.getenv("FRONTEND_ORIGIN", "").strip().rstrip("/")
    if explicit:
        try:
            parsed = urllib.parse.urlparse(explicit)
            if parsed.scheme in ("http", "https") and parsed.netloc:
                return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
        except Exception:
            pass

    cors = os.getenv("CORS_ORIGINS", "")
    for origin in cors.split(","):
        origin = origin.strip().rstrip("/")
        if "frontend" in origin or "subcan-frontend" in origin:
            try:
                parsed = urllib.parse.urlparse(origin)
                if parsed.scheme in ("http", "https") and parsed.netloc:
                    return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
            except Exception:
                pass

    if is_production():
        return "https://subcan-frontend.onrender.com"

    return ""


def is_cookie_secure() -> bool:
    """Determine whether cookies must use the Secure flag."""
    val = os.getenv("COOKIE_SECURE", "").strip().lower()
    if val in ("true", "1", "yes"):
        return True
    if val in ("false", "0", "no"):
        return False
    return is_production()


def is_live_oauth_configured() -> bool:
    """Determine whether valid, non-placeholder Google OAuth credentials exist.

    Returns True only when both GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are
    provided, non-empty, and not marked as synthetic or placeholder values.
    """
    client_id = get_google_client_id()
    client_secret = get_google_client_secret()

    if not client_id or not client_secret:
        return False

    lowered_id = client_id.lower()
    lowered_secret = client_secret.lower()

    # Reject common placeholder and template patterns
    if (
        lowered_id.startswith("mock_")
        or lowered_id.startswith("test_")
        or "placeholder" in lowered_id
        or "<" in lowered_id
        or ">" in lowered_id
        or lowered_id in ("your_google_client_id_here", "your_client_id", "client_id", "none", "null")
    ):
        return False

    if (
        lowered_secret.startswith("mock_")
        or lowered_secret.startswith("test_")
        or "placeholder" in lowered_secret
        or "<" in lowered_secret
        or ">" in lowered_secret
        or len(client_secret) < 16
        or lowered_secret in ("your_google_client_secret_here", "your_client_secret", "client_secret", "none", "null")
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
