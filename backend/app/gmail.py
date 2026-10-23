"""SubCan Gmail OAuth 2.0 and billing-email discovery module.

SECURITY INVARIANTS:
1. Tokens are stored ONLY in the local SQLite database, never in source code
   or environment variables at runtime.
2. PKCE (Proof Key for Code Exchange) is used for all authorization requests.
3. State parameter is a cryptographically random value validated on callback.
4. Tokens are never written to logs, responses, or error messages.
5. Gmail API calls are read-only (gmail.readonly scope only).
6. Email bodies are treated as untrusted data — no code execution from content.
7. Data minimisation: only billing-relevant metadata is extracted and stored.
8. A "connected" status is only reported after token verification succeeds.

GMAIL IS NOT LIVE until the user completes the Google OAuth consent flow with
valid credentials. This module degrades gracefully when credentials are absent.
"""

from __future__ import annotations

import base64
import email
import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.app.config import (
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
    GOOGLE_OAUTH_SCOPES,
    GOOGLE_REDIRECT_URI,
    oauth_credentials_configured,
)


# ==============================================================================
# OAuth State Store (in-process, thread-safe, TTL-limited)
# ==============================================================================

class _OAuthStateStore:
    """Thread-safe short-lived store for pending OAuth state/PKCE pairs."""

    _TTL_SECONDS = 600  # 10-minute window for OAuth round-trip

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._states: Dict[str, Dict[str, Any]] = {}

    def create(self, state: str, code_verifier: str, user_id: str) -> None:
        now = datetime.now(timezone.utc).timestamp()
        with self._lock:
            # Purge expired entries
            expired = [k for k, v in self._states.items()
                       if now - v["created_at"] > self._TTL_SECONDS]
            for k in expired:
                del self._states[k]
            self._states[state] = {
                "code_verifier": code_verifier,
                "user_id": user_id,
                "created_at": now,
            }

    def consume(self, state: str) -> Optional[Dict[str, Any]]:
        """Retrieve and delete a state entry. Returns None if expired or unknown."""
        now = datetime.now(timezone.utc).timestamp()
        with self._lock:
            entry = self._states.pop(state, None)
            if entry is None:
                return None
            if now - entry["created_at"] > self._TTL_SECONDS:
                return None  # Expired
            return entry


_STATE_STORE = _OAuthStateStore()


# ==============================================================================
# PKCE Helpers
# ==============================================================================

def _generate_pkce_pair() -> Tuple[str, str]:
    """Generate a (code_verifier, code_challenge) PKCE pair using S256 method."""
    code_verifier = secrets.token_urlsafe(96)  # 128 bytes → ~171 chars
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


# ==============================================================================
# Token Persistence (SQLite — never in env or logs)
# ==============================================================================

def _token_db_path() -> Path:
    """Resolve path to the main SubCan database for token storage."""
    from backend.app.database import _database_path
    return _database_path()


def _ensure_token_table() -> None:
    """Add oauth_tokens table to the existing database if not present."""
    path = _token_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS oauth_tokens (
                user_id     TEXT PRIMARY KEY,
                token_json  TEXT NOT NULL,
                email       TEXT,
                connected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at   TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()


def _save_token(user_id: str, token_data: Dict[str, Any], email_addr: Optional[str]) -> None:
    """Persist OAuth token blob. NEVER log token_data contents."""
    _ensure_token_table()
    path = _token_db_path()
    with sqlite3.connect(path) as conn:
        conn.execute("""
            INSERT INTO oauth_tokens(user_id, token_json, email, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                token_json=excluded.token_json,
                email=excluded.email,
                updated_at=CURRENT_TIMESTAMP
        """, (user_id, json.dumps(token_data), email_addr))
        conn.commit()


def _load_token(user_id: str) -> Optional[Tuple[Dict[str, Any], Optional[str]]]:
    """Load token data for a user. Returns (token_dict, email) or None."""
    try:
        _ensure_token_table()
        path = _token_db_path()
        with sqlite3.connect(path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT token_json, email FROM oauth_tokens WHERE user_id = ?",
                (user_id,)
            ).fetchone()
        if row is None:
            return None
        return json.loads(row["token_json"]), row["email"]
    except Exception:
        return None


def _delete_token(user_id: str) -> None:
    """Remove stored tokens for a user (disconnect / revoke)."""
    try:
        _ensure_token_table()
        path = _token_db_path()
        with sqlite3.connect(path) as conn:
            conn.execute("DELETE FROM oauth_tokens WHERE user_id = ?", (user_id,))
            conn.commit()
    except Exception:
        pass


# ==============================================================================
# OAuth Flow
# ==============================================================================

class OAuthNotConfiguredError(RuntimeError):
    """Raised when Google OAuth credentials are absent from environment."""
    pass


class OAuthStateError(ValueError):
    """Raised when OAuth callback state is invalid, missing, or expired."""
    pass


class OAuthTokenError(RuntimeError):
    """Raised when token exchange or refresh fails."""
    pass


def build_authorization_url(user_id: str) -> str:
    """Build the Google authorization URL with PKCE and a secure random state.

    Raises OAuthNotConfiguredError if credentials are not set.
    Returns the URL the user should be redirected to.
    """
    if not oauth_credentials_configured():
        raise OAuthNotConfiguredError(
            "Google OAuth credentials are not configured. "
            "Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env."
        )

    from google_auth_oauthlib.flow import Flow

    code_verifier, code_challenge = _generate_pkce_pair()
    state = secrets.token_urlsafe(32)

    _STATE_STORE.create(state=state, code_verifier=code_verifier, user_id=user_id)

    flow = Flow.from_client_config(
        client_config={
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [GOOGLE_REDIRECT_URI],
            }
        },
        scopes=GOOGLE_OAUTH_SCOPES,
        redirect_uri=GOOGLE_REDIRECT_URI,
    )

    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        state=state,
        code_challenge=code_challenge,
        code_challenge_method="S256",
        prompt="consent",  # Force refresh_token on re-auth
    )
    return auth_url


def handle_oauth_callback(
    code: str,
    state: str,
    error: Optional[str] = None,
) -> Dict[str, Any]:
    """Handle the OAuth callback, validate state, exchange code for tokens.

    Returns a safe status dict (NO token values).
    Raises OAuthStateError or OAuthTokenError on failure.
    """
    if error:
        raise OAuthStateError(f"Google authorization denied: {error}")

    entry = _STATE_STORE.consume(state)
    if entry is None:
        raise OAuthStateError(
            "OAuth state is invalid, expired, or already used. "
            "Please restart the authorization flow."
        )

    user_id: str = entry["user_id"]
    code_verifier: str = entry["code_verifier"]

    if not oauth_credentials_configured():
        raise OAuthNotConfiguredError("OAuth credentials not configured.")

    try:
        from google_auth_oauthlib.flow import Flow
        from googleapiclient.discovery import build as google_build

        flow = Flow.from_client_config(
            client_config={
                "web": {
                    "client_id": GOOGLE_CLIENT_ID,
                    "client_secret": GOOGLE_CLIENT_SECRET,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": [GOOGLE_REDIRECT_URI],
                }
            },
            scopes=GOOGLE_OAUTH_SCOPES,
            redirect_uri=GOOGLE_REDIRECT_URI,
            state=state,
        )

        # Exchange code for tokens (includes PKCE verifier)
        flow.fetch_token(
            code=code,
            code_verifier=code_verifier,
        )
        credentials = flow.credentials

        # Verify the token works by fetching the user's email address
        gmail_user_info = google_build(
            "oauth2", "v2", credentials=credentials
        ).userinfo().get().execute()
        gmail_email = gmail_user_info.get("email")

        token_data = {
            "token": credentials.token,
            "refresh_token": credentials.refresh_token,
            "token_uri": credentials.token_uri,
            "client_id": credentials.client_id,
            "client_secret": credentials.client_secret,
            "scopes": list(credentials.scopes or []),
            "expiry": credentials.expiry.isoformat() if credentials.expiry else None,
        }
        _save_token(user_id, token_data, gmail_email)

        return {
            "status": "connected",
            "email": gmail_email,
            "user_id": user_id,
            "scopes": list(credentials.scopes or []),
        }

    except Exception as exc:
        # Do NOT include raw exception message which might contain token fragments
        raise OAuthTokenError(
            f"Token exchange failed. Check credentials and try again. "
            f"({type(exc).__name__})"
        ) from exc


def get_connection_status(user_id: str) -> Dict[str, Any]:
    """Return the OAuth connection status for a user.

    Returns a safe status dict — never includes token values.
    """
    if not oauth_credentials_configured():
        return {
            "connected": False,
            "email": None,
            "reason": "Google OAuth credentials not configured in .env",
            "oauth_ready": False,
        }

    result = _load_token(user_id)
    if result is None:
        return {
            "connected": False,
            "email": None,
            "reason": "Not authorized. Use /api/email/oauth/start to connect Gmail.",
            "oauth_ready": True,
        }

    token_data, email_addr = result

    # Verify the credentials are still usable
    try:
        credentials = _credentials_from_token_data(token_data)
        if credentials is None:
            _delete_token(user_id)
            return {
                "connected": False,
                "email": email_addr,
                "reason": "Stored credentials are invalid. Please reconnect.",
                "oauth_ready": True,
            }

        # Check expiry and attempt refresh if needed
        from google.auth.transport.requests import Request
        if credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
            # Save refreshed token
            refreshed_data = {
                **token_data,
                "token": credentials.token,
                "expiry": credentials.expiry.isoformat() if credentials.expiry else None,
            }
            _save_token(user_id, refreshed_data, email_addr)

        return {
            "connected": True,
            "email": email_addr,
            "reason": None,
            "oauth_ready": True,
        }
    except Exception as exc:
        return {
            "connected": False,
            "email": email_addr,
            "reason": f"Credentials may be expired or revoked ({type(exc).__name__}). Please reconnect.",
            "oauth_ready": True,
        }


def revoke_connection(user_id: str) -> bool:
    """Revoke stored OAuth tokens and remove from database.

    Returns True if tokens were present and removed, False if no tokens found.
    """
    result = _load_token(user_id)
    if result is None:
        return False

    token_data, _ = result
    # Best-effort revoke with Google
    try:
        import requests as req_lib
        req_lib.post(
            "https://oauth2.googleapis.com/revoke",
            params={"token": token_data.get("token")},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=5,
        )
    except Exception:
        pass  # Revocation is best-effort; always delete locally

    _delete_token(user_id)
    return True


# ==============================================================================
# Credentials Helper
# ==============================================================================

def _credentials_from_token_data(token_data: Dict[str, Any]):
    """Reconstruct a google.oauth2.credentials.Credentials object from stored data."""
    try:
        from google.oauth2.credentials import Credentials
        expiry = None
        if token_data.get("expiry"):
            try:
                expiry = datetime.fromisoformat(token_data["expiry"])
            except ValueError:
                pass

        return Credentials(
            token=token_data.get("token"),
            refresh_token=token_data.get("refresh_token"),
            token_uri=token_data.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=token_data.get("client_id"),
            client_secret=token_data.get("client_secret"),
            scopes=token_data.get("scopes"),
            expiry=expiry,
        )
    except Exception:
        return None


def _get_valid_credentials(user_id: str):
    """Get validated, auto-refreshed credentials for a user or raise."""
    result = _load_token(user_id)
    if result is None:
        raise OAuthTokenError("No credentials found. Connect Gmail first.")

    token_data, email_addr = result
    credentials = _credentials_from_token_data(token_data)
    if credentials is None:
        raise OAuthTokenError("Stored credentials are invalid.")

    if credentials.expired and credentials.refresh_token:
        from google.auth.transport.requests import Request
        credentials.refresh(Request())
        refreshed_data = {
            **token_data,
            "token": credentials.token,
            "expiry": credentials.expiry.isoformat() if credentials.expiry else None,
        }
        _save_token(user_id, refreshed_data, email_addr)

    return credentials


# ==============================================================================
# Billing Email Discovery
# ==============================================================================

# Billing-related subject/sender keywords (case-insensitive)
_BILLING_SUBJECT_PATTERNS = [
    r"receipt",
    r"invoice",
    r"payment\s+confirm",
    r"billing\s+statement",
    r"subscription\s+renewal",
    r"your\s+order",
    r"renewal\s+notice",
    r"charge\s+from",
    r"payment\s+receipt",
    r"subscription\s+confirmed",
]
_BILLING_REGEX = re.compile(
    "|".join(_BILLING_SUBJECT_PATTERNS), re.IGNORECASE
)

# Provider name extraction from common subscription services
_KNOWN_PROVIDERS = [
    "Netflix", "Spotify", "Amazon Prime", "Disney+", "Hulu", "Apple",
    "YouTube Premium", "Google", "Microsoft", "Adobe", "Dropbox",
    "LinkedIn", "Slack", "Zoom", "GitHub", "Canva", "Notion",
    "Figma", "Duolingo", "Headspace", "Calm", "Peloton",
    "HBO Max", "Paramount+", "Peacock", "Crunchyroll",
    "PlayStation", "Xbox", "Nintendo", "Steam", "Epic Games",
    "Audible", "Kindle Unlimited", "iCloud",
]

# Currency amount pattern
_AMOUNT_PATTERN = re.compile(
    r"(?:USD|GBP|EUR|INR|CAD|AUD)?\s*[\$\£\€\₹]?\s*(\d{1,6}(?:[.,]\d{2,3})*)",
    re.IGNORECASE,
)

# Date pattern (ISO or common formats)
_DATE_PATTERN = re.compile(
    r"\b(\d{4}-\d{2}-\d{2}|\d{1,2}[\/\-]\d{1,2}[\/\-]\d{2,4})\b"
)


def _decode_message_body(payload: Dict[str, Any]) -> str:
    """Recursively decode a Gmail message payload to plain text."""
    mime_type = payload.get("mimeType", "")
    body = payload.get("body", {})
    data = body.get("data", "")

    if mime_type == "text/plain" and data:
        try:
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
        except Exception:
            return ""

    # Recurse into multipart
    parts = payload.get("parts", [])
    for part in parts:
        text = _decode_message_body(part)
        if text:
            return text
    return ""


def _extract_provider(subject: str, sender: str, body_text: str) -> Optional[str]:
    """Attempt to identify the subscription provider from message metadata."""
    combined = f"{subject} {sender} {body_text[:500]}"
    for provider in _KNOWN_PROVIDERS:
        if re.search(re.escape(provider), combined, re.IGNORECASE):
            return provider
    # Fallback: extract domain from sender
    match = re.search(r"@([\w\-]+)\.", sender)
    if match:
        domain = match.group(1)
        return domain.capitalize() if len(domain) > 2 else None
    return None


def _extract_amount(body_text: str) -> Optional[float]:
    """Extract the first plausible billing amount from body text."""
    matches = _AMOUNT_PATTERN.findall(body_text[:2000])
    for m in matches:
        try:
            cleaned = m.replace(",", "")
            val = float(cleaned)
            if 0.01 <= val <= 99999:  # Sanity bounds
                return round(val, 2)
        except ValueError:
            continue
    return None


def _extract_date(body_text: str) -> Optional[str]:
    """Extract the first recognisable date from body text (ISO format returned)."""
    matches = _DATE_PATTERN.findall(body_text[:2000])
    for m in matches:
        try:
            # Try ISO first
            dt = datetime.strptime(m, "%Y-%m-%d")
            return dt.date().isoformat()
        except ValueError:
            pass
        # Try MM/DD/YYYY or DD/MM/YYYY
        for fmt in ("%m/%d/%Y", "%d/%m/%Y", "%m-%d-%Y"):
            try:
                dt = datetime.strptime(m, fmt)
                return dt.date().isoformat()
            except ValueError:
                pass
    return None


def discover_billing_emails(
    user_id: str,
    max_messages: int = 100,
    page_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Discover billing emails from the user's Gmail account.

    Uses gmail.readonly scope — read-only, no modification.
    Treats all email content as untrusted data.

    Returns:
        {
            "candidates": [...],
            "next_page_token": str | None,
            "messages_scanned": int,
            "synced_at": ISO timestamp,
            "data_source": "gmail_live",
        }
    """
    credentials = _get_valid_credentials(user_id)

    from googleapiclient.discovery import build as google_build

    service = google_build("gmail", "v1", credentials=credentials)

    # Build bounded search query — billing keywords only
    query = (
        "subject:(receipt OR invoice OR billing OR renewal OR payment OR subscription) "
        "category:primary OR category:updates"
    )

    list_params: Dict[str, Any] = {
        "userId": "me",
        "q": query,
        "maxResults": min(max_messages, 100),  # Gmail API cap per request
    }
    if page_token:
        list_params["pageToken"] = page_token

    try:
        result = service.users().messages().list(**list_params).execute()
    except Exception as exc:
        raise OAuthTokenError(
            f"Gmail API list failed ({type(exc).__name__}). "
            "Check credentials and quota."
        ) from exc

    messages = result.get("messages", [])
    next_page_token = result.get("nextPageToken")
    candidates = []
    seen_ids: set = set()

    for msg_ref in messages:
        msg_id = msg_ref["id"]
        if msg_id in seen_ids:
            continue
        seen_ids.add(msg_id)

        try:
            msg = service.users().messages().get(
                userId="me",
                id=msg_id,
                format="full",
            ).execute()
        except Exception:
            continue  # Skip individual message failures

        headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
        subject = headers.get("subject", "")
        sender = headers.get("from", "")
        date_header = headers.get("date", "")

        # Subject/sender filter — only billing-relevant messages
        if not _BILLING_REGEX.search(subject) and not _BILLING_REGEX.search(sender):
            continue

        # Decode body (limited — not storing full emails)
        body_text = _decode_message_body(msg.get("payload", {}))
        body_preview = body_text[:500].strip()  # Store only excerpt

        provider = _extract_provider(subject, sender, body_text)
        amount = _extract_amount(body_text)
        billing_date = _extract_date(body_text) or date_header[:10] or None

        # Stable deduplication key: message_id is Gmail-unique
        candidate = {
            "source": "gmail",
            "message_id": msg_id,
            "thread_id": msg.get("threadId"),
            "subject": subject[:200],          # Truncate for storage
            "sender": sender[:200],
            "date_header": date_header[:50],
            "provider_name": provider,
            "amount": amount,
            "currency": None,                  # TODO: extract currency code
            "billing_date": billing_date,
            "body_preview": body_preview,      # Excerpt only — not full body
            "extracted_at": datetime.now(timezone.utc).isoformat(),
            "user_id": user_id,
        }
        candidates.append(candidate)

    return {
        "candidates": candidates,
        "next_page_token": next_page_token,
        "messages_scanned": len(messages),
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "data_source": "gmail_live",
    }
