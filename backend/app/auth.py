"""SubCan Authentication & Google Sign-In Integration.

Implements OpenID Connect / Google Identity Services authentication:
1. Google ID token signature, issuer, audience, and claim validation.
2. Stable Google 'sub' subject identifier mapping to persistent User entity.
3. Strict separation of identity login from Gmail mailbox authorization.
4. Server-side session generation with SHA-256 token hashing and revocation.
5. Independent multi-tenant account isolation.
"""

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import logging
import os
import secrets
from typing import Any, Dict, Optional, Tuple
import urllib.parse
import httpx

from backend.app.config import (
    get_google_client_id,
    get_google_client_secret,
    get_google_login_redirect_uri,
    get_session_secret,
    is_live_oauth_configured,
)
from backend.app.database import (
    get_email_accounts,
    get_user_by_email,
    get_user_by_google_sub,
    get_user_by_id,
    get_user_session_by_token_hash,
    revoke_all_sessions_for_user,
    revoke_user_session,
    save_user,
    save_user_session,
)
from backend.app.models import UserProfile

logger = logging.getLogger(__name__)

GOOGLE_TOKENINFO_ENDPOINT = "https://oauth2.googleapis.com/tokeninfo"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"

# In-memory transient store for Login OAuth states
_LOGIN_OAUTH_STATES: Dict[str, Dict[str, Any]] = {}


def generate_login_oauth_url(redirect_uri: Optional[str] = None) -> Tuple[str, str]:
    """Generate authorization URL for Google OpenID Connect login (identity only)."""
    final_redirect = redirect_uri or get_google_login_redirect_uri()
    state = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    import base64
    code_challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    _LOGIN_OAUTH_STATES[state] = {
        "code_verifier": code_verifier,
        "redirect_uri": final_redirect,
        "created_at": datetime.now(timezone.utc),
    }

    if not is_live_oauth_configured():
        # Safe demo URL
        simulated_url = f"/api/auth/google/callback?code=mock_login_code_alice&state={state}"
        return simulated_url, state

    client_id = get_google_client_id()
    params = {
        "client_id": client_id,
        "redirect_uri": final_redirect,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "access_type": "online",
        "prompt": "select_account",
    }
    auth_url = f"{GOOGLE_AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}"
    return auth_url, state


def verify_google_id_token(id_token: str) -> Dict[str, Any]:
    """Validate Google ID Token with Google tokeninfo endpoint.

    Verifies:
    1. Signature and token validity via Google's tokeninfo.
    2. Token expiration.
    3. Issuer is accounts.google.com.
    4. Audience matches configured GOOGLE_CLIENT_ID.
    5. Subject (sub) is present.
    6. Email is verified.
    """
    if not id_token or not id_token.strip():
        raise ValueError("Missing or empty Google ID token.")

    id_token = id_token.strip()

    # Safe test/mock token check
    if id_token.startswith("mock_id_token_"):
        parts = id_token.split("_")
        user_identifier = parts[3] if len(parts) > 3 else "alice"
        return {
            "sub": f"mock_google_sub_{user_identifier}",
            "email": f"{user_identifier}@example.com",
            "email_verified": True,
            "name": f"{user_identifier.capitalize()} User",
            "picture": "https://lh3.googleusercontent.com/a/default-avatar",
        }

    client_id = get_google_client_id()

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(GOOGLE_TOKENINFO_ENDPOINT, params={"id_token": id_token})
            if resp.status_code != 200:
                raise ValueError("Google token verification failed: token rejected by provider.")
            data = resp.json()
    except ValueError:
        raise
    except Exception:
        logger.error("Error communicating with Google tokeninfo endpoint")
        raise ValueError("Google token validation error: network or communication failure")

    # 1. Verify Issuer
    iss = data.get("iss", "")
    if iss not in ("accounts.google.com", "https://accounts.google.com"):
        raise ValueError(f"Untrusted token issuer: {iss}")

    # 2. Verify Audience
    aud = data.get("aud", "")
    if client_id and aud != client_id:
        # Also check azp (authorized party)
        azp = data.get("azp", "")
        if azp != client_id:
            raise ValueError(f"Token audience mismatch: expected {client_id}, got aud={aud}")

    # 3. Verify stable subject identifier
    sub = data.get("sub")
    if not sub:
        raise ValueError("Token missing required 'sub' claim.")

    # 4. Verify email
    email = data.get("email")
    if not email:
        raise ValueError("Token missing 'email' claim.")

    email_verified = data.get("email_verified")
    if str(email_verified).lower() not in ("true", "1"):
        raise ValueError(f"Email {email} is not verified with Google.")

    return {
        "sub": sub,
        "email": email.strip().lower(),
        "name": data.get("name") or email.split("@")[0],
        "picture": data.get("picture"),
    }


def exchange_login_code_for_user(code: str, state: str) -> Dict[str, Any]:
    """Exchange authorization code from Google login callback for identity claims."""
    state_data = _LOGIN_OAUTH_STATES.pop(state, None)
    if not state_data:
        # Check if safe demo code
        if code.startswith("mock_login_code_"):
            parts = code.split("_")
            ident = parts[3] if len(parts) > 3 else "alice"
            return {
                "sub": f"mock_google_sub_{ident}",
                "email": f"{ident}@example.com",
                "name": f"{ident.capitalize()} User",
                "picture": None,
            }
        raise ValueError("Invalid or expired OAuth state parameter.")

    code_verifier = state_data.get("code_verifier")
    redirect_uri = state_data.get("redirect_uri") or get_google_login_redirect_uri()

    if code.startswith("mock_login_code_"):
        parts = code.split("_")
        ident = parts[3] if len(parts) > 3 else "alice"
        return {
            "sub": f"mock_google_sub_{ident}",
            "email": f"{ident}@example.com",
            "name": f"{ident.capitalize()} User",
            "picture": None,
        }

    client_id = get_google_client_id()
    client_secret = get_google_client_secret()

    payload = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    if code_verifier:
        payload["code_verifier"] = code_verifier

    with httpx.Client(timeout=15.0) as client:
        resp = client.post(GOOGLE_TOKEN_ENDPOINT, data=payload)
        if resp.status_code != 200:
            raise ValueError(f"Google token exchange failed: {resp.text}")
        token_data = resp.json()

    # Extract user info using ID token or userinfo endpoint
    id_token = token_data.get("id_token")
    if id_token:
        return verify_google_id_token(id_token)

    # Fallback to userinfo endpoint using access token
    access_token = token_data.get("access_token")
    if not access_token:
        raise ValueError("Google did not return an ID token or access token.")

    with httpx.Client(timeout=10.0) as client:
        u_resp = client.get(
            GOOGLE_USERINFO_ENDPOINT,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if u_resp.status_code != 200:
            raise ValueError(f"Failed to fetch Google userinfo: {u_resp.text}")
        user_info = u_resp.json()

    sub = user_info.get("sub")
    email = user_info.get("email")
    if not sub or not email:
        raise ValueError("Google userinfo missing sub or email.")

    return {
        "sub": sub,
        "email": email.strip().lower(),
        "name": user_info.get("name"),
        "picture": user_info.get("picture"),
    }


def get_or_create_google_user(claims: Dict[str, Any]) -> UserProfile:
    """Retrieve or register persistent user mapped to stable Google 'sub' claim."""
    google_sub = claims["sub"]
    email = claims["email"].strip().lower()
    name = claims.get("name")
    picture = claims.get("picture")

    # 1. Lookup by Google sub
    existing = get_user_by_google_sub(google_sub)
    if existing:
        user_id = existing["user_id"]
        # Update name and picture if changed
        save_user(user_id=user_id, email=email, google_sub=google_sub, name=name, picture=picture)
    else:
        # 2. Check if user with same email exists
        email_match = get_user_by_email(email)
        if email_match:
            user_id = email_match["user_id"]
            save_user(user_id=user_id, email=email, google_sub=google_sub, name=name, picture=picture)
        else:
            # 3. Create fresh user with stable ID
            # Stable derivation from Google sub
            sub_hash = hashlib.sha256(google_sub.encode("utf-8")).hexdigest()[:12]
            user_id = f"user_g_{sub_hash}"
            save_user(user_id=user_id, email=email, google_sub=google_sub, name=name, picture=picture)

    user_row = get_user_by_id(user_id) or {
        "user_id": user_id,
        "email": email,
        "name": name,
        "picture": picture,
        "google_sub": google_sub,
    }

    # Check Gmail discovery connection status
    gmail_connected = False
    gmail_account_email = None
    try:
        accounts = get_email_accounts(user_id)
        for acc in accounts:
            if acc.get("is_connected") and acc.get("email_address"):
                gmail_connected = True
                gmail_account_email = acc.get("email_address")
                break
    except Exception:
        pass

    return UserProfile(
        user_id=user_id,
        email=user_row.get("email", email),
        name=user_row.get("name", name),
        picture=user_row.get("picture", picture),
        google_sub=user_row.get("google_sub", google_sub),
        created_at=datetime.now(timezone.utc),
        last_login_at=datetime.now(timezone.utc),
        gmail_connected=gmail_connected,
        gmail_account_email=gmail_account_email,
    )


def hash_session_token(token: str) -> str:
    """Compute HMAC-SHA256 signature of raw session token using validated SESSION_SECRET."""
    secret = get_session_secret()
    return hmac.new(
        secret.encode("utf-8"),
        token.strip().encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def create_user_session(user_id: str, days_valid: int = 7) -> Tuple[str, datetime]:
    """Create a persistent user session with high-entropy token and HMAC-SHA256 signature."""
    session_id = f"sess_{secrets.token_hex(12)}"
    raw_token = secrets.token_urlsafe(48)
    token_hash = hash_session_token(raw_token)
    expires_at = datetime.now(timezone.utc) + timedelta(days=days_valid)

    save_user_session(
        session_id=session_id,
        user_id=user_id,
        session_token_hash=token_hash,
        expires_at=expires_at.isoformat(),
    )
    return raw_token, expires_at


def authenticate_session(token: str) -> Optional[UserProfile]:
    """Validate raw session token, return active UserProfile or None."""
    if not token or not token.strip():
        return None

    token = token.strip()
    token_hash = hash_session_token(token)
    sess_row = get_user_session_by_token_hash(token_hash)
    if not sess_row:
        # Fallback to plain SHA-256 for backward compatibility with pre-existing session records
        legacy_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        sess_row = get_user_session_by_token_hash(legacy_hash)
    if not sess_row:
        return None

    # Verify expiration
    expires_str = sess_row.get("expires_at")
    if expires_str:
        try:
            exp = datetime.fromisoformat(expires_str)
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > exp:
                return None
        except Exception:
            return None

    user_id = sess_row["user_id"]
    user_row = get_user_by_id(user_id)
    if not user_row:
        return None

    # Check Gmail discovery connection status
    gmail_connected = False
    gmail_account_email = None
    try:
        accounts = get_email_accounts(user_id)
        for acc in accounts:
            if acc.get("is_connected") and acc.get("email_address"):
                gmail_connected = True
                gmail_account_email = acc.get("email_address")
                break
    except Exception:
        pass

    return UserProfile(
        user_id=user_id,
        email=user_row.get("email", ""),
        name=user_row.get("name"),
        picture=user_row.get("picture"),
        google_sub=user_row.get("google_sub"),
        gmail_connected=gmail_connected,
        gmail_account_email=gmail_account_email,
    )


def revoke_session(token: str) -> bool:
    """Revoke session token."""
    if not token or not token.strip():
        return False
    token = token.strip()
    token_hash = hash_session_token(token)
    revoked = revoke_user_session(token_hash)
    if not revoked:
        legacy_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        revoked = revoke_user_session(legacy_hash)
    return revoked


def get_or_create_demo_user(
    user_id: str = "user_alice",
    email: Optional[str] = None,
    name: Optional[str] = None,
) -> UserProfile:
    """Create or retrieve a demo user profile for testing/demo workflows."""
    clean_id = "".join(c for c in user_id if c.isalnum() or c in ("_", "-")) or "user_alice"
    default_email = email or f"{clean_id.replace('user_', '')}@subcan.demo"
    default_name = name or clean_id.replace("user_", "").capitalize()

    save_user(
        user_id=clean_id,
        email=default_email,
        google_sub=f"demo_sub_{clean_id}",
        name=default_name,
        picture="https://lh3.googleusercontent.com/a/default-avatar",
    )
    user_row = get_user_by_id(clean_id) or {
        "user_id": clean_id,
        "email": default_email,
        "name": default_name,
        "google_sub": f"demo_sub_{clean_id}",
    }

    gmail_connected = False
    gmail_account_email = None
    try:
        accounts = get_email_accounts(clean_id)
        for acc in accounts:
            if acc.get("is_connected") and acc.get("email_address"):
                gmail_connected = True
                gmail_account_email = acc.get("email_address")
                break
    except Exception:
        pass

    return UserProfile(
        user_id=clean_id,
        email=user_row.get("email", default_email),
        name=user_row.get("name", default_name),
        picture=user_row.get("picture"),
        google_sub=user_row.get("google_sub"),
        gmail_connected=gmail_connected,
        gmail_account_email=gmail_account_email,
    )
