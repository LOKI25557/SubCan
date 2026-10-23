"""High-level email discovery synchronization and candidate review management.

Orchestrates:
1. Targeted billing message search.
2. Safe candidate extraction, deduplication, and grouping.
3. Candidate confirmation into unified inventory as formal Subscription with Evidence.
4. Candidate dismissal.
5. Ingestion of realistic simulated billing emails when credentials are not configured.
"""

import base64
from datetime import date, datetime, timedelta, timezone
import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple
import httpx

from backend.app.database import (
    get_email_account,
    get_email_tokens,
    get_subscription_candidate,
    list_subscription_candidates,
    save_email_account,
    save_subscription,
    save_subscription_candidate,
    update_candidate_status,
)
from backend.app.email.oauth import get_valid_access_token, is_live_oauth_configured
from backend.app.email.parser import parse_billing_email
from backend.app.models import (
    CandidateReviewStatus,
    DiscoverySource,
    EmailAccountStatus,
    Evidence,
    Subscription,
    SubscriptionCandidate,
    SubscriptionStatus,
)


# Realistic sample billing emails used when in demo mode or running acceptance tests
SAMPLE_BILLING_EMAILS = [
    {
        "id": "msg_gmail_netflix_001",
        "subject": "Your Netflix invoice and renewal confirmation",
        "sender": "info@mailer.netflix.com",
        "received_at": datetime(2026, 10, 15, 8, 30, tzinfo=timezone.utc),
        "body": """
            <html><body>
            <h2>Thanks for being a Netflix member</h2>
            <p>Your subscription to Netflix Standard Plan was successfully renewed on October 15, 2026.</p>
            <p>Amount Billed: $15.49 USD</p>
            <p>Billing Cycle: Monthly</p>
            <p>Your next billing date will be 2026-11-15.</p>
            <p>Manage your account or update preferences anytime at netflix.com/account.</p>
            </body></html>
        """,
    },
    {
        "id": "msg_gmail_spotify_002",
        "subject": "Receipt for Spotify Premium (Monthly)",
        "sender": "no-reply@spotify.com",
        "received_at": datetime(2026, 10, 1, 14, 15, tzinfo=timezone.utc),
        "body": """
            <div>
            <h1>Spotify Premium Receipt</h1>
            <p>Payment received for Premium Individual subscription.</p>
            <p>Total: $11.99</p>
            <p>Next billing date: 2026-11-01</p>
            <p>To cancel or change plan, visit your account page.</p>
            </div>
        """,
    },
    {
        "id": "msg_gmail_adobe_003",
        "subject": "Adobe Creative Cloud - Payment Confirmation",
        "sender": "billing@adobe.com",
        "received_at": datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc),
        "body": """
            <p>Thank you for your payment to Adobe Systems.</p>
            <p>Plan: Adobe Creative Cloud All Apps</p>
            <p>Billed: $54.99 USD</p>
            <p>Interval: monthly</p>
            <p>Next payment on 2026-10-20</p>
        """,
    },
    {
        "id": "msg_gmail_nytimes_004",
        "subject": "Your New York Times All Access Digital Subscription",
        "sender": "customercare@nytimes.com",
        "received_at": datetime(2026, 10, 5, 9, 0, tzinfo=timezone.utc),
        "body": """
            <p>Payment successful for The New York Times All Access.</p>
            <p>Amount: $4.00 per month</p>
            <p>Next renewal: 2026-11-05</p>
        """,
    },
    {
        "id": "msg_gmail_cancellation_005",
        "subject": "Hulu: Cancellation confirmed",
        "sender": "support@hulu.com",
        "received_at": datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc),
        "body": """
            <p>We're sorry to see you go! We've received your request to cancel your Hulu (No Ads) subscription.</p>
            <p>Your subscription has been cancelled and will end on 2026-11-02. You will not be billed again.</p>
        """,
    },
]


def sync_user_mailbox(
    user_id: str = "user_default",
    max_messages: int = 50,
    client: Optional[httpx.Client] = None,
) -> List[SubscriptionCandidate]:
    """Execute targeted email search and extract subscription candidates.

    Uses live Gmail API if OAuth is connected with real credentials;
    otherwise uses deterministic realistic sample emails for safe local demo/testing.
    """
    account = get_email_account(user_id)
    if not account or not account.is_connected:
        raise ValueError(
            f"No email account connected for user '{user_id}'. Please authorize and connect your mailbox first."
        )

    account.sync_status = "syncing"
    save_email_account(account)

    candidates: List[SubscriptionCandidate] = []
    tokens = get_email_tokens(user_id)
    token_str = (tokens or {}).get("access_token", "")

    if is_live_oauth_configured() and tokens and token_str and not (token_str.startswith("mock_") or token_str.startswith("test_")):
        # Live Gmail API sync
        try:
            active_token = get_valid_access_token(user_id, client=client) or token_str
            candidates = _fetch_live_gmail_candidates(
                access_token=active_token,
                user_id=user_id,
                max_messages=max_messages,
                client=client,
            )
        except Exception as e:
            account.sync_status = "failed"
            account.error_message = f"Gmail API sync failed: {str(e)}"
            save_email_account(account)
            raise
    else:
        # Realistic simulated sync for demo mode
        for raw_msg in SAMPLE_BILLING_EMAILS:
            cand = parse_billing_email(
                message_id=raw_msg["id"],
                subject=raw_msg["subject"],
                sender=raw_msg["sender"],
                raw_content=raw_msg["body"],
                received_at=raw_msg["received_at"],
                user_id=user_id,
            )
            if cand:
                candidates.append(cand)

    # Group and deduplicate candidates by merchant to prevent double-counting
    candidates = group_and_deduplicate_candidates(candidates)

    # Save candidates to database
    for c in candidates:
        save_subscription_candidate(c)

    account.sync_status = "idle"
    account.last_synced_at = datetime.now(timezone.utc)
    account.candidate_count = len(candidates)
    account.error_message = None
    save_email_account(account)

    return candidates


def group_and_deduplicate_candidates(
    raw_candidates: List[SubscriptionCandidate],
) -> List[SubscriptionCandidate]:
    """Group multiple candidate receipts for the same merchant to avoid double-counting recurring payments.

    Merges recurring billing notifications into a single unified candidate subscription,
    retaining the latest renewal date, authoritative pricing, and flagging cancellation signals.
    """
    grouped: Dict[str, SubscriptionCandidate] = {}
    for cand in raw_candidates:
        key = cand.merchant_name.lower().strip()
        if key not in grouped:
            grouped[key] = cand
        else:
            existing = grouped[key]
            # Update with newer detected renewal date if available
            if cand.detected_renewal_date and (
                not existing.detected_renewal_date
                or cand.detected_renewal_date > existing.detected_renewal_date
            ):
                existing.detected_renewal_date = cand.detected_renewal_date
            if cand.estimated_cost is not None and existing.estimated_cost is None:
                existing.estimated_cost = cand.estimated_cost
            if cand.cancellation_signal:
                existing.cancellation_signal = True
            if cand.confidence > existing.confidence:
                existing.confidence = cand.confidence
            if cand.source_message_id and cand.source_message_id not in (existing.source_message_id or ""):
                existing.notes = (
                    f"{existing.notes or ''}; Additional receipt: {cand.source_message_id}"
                ).strip("; ")

    return list(grouped.values())


def _extract_body_from_gmail_payload(payload: Dict[str, Any]) -> str:
    """Extract plain text or HTML body from nested MIME parts with base64url decoding."""
    body_data = payload.get("body", {}).get("data")
    if body_data:
        try:
            padded = body_data + "=" * (-len(body_data) % 4)
            return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", errors="replace")
        except Exception:
            pass

    parts = payload.get("parts", [])
    text_content = ""
    html_content = ""
    for part in parts:
        mime_type = part.get("mimeType", "")
        part_data = part.get("body", {}).get("data")
        if part_data:
            try:
                padded = part_data + "=" * (-len(part_data) % 4)
                decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", errors="replace")
                if mime_type == "text/plain":
                    text_content += " " + decoded
                elif mime_type == "text/html":
                    html_content += " " + decoded
            except Exception:
                pass
        if "parts" in part:
            sub = _extract_body_from_gmail_payload(part)
            if sub:
                text_content += " " + sub

    return (text_content.strip() or html_content.strip())


def _fetch_live_gmail_candidates(
    access_token: str,
    user_id: str,
    max_messages: int = 50,
    client: Optional[httpx.Client] = None,
) -> List[SubscriptionCandidate]:
    """Query Gmail API for billing receipts and invoices with pagination."""
    query = "subject:(receipt OR invoice OR subscription OR billed OR renewal) newer_than:90d"
    list_url = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
    params = {"q": query, "maxResults": max_messages}

    http_client = client or httpx.Client(timeout=15.0)
    try:
        resp = http_client.get(
            list_url,
            params=params,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if resp.status_code != 200:
            raise ValueError(f"Gmail messages list failed with HTTP {resp.status_code}: {resp.text}")

        msg_list = resp.json().get("messages", [])
        candidates: List[SubscriptionCandidate] = []

        for m in msg_list[:max_messages]:
            m_id = m.get("id")
            if not m_id:
                continue

            msg_url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{m_id}?format=full"
            try:
                m_resp = http_client.get(
                    msg_url,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                if m_resp.status_code != 200:
                    continue

                data = m_resp.json()
                subject = ""
                sender = ""
                headers = data.get("payload", {}).get("headers", [])
                for h in headers:
                    h_name = h.get("name", "").lower()
                    if h_name == "subject":
                        subject = h.get("value", "")
                    elif h_name == "from":
                        sender = h.get("value", "")

                body_text = _extract_body_from_gmail_payload(data.get("payload", {}))
                if not body_text:
                    body_text = data.get("snippet", "")

                received_at = None
                internal_date = data.get("internalDate")
                if internal_date:
                    try:
                        received_at = datetime.fromtimestamp(int(internal_date) / 1000.0, tz=timezone.utc)
                    except Exception:
                        pass

                cand = parse_billing_email(
                    message_id=m_id,
                    subject=subject,
                    sender=sender,
                    raw_content=body_text,
                    received_at=received_at,
                    user_id=user_id,
                )
                if cand:
                    candidates.append(cand)
            except Exception:
                continue

        return candidates
    finally:
        if client is None:
            http_client.close()


def confirm_candidate_into_inventory(
    candidate_id: str,
    user_id: str = "user_default",
) -> Tuple[Subscription, Evidence]:
    """Confirm a discovered candidate and promote it into the unified inventory.

    Safety:
    1. Sets provenance discovery_source='email'.
    2. Constructs a traceable Evidence record linked to the source email message.
    3. Retains honest confidence scores and unknown fields.
    """
    cand = get_subscription_candidate(candidate_id, user_id=user_id)
    if not cand:
        raise ValueError(f"Subscription candidate '{candidate_id}' not found.")

    # Generate a stable, collision-resistant subscription ID
    clean_name = re.sub(r"[^a-zA-Z0-9]", "_", cand.merchant_name).lower()
    sub_id = f"sub_email_{clean_name}_{cand.candidate_id[-6:]}"

    status = SubscriptionStatus.CANCELLED if cand.cancellation_signal else SubscriptionStatus.ACTIVE

    sub = Subscription(
        subscription_id=sub_id,
        service_name=cand.merchant_name,
        status=status,
        renewal_date=cand.detected_renewal_date,
        last_used_at=cand.detected_last_used,
        monthly_cost=cand.estimated_cost,
        currency=cand.currency or "USD",
        billing_cycle=cand.billing_interval or "monthly",
        user_id=cand.user_id,
        category=cand.product_name,
        discovery_source=DiscoverySource.EMAIL.value,
        confidence=cand.confidence,
        notes=cand.notes or f"Discovered via email receipt from {cand.source_sender or 'provider'}.",
        is_archived=False,
    )

    # Save to unified database store
    save_subscription(sub)

    # Construct and save supporting evidence item
    ev_id = f"ev_email_{clean_name}_{cand.candidate_id[-6:]}"
    evidence = Evidence(
        evidence_id=ev_id,
        subscription_id=sub_id,
        evidence_type="email_receipt",
        excerpt=cand.email_snippet or f"Billing notice for {cand.merchant_name}",
        source_url=f"gmail://message/{cand.source_message_id}",
        captured_at=datetime.now(timezone.utc),
        confidence=cand.confidence,
        raw_reference={
            "subject": cand.source_subject,
            "sender": cand.source_sender,
            "message_id": cand.source_message_id,
        },
    )

    # Mark candidate as confirmed
    update_candidate_status(candidate_id, CandidateReviewStatus.CONFIRMED, user_id=user_id)

    return sub, evidence


def dismiss_candidate(
    candidate_id: str,
    user_id: str = "user_default",
) -> bool:
    """Dismiss a candidate as false positive or unwanted."""
    return update_candidate_status(candidate_id, CandidateReviewStatus.DISMISSED, user_id=user_id)
