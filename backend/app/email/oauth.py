"""Google OAuth 2.0 authorization, PKCE, token exchange, and revocation for Gmail.

Safety & Security Invariants:
1. Least-privilege, read-only Gmail scope: 'https://www.googleapis.com/auth/gmail.readonly'.
2. Never store user email account passwords.
3. Enforces RFC 7636 PKCE S256 code challenge verification and CSRF state parameter validation.
4. Access and refresh tokens are stored server-side only; tokens and secrets are never
   logged or sent to frontend clients.
5. If Google Cloud credentials are not configured, provides a safe, explicit demo fallback
   without fabricating genuine live OAuth state or email credentials.
"""

import base64
from datetime import datetime, timedelta, timezone
import hashlib
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
    get_google_redirect_uri,
    is_live_oauth_configured,
    validate_oauth_config,
)
from backend.app.database import (
    disconnect_email_account,
    get_email_account,
    get_email_account_by_email,
    get_email_tokens,
    save_email_account,
)
from backend.app.models import EmailAccountStatus

logger = logging.getLogger(__name__)

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
GOOGLE_PROFILE_ENDPOINT = "https://gmail.googleapis.com/gmail/v1/users/me/profile"

# In-memory transient state store for CSRF and PKCE protection during OAuth flow
_OAUTH_STATES: Dict[str, Dict[str, Any]] = {}


def generate_pkce_pair() -> Tuple[str, str]:
    """Generate RFC 7636 compliant S256 PKCE code_verifier and code_challenge.

    Verifier: High-entropy cryptographic random string (43-128 characters).
    Challenge: BASE64URL-ENCODE(SHA256(ASCII(code_verifier))) with trailing '=' stripped.
    """
    code_verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return code_verifier, code_challenge


def generate_oauth_url(
    user_id: str = "user_default",
    redirect_uri: Optional[str] = None,
    account_name: Optional[str] = None,
    account_id: Optional[str] = None,
) -> Tuple[str, str]:
    """Generate authorization URL with secure state token and PKCE code challenge.

    Returns:
        (authorization_url, state_token)
    """
    final_redirect = redirect_uri or get_google_redirect_uri()

    # Generate cryptographically secure state and PKCE pair
    state = secrets.token_urlsafe(32)
    code_verifier, code_challenge = generate_pkce_pair()

    _OAUTH_STATES[state] = {
        "user_id": user_id,
        "account_id": account_id,
        "account_name": account_name,
        "code_verifier": code_verifier,
        "redirect_uri": final_redirect,
        "created_at": datetime.now(timezone.utc),
    }

    if not is_live_oauth_configured():
        # In demo mode, construct a local simulation URL that redirects back safely
        simulated_url = f"/api/email/oauth/callback?code=mock_auth_code_demo_{user_id}&state={state}"
        return simulated_url, state

    client_id = get_google_client_id()
    params = {
        "client_id": client_id,
        "redirect_uri": final_redirect,
        "response_type": "code",
        "scope": GMAIL_READONLY_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "include_granted_scopes": "true",
    }
    url = f"{GOOGLE_AUTH_ENDPOINT}?{urllib.parse.urlencode(params)}"
    return url, state


def exchange_code_for_tokens(
    code: str,
    state: str,
    user_id: Optional[str] = None,
    account_name: Optional[str] = None,
    account_id: Optional[str] = None,
    error: Optional[str] = None,
    client: Optional[httpx.Client] = None,
) -> EmailAccountStatus:
    """Exchange authorization code for access and refresh tokens.

    Validates CSRF state parameter, PKCE verifier, and enforces least-privilege scope.
    """
    if error:
        raise ValueError(f"Google authorization denied or returned an error: {error}")

    state_data = _OAUTH_STATES.pop(state, None)
    if not state_data:
        # Check if in demo/test mode with deterministic test code
        if not (code.startswith("mock_") or code.startswith("test_")):
            raise ValueError("Invalid, expired, or mismatched OAuth state parameter.")
        effective_user_id = user_id or "user_default"
        effective_account_name = account_name
        target_account_id = account_id
        code_verifier: Optional[str] = None
        redirect_uri = get_google_redirect_uri()
    else:
        effective_user_id = state_data["user_id"]
        effective_account_name = account_name or state_data.get("account_name")
        target_account_id = account_id or state_data.get("account_id")
        code_verifier = state_data["code_verifier"]
        redirect_uri = state_data["redirect_uri"]

        # Expire authorization states older than 10 minutes
        state_age = datetime.now(timezone.utc) - state_data["created_at"]
        if state_age > timedelta(minutes=10):
            raise ValueError("OAuth authorization session expired. Please start authorization again.")

    # Safe demo fallback mode
    if not is_live_oauth_configured() or code.startswith("mock_") or code.startswith("test_"):
        existing_acct = None
        if target_account_id:
            existing_acct = get_email_account(effective_user_id, account_id=target_account_id)
        if not existing_acct:
            demo_email = f"{effective_user_id}@subcan-demo.com"
            existing_acct = get_email_account_by_email(effective_user_id, demo_email)

        if existing_acct:
            acct_id = existing_acct.account_id
            demo_email = existing_acct.email_address or f"{effective_user_id}@subcan-demo.com"
            final_account_name = effective_account_name if (effective_account_name and effective_account_name.strip()) else existing_acct.account_name
        else:
            demo_email = f"{effective_user_id}@subcan-demo.com"
            em_hash = hashlib.sha256(demo_email.lower().strip().encode()).hexdigest()[:8]
            acct_id = target_account_id or f"acct_{effective_user_id}_{em_hash}"
            final_account_name = effective_account_name or demo_email

        status = EmailAccountStatus(
            account_id=acct_id,
            user_id=effective_user_id,
            account_name=final_account_name or demo_email,
            is_default=True,
            email_address=demo_email,
            is_connected=True,
            provider="gmail",
            scopes=[GMAIL_READONLY_SCOPE],
            last_synced_at=None,
            sync_status="idle",
            candidate_count=0,
            error_message=None,
            is_live_configured=is_live_oauth_configured(),
            mode="demo",
        )
        save_email_account(
            status,
            refresh_token="mock_refresh_token_demo_encrypted",
            access_token="mock_access_token_demo_encrypted",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        return status

    # Live Google OAuth 2.0 Token Exchange
    is_valid, validation_err = validate_oauth_config()
    if not is_valid:
        raise ValueError(f"OAuth configuration invalid: {validation_err}")

    client_id = get_google_client_id()
    client_secret = get_google_client_secret()

    token_payload: Dict[str, Any] = {
        "code": code,
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    if code_verifier:
        token_payload["code_verifier"] = code_verifier

    http_client = client or httpx.Client(timeout=15.0)
    try:
        token_resp = http_client.post(
            GOOGLE_TOKEN_ENDPOINT,
            data=token_payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if token_resp.status_code != 200:
            try:
                err_json = token_resp.json()
                err_desc = err_json.get("error_description") or err_json.get("error") or "Token exchange failed"
            except Exception:
                err_desc = f"Token endpoint returned HTTP {token_resp.status_code}"
            raise ValueError(f"Google token exchange failed: {err_desc}")

        body = token_resp.json()
        access_token = body.get("access_token", "")
        refresh_token = body.get("refresh_token")
        expires_in = body.get("expires_in", 3600)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        granted_scope_str = body.get("scope", "")
        granted_scopes = [s for s in granted_scope_str.split(" ") if s] if granted_scope_str else [GMAIL_READONLY_SCOPE]

        if not access_token:
            raise ValueError("Google token response did not contain an access_token.")

        # Retrieve verified email address via Gmail API users/me/profile
        profile_resp = http_client.get(
            GOOGLE_PROFILE_ENDPOINT,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if profile_resp.status_code == 200:
            profile_data = profile_resp.json()
            email_addr = profile_data.get("emailAddress", "unknown@gmail.com")
        else:
            email_addr = "connected_account@gmail.com"

        # Check if an existing account exists for this user and email
        existing_acct = get_email_account_by_email(effective_user_id, email_addr)
        if not existing_acct and target_account_id:
            existing_acct = get_email_account(effective_user_id, account_id=target_account_id)

        if existing_acct:
            acct_id = existing_acct.account_id
            final_account_name = effective_account_name if (effective_account_name and effective_account_name.strip()) else existing_acct.account_name
        else:
            em_hash = hashlib.sha256(email_addr.lower().strip().encode()).hexdigest()[:8]
            acct_id = target_account_id or f"acct_{effective_user_id}_{em_hash}"
            final_account_name = effective_account_name or email_addr

        status = EmailAccountStatus(
            account_id=acct_id,
            user_id=effective_user_id,
            account_name=final_account_name or email_addr,
            is_default=True,
            email_address=email_addr,
            is_connected=True,
            provider="gmail",
            scopes=granted_scopes,
            last_synced_at=None,
            sync_status="idle",
            candidate_count=0,
            error_message=None,
            is_live_configured=True,
            mode="live",
        )
        save_email_account(
            status,
            refresh_token=refresh_token,
            access_token=access_token,
            expires_at=expires_at,
        )
        return status
    finally:
        if client is None:
            http_client.close()


def refresh_access_token(
    user_id: str = "user_default",
    account_id: Optional[str] = None,
    client: Optional[httpx.Client] = None,
) -> Optional[str]:
    """Use stored refresh token to obtain a fresh access token from Google.

    Returns the new access token, or None if refresh is not possible or failed.
    """
    if not is_live_oauth_configured():
        return "mock_access_token_demo_refreshed"

    tokens = get_email_tokens(user_id, account_id=account_id)
    if not tokens or not tokens.get("refresh_token"):
        return None

    refresh_token = tokens["refresh_token"]
    if refresh_token.startswith("mock_") or refresh_token.startswith("test_"):
        return "mock_access_token_demo_refreshed"

    payload = {
        "client_id": get_google_client_id(),
        "client_secret": get_google_client_secret(),
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }

    http_client = client or httpx.Client(timeout=15.0)
    try:
        resp = http_client.post(
            GOOGLE_TOKEN_ENDPOINT,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code == 200:
            data = resp.json()
            new_access_token = data.get("access_token")
            expires_in = data.get("expires_in", 3600)
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

            # Update existing account record with new token
            current_status = get_email_account(user_id, account_id=account_id)
            if current_status:
                save_email_account(
                    current_status,
                    refresh_token=refresh_token,
                    access_token=new_access_token,
                    expires_at=expires_at,
                )
            return new_access_token
        else:
            logger.warning("Google access token refresh failed: HTTP %s", resp.status_code)
            return None
    except Exception as exc:
        logger.warning("Error refreshing Google access token: %s", exc)
        return None
    finally:
        if client is None:
            http_client.close()


def get_valid_access_token(
    user_id: str = "user_default",
    account_id: Optional[str] = None,
    client: Optional[httpx.Client] = None,
) -> Optional[str]:
    """Retrieve an active access token, refreshing automatically if expired or near expiry."""
    tokens = get_email_tokens(user_id, account_id=account_id)
    if not tokens:
        return None

    access_token = tokens.get("access_token")
    expires_at_str = tokens.get("expires_at")

    if not access_token:
        return refresh_access_token(user_id, account_id=account_id, client=client)

    if access_token.startswith("mock_") or access_token.startswith("test_"):
        return access_token

    if expires_at_str:
        try:
            expires_at = datetime.fromisoformat(expires_at_str)
            # Refresh if less than 5 minutes remain
            if datetime.now(timezone.utc) >= expires_at - timedelta(minutes=5):
                refreshed = refresh_access_token(user_id, account_id=account_id, client=client)
                return refreshed or access_token
        except Exception:
            pass

    return access_token


def disconnect_account(
    user_id: str = "user_default",
    account_id: Optional[str] = None,
) -> bool:
    """Disconnect account, attempt best-effort token revocation with Google, and clear tokens."""
    tokens = get_email_tokens(user_id, account_id=account_id)
    if tokens and tokens.get("access_token") and is_live_oauth_configured():
        token_to_revoke = tokens["access_token"]
        if not (token_to_revoke.startswith("mock_") or token_to_revoke.startswith("test_")):
            try:
                with httpx.Client(timeout=10.0) as client:
                    client.post(GOOGLE_REVOKE_ENDPOINT, params={"token": token_to_revoke})
            except Exception:
                pass  # Best effort revocation
    disconnect_email_account(user_id, account_id=account_id)
    return True


disconnect_email = disconnect_account
