"""High-level email discovery synchronization and candidate review management.

Orchestrates:
1. Targeted billing message search.
2. Safe candidate extraction, deduplication, and grouping.
3. Candidate confirmation into unified inventory as formal Subscription with Evidence.
4. Candidate dismissal.
5. Ingestion of realistic simulated billing emails when credentials are not configured.
6. Honest, evidence-based sync status accounting (retrieved, skipped, parsed, duplicates, failed).
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
    list_subscriptions_from_db,
    save_email_account,
    save_subscription,
    save_subscription_candidate,
    update_candidate_status,
)
from backend.app.email.oauth import get_valid_access_token, is_live_oauth_configured
from backend.app.email.parser import evaluate_billing_email, parse_billing_email, sanitize_email_content
from backend.app.models import (
    CandidateReviewStatus,
    DiscoverySource,
    EmailAccountStatus,
    Evidence,
    Subscription,
    SubscriptionCandidate,
    SubscriptionStatus,
)

logger = logging.getLogger("subcan.email.service")


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
    account_id: Optional[str] = None,
    max_messages: int = 50,
    client: Optional[httpx.Client] = None,
) -> List[SubscriptionCandidate]:
    """Execute targeted email search and extract subscription candidates.

    Uses live Gmail API if OAuth is connected with real credentials;
    otherwise uses deterministic realistic sample emails for safe local demo/testing.
    """
    account = get_email_account(user_id, account_id=account_id)
    if not account or not account.is_connected:
        raise ValueError(
            f"No email account connected for user '{user_id}'. Please authorize and connect your mailbox first."
        )

    account.sync_status = "syncing"
    save_email_account(account)

    raw_candidates: List[SubscriptionCandidate] = []
    tokens = get_email_tokens(user_id, account_id=account.account_id)
    token_str = (tokens or {}).get("access_token", "")

    metrics: Dict[str, Any] = {
        "messages_discovered": 0,
        "messages_retrieved": 0,
        "messages_parsed": 0,
        "candidates_created": 0,
        "messages_skipped": 0,
        "messages_insufficient_evidence": 0,
        "duplicates_skipped": 0,
        "parsing_failures": 0,
        "is_partial_scan": False,
        "scan_limit": max_messages,
        "scan_date_range": None,
        "skip_reasons": {},
        "failure_reasons": {},
    }

    is_demo_account = (
        (account.email_address and "@subcan-demo.com" in account.email_address)
        or user_id.startswith("user_demo")
        or user_id.startswith("user_sync_")
        or user_id.startswith("test_")
    )

    if is_live_oauth_configured() and tokens and token_str and not (token_str.startswith("mock_") or token_str.startswith("test_")):
        # Live Gmail API sync
        try:
            active_token = get_valid_access_token(user_id, account_id=account.account_id, client=client) or token_str
            raw_candidates = _fetch_live_gmail_candidates(
                access_token=active_token,
                user_id=user_id,
                account_id=account.account_id,
                account_email=account.email_address,
                max_messages=max_messages,
                client=client,
                metrics=metrics,
            )
        except Exception as e:
            account.sync_status = "failed"
            account.error_message = f"Gmail API sync failed: {str(e)}"
            save_email_account(account)
            raise
    elif is_demo_account:
        # Realistic simulated sync ONLY for explicit demo/test accounts
        metrics["messages_discovered"] = len(SAMPLE_BILLING_EMAILS)
        metrics["messages_retrieved"] = len(SAMPLE_BILLING_EMAILS)
        metrics["scan_date_range"] = "2026-10-01 to 2026-10-24"
        for raw_msg in SAMPLE_BILLING_EMAILS:
            eval_result = evaluate_billing_email(
                message_id=raw_msg["id"],
                subject=raw_msg["subject"],
                sender=raw_msg["sender"],
                raw_content=raw_msg["body"],
                received_at=raw_msg["received_at"],
                user_id=user_id,
                account_id=account.account_id,
                account_email=account.email_address,
            )
            if eval_result.status == "candidate" and eval_result.candidate:
                cand = eval_result.candidate
                cand.is_demo = True
                raw_candidates.append(cand)
                metrics["candidates_created"] += 1
                metrics["messages_parsed"] += 1
            elif eval_result.status == "skipped":
                metrics["messages_parsed"] += 1
                if eval_result.reason == "insufficient_evidence":
                    metrics["messages_insufficient_evidence"] += 1
                else:
                    metrics["messages_skipped"] += 1
                r_key = eval_result.reason or "skipped"
                metrics.setdefault("skip_reasons", {})[r_key] = metrics.get("skip_reasons", {}).get(r_key, 0) + 1
            else:
                metrics["parsing_failures"] += 1
                r_key = eval_result.reason or "failed"
                metrics.setdefault("failure_reasons", {})[r_key] = metrics.get("failure_reasons", {}).get(r_key, 0) + 1
    else:
        account.sync_status = "failed"
        account.error_message = "Live Gmail OAuth is not configured or token expired. Please connect your Gmail account."
        save_email_account(account)
        raise ValueError("Live Gmail OAuth is not configured or token expired. Please connect your Gmail account.")

    # Step 1: In-batch deduplication (merges multiple receipts for the same merchant in this sync)
    in_batch_deduped = group_and_deduplicate_candidates(raw_candidates)
    in_batch_duplicates = max(0, len(raw_candidates) - len(in_batch_deduped))

    # Step 2: Cross-sync deduplication against existing database candidates and inventory
    existing_candidates = list_subscription_candidates(user_id=user_id, account_id=account.account_id)
    existing_by_merchant: Dict[str, SubscriptionCandidate] = {
        canonical_merchant_name(c.merchant_name).lower().strip(): c for c in existing_candidates
    }
    existing_by_msg_id: Dict[str, SubscriptionCandidate] = {
        c.source_message_id: c for c in existing_candidates if c.source_message_id
    }

    existing_subs = list_subscriptions_from_db(user_id=user_id, include_archived=False)
    existing_subs_by_name = {
        canonical_merchant_name(s.service_name).lower().strip(): s for s in existing_subs
    }

    final_candidates: List[SubscriptionCandidate] = []
    db_duplicates = 0

    for cand in in_batch_deduped:
        merchant_key = canonical_merchant_name(cand.merchant_name).lower().strip()
        msg_id = cand.source_message_id

        # Match existing candidate by message id or canonical merchant key
        db_cand = None
        if msg_id and msg_id in existing_by_msg_id:
            db_cand = existing_by_msg_id[msg_id]
        elif merchant_key in existing_by_merchant:
            db_cand = existing_by_merchant[merchant_key]

        if db_cand:
            db_duplicates += 1
            if db_cand.review_status == CandidateReviewStatus.PENDING:
                # Merge upgraded data into existing pending candidate
                if cand.detected_renewal_date and (
                    not db_cand.detected_renewal_date or cand.detected_renewal_date > db_cand.detected_renewal_date
                ):
                    db_cand.detected_renewal_date = cand.detected_renewal_date
                if cand.estimated_cost is not None and (db_cand.estimated_cost is None or db_cand.estimated_cost == 0):
                    db_cand.estimated_cost = cand.estimated_cost
                    db_cand.currency = cand.currency
                    if cand.billing_interval:
                        db_cand.billing_interval = cand.billing_interval
                if cand.product_name and (not db_cand.product_name or db_cand.product_name.lower() in ("subscription", "membership", "cloud_storage", "music", "security", "general", "-----")):
                    db_cand.product_name = cand.product_name
                if cand.merchant_name and db_cand.merchant_name != cand.merchant_name:
                    db_cand.merchant_name = cand.merchant_name
                if cand.cancellation_signal:
                    db_cand.cancellation_signal = True
                if cand.lifecycle_status:
                    db_cand.lifecycle_status = cand.lifecycle_status
                if cand.evidence_category:
                    db_cand.evidence_category = cand.evidence_category
                if cand.trial_end_date:
                    db_cand.trial_end_date = cand.trial_end_date
                if cand.purchase_date:
                    db_cand.purchase_date = cand.purchase_date
                if cand.cancellation_date:
                    db_cand.cancellation_date = cand.cancellation_date
                if cand.last_payment_date:
                    db_cand.last_payment_date = cand.last_payment_date
                if cand.confidence > db_cand.confidence:
                    db_cand.confidence = cand.confidence
                save_subscription_candidate(db_cand)
                final_candidates.append(db_cand)
            # If confirmed or dismissed, keep user's decision!
            continue

        # Case C: Subscription already active in inventory
        if merchant_key in existing_subs_by_name:
            db_duplicates += 1
            continue

        # Case D: Genuinely new candidate
        save_subscription_candidate(cand)
        final_candidates.append(cand)

    metrics["duplicates_skipped"] = in_batch_duplicates + db_duplicates

    # Update account status record
    pending_count = len(list_subscription_candidates(user_id=user_id, review_status="pending"))
    account.sync_status = "idle"
    account.last_synced_at = datetime.now(timezone.utc)
    account.candidate_count = pending_count
    account.messages_discovered = metrics.get("messages_discovered", metrics["messages_retrieved"])
    account.messages_retrieved = metrics["messages_retrieved"]
    account.messages_parsed = metrics.get("messages_parsed", 0)
    account.messages_skipped = metrics["messages_skipped"]
    account.messages_insufficient_evidence = metrics.get("messages_insufficient_evidence", 0)
    account.duplicates_skipped = metrics["duplicates_skipped"]
    account.parsing_failures = metrics["parsing_failures"]
    account.is_partial_scan = metrics.get("is_partial_scan", False)
    account.scan_limit = metrics.get("scan_limit", max_messages)
    account.scan_date_range = metrics.get("scan_date_range")
    account.skip_reasons = metrics.get("skip_reasons", {})
    account.failure_reasons = metrics.get("failure_reasons", {})
    account.error_message = None
    save_email_account(account)

    return final_candidates


MERCHANT_CANONICAL_MAP = {
    "google workspace": "Google One",
    "google ai plus": "Google One",
    "google": "Google One",
    "microsoft": "Microsoft 365",
    "microsoft corporation": "Microsoft 365",
    "chatgpt": "OpenAI ChatGPT Plus",
    "chatgpt plus": "OpenAI ChatGPT Plus",
    "openai": "OpenAI ChatGPT Plus",
    "spotify india": "Spotify",
    "spotify premium": "Spotify",
}


def canonical_merchant_name(name: str) -> str:
    cleaned = (name or "").lower().strip()
    return MERCHANT_CANONICAL_MAP.get(cleaned, (name or "").strip())


def group_and_deduplicate_candidates(
    raw_candidates: List[SubscriptionCandidate],
) -> List[SubscriptionCandidate]:
    """Group multiple candidate receipts for the same merchant to avoid double-counting recurring payments.

    Merges recurring billing notifications chronologically into a single unified candidate subscription,
    retaining the latest renewal date, authoritative pricing, and properly interpreting lifecycle transitions
    (such as subsequent refunds, payment failures, or tier downgrades).
    """
    # Sort chronologically by source_date ascending so newer events supersede older assumptions
    sorted_cands = sorted(
        raw_candidates,
        key=lambda c: c.source_date if c.source_date else datetime.min.replace(tzinfo=timezone.utc),
    )

    grouped: Dict[Tuple[str, str, str], SubscriptionCandidate] = {}
    for cand in sorted_cands:
        canon_name = canonical_merchant_name(cand.merchant_name)
        key = (cand.user_id, cand.account_id or "", canon_name.lower().strip())
        if key not in grouped:
            cand.merchant_name = canon_name
            grouped[key] = cand
        else:
            existing = grouped[key]
            # 1. Authoritative non-zero cost retention
            if cand.estimated_cost is not None and cand.estimated_cost > 0:
                existing.estimated_cost = cand.estimated_cost
                existing.currency = cand.currency or existing.currency
                existing.original_amount = cand.original_amount or existing.original_amount
                existing.original_currency = cand.original_currency or existing.original_currency
                if cand.billing_interval:
                    existing.billing_interval = cand.billing_interval
            elif existing.estimated_cost is None and cand.estimated_cost is not None:
                existing.estimated_cost = cand.estimated_cost
                existing.currency = cand.currency

            # 2. Retain specific product name if available
            if cand.product_name and (not existing.product_name or existing.product_name.lower() in ("subscription", "membership", "cloud_storage", "music", "security", "general", "-----")):
                existing.product_name = cand.product_name

            # 3. Chronological lifecycle reconciliation:
            # If the newer event reports a refund or downgrade or failure, it overrides an earlier active/trial state!
            if cand.lifecycle_status in ("refunded", "cancelled", "expired"):
                existing.lifecycle_status = cand.lifecycle_status
                existing.cancellation_signal = True
                existing.evidence_category = cand.evidence_category or existing.evidence_category
                if cand.cancellation_date:
                    existing.cancellation_date = cand.cancellation_date
            elif cand.lifecycle_status == "payment_failed":
                existing.lifecycle_status = "payment_failed"
                existing.cancellation_signal = True
                existing.evidence_category = cand.evidence_category or existing.evidence_category
            elif cand.lifecycle_status == "trial" and existing.lifecycle_status not in ("refunded", "cancelled", "expired", "payment_failed"):
                existing.lifecycle_status = "trial"

            # 4. Dates:
            if cand.trial_end_date:
                existing.trial_end_date = cand.trial_end_date
            if cand.purchase_date:
                existing.purchase_date = cand.purchase_date
            if cand.last_payment_date:
                existing.last_payment_date = cand.last_payment_date

            # If not cancelled or refunded, take newer detected renewal date if valid
            if existing.lifecycle_status not in ("refunded", "cancelled", "expired") and cand.detected_renewal_date:
                if not existing.detected_renewal_date or cand.detected_renewal_date > existing.detected_renewal_date:
                    existing.detected_renewal_date = cand.detected_renewal_date

            if cand.confidence > existing.confidence:
                existing.confidence = cand.confidence

            if cand.source_message_id and cand.source_message_id not in (existing.source_message_id or ""):
                existing.notes = (
                    f"{existing.notes or ''}; Additional receipt: {cand.source_message_id}"
                ).strip("; ")

    return list(grouped.values())


def _safe_b64url_decode(data_str: str, charset: str = "utf-8") -> Tuple[str, bool]:
    """Safely decode base64url data string with encoding fallbacks and whitespace cleaning.

    Returns (decoded_text, is_success).
    """
    if not data_str:
        return "", True
    cleaned = re.sub(r"\s+", "", data_str)
    # Check for invalid characters not part of base64url or padding
    if re.search(r"[^A-Za-z0-9_=\-]", cleaned):
        return "", False
    padded = cleaned + "=" * (-len(cleaned) % 4)
    raw_bytes = None
    try:
        raw_bytes = base64.urlsafe_b64decode(padded.encode("ascii"))
    except Exception:
        try:
            raw_bytes = base64.b64decode(padded.encode("ascii"))
        except Exception:
            return "", False

    if raw_bytes is None:
        return "", False

    for enc in [charset, "utf-8", "windows-1252", "iso-8859-1", "latin-1"]:
        if not enc:
            continue
        try:
            return raw_bytes.decode(enc), True
        except (UnicodeDecodeError, LookupError):
            continue
    return raw_bytes.decode("utf-8", errors="replace"), True


def _extract_all_mime_parts(payload: Dict[str, Any]) -> Tuple[List[str], List[str], bool]:
    """Recursively traverse nested MIME payload parts and extract plain and HTML content.

    Returns (plain_text_parts, html_text_parts, has_malformed_part).
    """
    plain_parts: List[str] = []
    html_parts: List[str] = []
    has_malformed = False

    body_data = payload.get("body", {}).get("data")
    mime_type = payload.get("mimeType", "").lower()

    charset = "utf-8"
    headers = payload.get("headers", [])
    for h in headers:
        if h.get("name", "").lower() == "content-type":
            ct = h.get("value", "")
            m = re.search(r'charset=["\']?([a-zA-Z0-9_-]+)', ct, re.IGNORECASE)
            if m:
                charset = m.group(1)

    if body_data:
        decoded, ok = _safe_b64url_decode(body_data, charset=charset)
        if not ok:
            has_malformed = True
        elif decoded.strip():
            if "html" in mime_type:
                html_parts.append(decoded)
            else:
                plain_parts.append(decoded)

    for part in payload.get("parts", []):
        sub_plain, sub_html, sub_malformed = _extract_all_mime_parts(part)
        if sub_malformed:
            has_malformed = True
        plain_parts.extend(sub_plain)
        html_parts.extend(sub_html)

    return plain_parts, html_parts, has_malformed


def _extract_body_with_metadata(payload: Dict[str, Any]) -> Tuple[str, bool]:
    """Extract and normalize plain text and HTML body from nested MIME parts with base64url decoding.

    Returns (normalized_content, is_malformed).
    """
    plain_parts, html_parts, has_malformed = _extract_all_mime_parts(payload)

    plain_combined = "\n".join(p.strip() for p in plain_parts if p.strip()).strip()
    html_combined = "\n".join(h.strip() for h in html_parts if h.strip()).strip()

    # If HTML is present, sanitize it into readable text
    sanitized_html = sanitize_email_content(html_combined) if html_combined else ""

    if plain_combined and sanitized_html:
        # If plain text is short (<80 chars, often just "view in browser") or missing amounts while HTML has it:
        if len(plain_combined) < 80 or (
            any(c in sanitized_html for c in ["$", "€", "£", "USD", "EUR"])
            and not any(c in plain_combined for c in ["$", "€", "£", "USD", "EUR"])
        ):
            combined_body = f"{plain_combined}\n{sanitized_html}".strip()
        else:
            combined_body = plain_combined if len(plain_combined) >= len(sanitized_html) else f"{plain_combined}\n{sanitized_html}".strip()
    elif sanitized_html:
        combined_body = sanitized_html
    else:
        combined_body = plain_combined

    return combined_body, has_malformed


def _extract_body_from_gmail_payload(payload: Dict[str, Any]) -> str:
    """Extract plain text or sanitized HTML body from nested MIME parts with base64url decoding."""
    content, _ = _extract_body_with_metadata(payload)
    return content


def _fetch_live_gmail_candidates(
    access_token: str,
    user_id: str,
    account_id: Optional[str] = None,
    account_email: Optional[str] = None,
    max_messages: int = 100,
    client: Optional[httpx.Client] = None,
    metrics: Optional[Dict[str, Any]] = None,
) -> List[SubscriptionCandidate]:
    """Query Gmail API for billing receipts, invoices, and renewal notices with full pagination."""
    from backend.app.email.oauth import refresh_access_token

    # Broad, comprehensive query targeting billing evidence, receipts, renewal notices, and payment processors
    query = (
        '((subject:(receipt OR invoice OR subscription OR billed OR billing OR renewal OR renews OR renewing '
        'OR membership OR autopay OR mandate OR "auto-renew" OR "auto-debit" OR "payment confirmation" '
        'OR "order confirmation" OR "tax invoice" OR "plan" OR "trial" OR "payment received") '
        'OR from:(googleplay-noreply@google.com OR no_reply@email.apple.com OR stripe.com OR razorpay.com '
        'OR paypal.com OR "Google Play" OR "App Store" OR "Spotify")) -category:spam -category:trash)'
    )
    list_url = "https://gmail.googleapis.com/gmail/v1/users/me/messages"

    http_client = client or httpx.Client(timeout=20.0)
    try:
        all_message_stubs: List[Dict[str, str]] = []
        page_token = None
        is_partial_scan = False

        # Paginate through Gmail API results until max_messages is reached or pages are exhausted
        while len(all_message_stubs) < max_messages:
            fetch_limit = min(100, max_messages - len(all_message_stubs))
            params: Dict[str, Any] = {"q": query, "maxResults": fetch_limit}
            if page_token:
                params["pageToken"] = page_token

            resp = http_client.get(
                list_url,
                params=params,
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if resp.status_code == 401:
                refreshed = refresh_access_token(user_id, account_id=account_id, client=http_client)
                if refreshed:
                    access_token = refreshed
                    resp = http_client.get(
                        list_url,
                        params=params,
                        headers={"Authorization": f"Bearer {access_token}"},
                    )

            if resp.status_code != 200:
                logger.warning("Gmail API message list returned status %s: %s", resp.status_code, resp.text[:200])
                break

            list_data = resp.json()
            msgs = list_data.get("messages", [])
            all_message_stubs.extend(msgs)

            page_token = list_data.get("nextPageToken")
            if not page_token:
                break

        # If there are more pages available beyond max_messages, flag as partial scan
        if page_token and len(all_message_stubs) >= max_messages:
            is_partial_scan = True

        if metrics is not None:
            metrics["messages_discovered"] = len(all_message_stubs)
            metrics["is_partial_scan"] = is_partial_scan
            metrics["scan_limit"] = max_messages

        candidates: List[SubscriptionCandidate] = []
        earliest_date: Optional[datetime] = None
        latest_date: Optional[datetime] = None

        for m in all_message_stubs:
            m_id = m.get("id")
            if not m_id:
                continue

            msg_url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{m_id}?format=full"
            try:
                m_resp = http_client.get(
                    msg_url,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                if m_resp.status_code == 401:
                    refreshed = refresh_access_token(user_id, account_id=account_id, client=http_client)
                    if refreshed:
                        access_token = refreshed
                        m_resp = http_client.get(
                            msg_url,
                            headers={"Authorization": f"Bearer {access_token}"},
                        )

                if m_resp.status_code != 200:
                    if metrics is not None:
                        metrics["parsing_failures"] = metrics.get("parsing_failures", 0) + 1
                        metrics.setdefault("failure_reasons", {})["http_fetch_error"] = (
                            metrics.get("failure_reasons", {}).get("http_fetch_error", 0) + 1
                        )
                    continue

                if metrics is not None:
                    metrics["messages_retrieved"] = metrics.get("messages_retrieved", 0) + 1

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

                body_text, is_malformed = _extract_body_with_metadata(data.get("payload", {}))
                if is_malformed:
                    if metrics is not None:
                        metrics["parsing_failures"] = metrics.get("parsing_failures", 0) + 1
                        metrics.setdefault("failure_reasons", {})["malformed_mime"] = (
                            metrics.get("failure_reasons", {}).get("malformed_mime", 0) + 1
                        )
                    continue

                if not body_text:
                    body_text = data.get("snippet", "")

                received_at = None
                internal_date = data.get("internalDate")
                if internal_date:
                    try:
                        received_at = datetime.fromtimestamp(int(internal_date) / 1000.0, tz=timezone.utc)
                        if earliest_date is None or received_at < earliest_date:
                            earliest_date = received_at
                        if latest_date is None or received_at > latest_date:
                            latest_date = received_at
                    except Exception:
                        pass

                eval_result = evaluate_billing_email(
                    message_id=m_id,
                    subject=subject,
                    sender=sender,
                    raw_content=body_text,
                    received_at=received_at,
                    user_id=user_id,
                    account_id=account_id,
                    account_email=account_email,
                )

                safe_id = m_id[-8:] if len(m_id) >= 8 else m_id
                logger.info(
                    "Gmail evaluation: msg_id=%s, status=%s, reason=%s",
                    safe_id,
                    eval_result.status,
                    eval_result.reason,
                )

                if eval_result.status == "candidate" and eval_result.candidate:
                    eval_result.candidate.is_demo = False
                    candidates.append(eval_result.candidate)
                    if metrics is not None:
                        metrics["candidates_created"] = metrics.get("candidates_created", 0) + 1
                        metrics["messages_parsed"] = metrics.get("messages_parsed", 0) + 1
                elif eval_result.status == "skipped":
                    if metrics is not None:
                        metrics["messages_parsed"] = metrics.get("messages_parsed", 0) + 1
                        if eval_result.reason == "insufficient_evidence":
                            metrics["messages_insufficient_evidence"] = (
                                metrics.get("messages_insufficient_evidence", 0) + 1
                            )
                        else:
                            metrics["messages_skipped"] = metrics.get("messages_skipped", 0) + 1
                        r_key = eval_result.reason or "skipped"
                        metrics.setdefault("skip_reasons", {})[r_key] = (
                            metrics.get("skip_reasons", {}).get(r_key, 0) + 1
                        )
                else:
                    if metrics is not None:
                        metrics["parsing_failures"] = metrics.get("parsing_failures", 0) + 1
                        r_key = eval_result.reason or "failed"
                        metrics.setdefault("failure_reasons", {})[r_key] = (
                            metrics.get("failure_reasons", {}).get(r_key, 0) + 1
                        )

            except Exception:
                if metrics is not None:
                    metrics["parsing_failures"] = metrics.get("parsing_failures", 0) + 1
                    metrics.setdefault("failure_reasons", {})["parse_exception"] = (
                        metrics.get("failure_reasons", {}).get("parse_exception", 0) + 1
                    )
                continue

        if metrics is not None and earliest_date and latest_date:
            metrics["scan_date_range"] = f"{earliest_date.strftime('%Y-%m-%d')} to {latest_date.strftime('%Y-%m-%d')}"

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

    # Check if user already has a matching manual subscription
    from backend.app.database import list_subscriptions_from_db
    existing_user_subs = list_subscriptions_from_db(user_id=user_id, include_archived=False)
    matching_manual = next(
        (s for s in existing_user_subs if s.discovery_source == "manual" and s.service_name.lower().strip() == cand.merchant_name.lower().strip()),
        None
    )

    clean_name = re.sub(r"[^a-zA-Z0-9]", "_", cand.merchant_name).lower()
    if cand.lifecycle_status == "refunded":
        status = SubscriptionStatus.REFUNDED
    elif cand.lifecycle_status == "cancelled" or cand.cancellation_signal:
        status = SubscriptionStatus.CANCELLED
    elif cand.lifecycle_status == "expired":
        status = SubscriptionStatus.EXPIRED
    elif cand.lifecycle_status == "payment_failed":
        status = SubscriptionStatus.PAYMENT_FAILED
    elif cand.lifecycle_status == "trial":
        status = SubscriptionStatus.TRIAL
    elif cand.lifecycle_status == "unknown":
        status = SubscriptionStatus.UNKNOWN
    elif cand.lifecycle_status == "requires_review":
        status = SubscriptionStatus.REQUIRES_REVIEW
    else:
        status = SubscriptionStatus.ACTIVE

    clean_uid = re.sub(r"[^a-zA-Z0-9]", "_", cand.user_id).lower()

    if matching_manual:
        sub_id = matching_manual.subscription_id
        sub = matching_manual
        sub.account_id = cand.account_id or sub.account_id
        sub.account_email = cand.account_email or sub.account_email
        if sub.renewal_date is None and cand.detected_renewal_date is not None:
            sub.renewal_date = cand.detected_renewal_date
        if sub.monthly_cost is None and cand.estimated_cost is not None:
            sub.monthly_cost = cand.estimated_cost
            sub.currency = cand.currency or sub.currency or "INR"
        sub.original_amount = cand.original_amount or sub.original_amount
        sub.original_currency = cand.original_currency or sub.original_currency
        sub.lifecycle_status = cand.lifecycle_status or sub.lifecycle_status
        sub.evidence_category = cand.evidence_category or sub.evidence_category
        sub.trial_end_date = cand.trial_end_date or sub.trial_end_date
        sub.purchase_date = cand.purchase_date or sub.purchase_date
        sub.cancellation_date = cand.cancellation_date or sub.cancellation_date
        sub.last_payment_date = cand.last_payment_date or sub.last_payment_date
        if cand.lifecycle_status in ("refunded", "cancelled", "expired", "payment_failed", "trial", "unknown", "requires_review"):
            sub.status = status
        sub.notes = (sub.notes or "") + f" | Verified via Gmail billing evidence from {cand.source_sender or 'provider'}."
    else:
        sub_id = f"sub_email_{clean_uid}_{clean_name}_{cand.candidate_id[-8:]}"
        sub = Subscription(
            subscription_id=sub_id,
            service_name=cand.merchant_name,
            status=status,
            renewal_date=cand.detected_renewal_date,
            last_used_at=cand.detected_last_used,
            monthly_cost=cand.estimated_cost,
            currency=cand.currency or "INR",
            original_amount=cand.original_amount,
            original_currency=cand.original_currency,
            billing_cycle=cand.billing_interval or "monthly",
            user_id=cand.user_id,
            account_id=cand.account_id,
            account_email=cand.account_email,
            category=cand.product_name,
            discovery_source=DiscoverySource.EMAIL.value,
            confidence=cand.confidence,
            notes=cand.notes or f"Discovered via email receipt from {cand.source_sender or 'provider'}.",
            is_archived=False,
            lifecycle_status=cand.lifecycle_status,
            evidence_category=cand.evidence_category,
            trial_end_date=cand.trial_end_date,
            purchase_date=cand.purchase_date,
            cancellation_date=cand.cancellation_date,
            last_payment_date=cand.last_payment_date,
        )

    # Save to unified database store
    save_subscription(sub)

    # Construct and save supporting evidence item
    ev_id = f"ev_email_{clean_uid}_{clean_name}_{cand.candidate_id[-8:]}"
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
    cand = get_subscription_candidate(candidate_id, user_id=user_id)
    if not cand:
        raise ValueError(f"Subscription candidate '{candidate_id}' not found.")
    return update_candidate_status(candidate_id, CandidateReviewStatus.DISMISSED, user_id=user_id)
