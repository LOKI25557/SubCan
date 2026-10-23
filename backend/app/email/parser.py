"""Safe billing email parsing and subscription candidate extraction.

Safety & Sanitization Invariants:
1. Email body content is treated as UNTRUSTED DATA.
2. Strips all active executable markup (<script>, <iframe>, onclick, etc.).
3. Never allows email content to execute commands or override system policies.
4. Identifies receipt dates, renewal terms, monetary values, and cancellation signals.
"""

from datetime import date, datetime, timezone
import html
import re
from typing import Any, Dict, List, Optional, Tuple

from backend.app.models import CandidateReviewStatus, SubscriptionCandidate


# Common recurring subscription providers and their domain / name patterns
KNOWN_MERCHANT_PATTERNS = [
    (r"netflix", "Netflix", "streaming"),
    (r"spotify", "Spotify", "music"),
    (r"audiocloud", "AudioCloud Pro", "music"),
    (r"cinevault", "CineVault", "streaming"),
    (r"fitstream", "FitStream Pass", "fitness"),
    (r"moviemax", "MovieMax", "streaming"),
    (r"gamestream", "GameStream", "gaming"),
    (r"adobe|creative cloud", "Adobe Creative Cloud", "software"),
    (r"amazon prime|prime video", "Amazon Prime", "streaming"),
    (r"disney\+|disney plus", "Disney+", "streaming"),
    (r"apple music|apple tv|apple services", "Apple Services", "streaming"),
    (r"hulu", "Hulu", "streaming"),
    (r"youtube premium|youtube music", "YouTube Premium", "streaming"),
    (r"github", "GitHub", "developer"),
    (r"nytimes|new york times", "The New York Times", "news"),
    (r"dropbox", "Dropbox", "cloud_storage"),
    (r"google workspace|google one", "Google Workspace", "productivity"),
    (r"chatgpt|openai", "OpenAI ChatGPT Plus", "ai_tools"),
]

# Cancellation keywords indicating an existing cancellation confirmation
CANCELLATION_SIGNALS = [
    r"cancellation confirm",
    r"cancelled your subscription",
    r"canceled your subscription",
    r"sorry to see you go",
    r"membership has been cancelled",
    r"membership has been canceled",
    r"subscription has ended",
    r"refund has been processed",
    r"will not be renewed",
]


def sanitize_email_content(raw_html_or_text: str) -> str:
    """Sanitize raw email HTML/text to prevent XSS and code injection.

    Removes script tags, iframe tags, object tags, and event handlers.
    Unescapes entities safely into plain readable text.
    """
    if not raw_html_or_text:
        return ""

    # Remove script and iframe blocks entirely
    cleaned = re.sub(r"(?is)<script.*?>.*?</script>", " ", raw_html_or_text)
    cleaned = re.sub(r"(?is)<iframe.*?>.*?</iframe>", " ", cleaned)
    cleaned = re.sub(r"(?is)<style.*?>.*?</style>", " ", cleaned)

    # Remove HTML tags while preserving text
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)

    # Decode HTML entities
    decoded = html.unescape(cleaned)

    # Normalize whitespace
    normalized = re.sub(r"\s+", " ", decoded).strip()
    return normalized


def extract_monetary_amount(text: str) -> Tuple[Optional[float], Optional[str]]:
    """Extract price amount and currency symbol from email body."""
    # Matches patterns like $14.99, USD 14.99, €9.99, EUR 9.99, £8.50
    usd_match = re.search(r"(?:\$|USD\s*)\s*(\d+(?:\.\d{2})?)", text, re.IGNORECASE)
    if usd_match:
        try:
            return float(usd_match.group(1)), "USD"
        except ValueError:
            pass

    eur_match = re.search(r"(?:€|EUR\s*)\s*(\d+(?:\.\d{2})?)", text, re.IGNORECASE)
    if eur_match:
        try:
            return float(eur_match.group(1)), "EUR"
        except ValueError:
            pass

    gbp_match = re.search(r"(?:£|GBP\s*)\s*(\d+(?:\.\d{2})?)", text, re.IGNORECASE)
    if gbp_match:
        try:
            return float(gbp_match.group(1)), "GBP"
        except ValueError:
            pass

    generic_match = re.search(r"(?:total|amount|charge|billed):\s*(\d+(?:\.\d{2})?)", text, re.IGNORECASE)
    if generic_match:
        try:
            return float(generic_match.group(1)), "USD"
        except ValueError:
            pass

    return None, None


def extract_billing_interval(text: str) -> str:
    """Detect whether subscription is monthly, annual, or quarterly."""
    lower = text.lower()
    if any(k in lower for k in ["/year", "per year", "annual", "yearly", "billed annually"]):
        return "annual"
    if any(k in lower for k in ["quarterly", "/quarter", "every 3 months"]):
        return "quarterly"
    if any(k in lower for k in ["weekly", "/week", "per week"]):
        return "weekly"
    return "monthly"


def extract_merchant(subject: str, sender: str, body: str) -> Tuple[Optional[str], Optional[str]]:
    """Identify subscription provider name and category."""
    combined = f"{sender} {subject} {body[:500]}".lower()

    for pattern, name, category in KNOWN_MERCHANT_PATTERNS:
        if re.search(r"\b" + pattern + r"\b", combined, re.IGNORECASE):
            return name, category

    # Fallback heuristic: Extract name from sender e.g. "billing@example.com" -> "Example"
    sender_match = re.search(r"@([a-zA-Z0-9_-]+)\.", sender)
    if sender_match:
        domain_name = sender_match.group(1).capitalize()
        if domain_name.lower() not in ["gmail", "yahoo", "outlook", "hotmail", "mail"]:
            return domain_name, "general"

    return None, None


def detect_cancellation_signal(text: str) -> bool:
    """Check if message represents a subscription cancellation or refund."""
    lower = text.lower()
    return any(re.search(sig, lower) for sig in CANCELLATION_SIGNALS)


def extract_dates(text: str) -> Tuple[Optional[date], Optional[datetime]]:
    """Extract next renewal date or transaction date if present."""
    # Look for dates in ISO or Month DD, YYYY formats
    # e.g., 2026-11-15 or November 15, 2026
    iso_match = re.search(r"\b(202[4-9])-([01]\d)-([0-3]\d)\b", text)
    if iso_match:
        try:
            y, m, d = int(iso_match.group(1)), int(iso_match.group(2)), int(iso_match.group(3))
            return date(y, m, d), None
        except ValueError:
            pass

    month_names = {
        "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
        "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    date_pattern = r"\b(" + "|".join(month_names.keys()) + r")\s+(\d{1,2}),?\s+(202[4-9])\b"
    word_date_match = re.search(date_pattern, text, re.IGNORECASE)
    if word_date_match:
        try:
            month_str = word_date_match.group(1).lower()
            m = month_names[month_str]
            d = int(word_date_match.group(2))
            y = int(word_date_match.group(3))
            return date(y, m, d), None
        except (KeyError, ValueError):
            pass

    return None, None


def parse_billing_email(
    message_id: str,
    subject: str,
    sender: str,
    raw_content: str,
    received_at: Optional[datetime] = None,
    user_id: str = "user_default",
) -> Optional[SubscriptionCandidate]:
    """Parse a single email message into a candidate subscription if billing evidence found."""
    clean_text = sanitize_email_content(raw_content)
    clean_subject = sanitize_email_content(subject)
    clean_sender = sanitize_email_content(sender)

    # Check for billing relevance
    billing_keywords = [
        "receipt", "invoice", "subscription", "billed", "payment",
        "renewal", "membership", "plan", "trial", "cancelled", "canceled",
    ]
    relevance_text = f"{clean_subject} {clean_sender} {clean_text[:600]}".lower()
    if not any(k in relevance_text for k in billing_keywords):
        return None

    merchant, category = extract_merchant(clean_subject, clean_sender, clean_text)
    if not merchant:
        return None

    amount, currency = extract_monetary_amount(clean_text)
    interval = extract_billing_interval(clean_text)
    renewal_date, _ = extract_dates(clean_text)
    is_cancelled = detect_cancellation_signal(f"{clean_subject} {clean_text}")

    # Confidence calculation: higher if merchant + amount + renewal/cancellation found
    confidence = 0.6
    if amount is not None:
        confidence += 0.2
    if renewal_date is not None or is_cancelled:
        confidence += 0.15
    confidence = min(0.95, confidence)

    clean_uid = re.sub(r"[^a-zA-Z0-9]", "_", user_id).lower()
    clean_merchant = re.sub(r"[^a-zA-Z0-9]", "_", merchant).lower()
    candidate_id = f"cand_{clean_uid}_{clean_merchant}_{message_id[-8:]}"

    snippet = clean_text[:250] + ("…" if len(clean_text) > 250 else "")

    return SubscriptionCandidate(
        candidate_id=candidate_id,
        user_id=user_id,
        merchant_name=merchant,
        product_name=category,
        estimated_cost=amount,
        currency=currency or "USD",
        billing_interval=interval,
        detected_renewal_date=renewal_date,
        detected_last_used=None,  # Email receipts establish billing, never user streaming/usage
        review_status=CandidateReviewStatus.PENDING,
        confidence=round(confidence, 2),
        source_message_id=message_id,
        source_subject=clean_subject[:200],
        source_sender=clean_sender[:100],
        source_date=received_at or datetime.now(timezone.utc),
        email_snippet=snippet,
        cancellation_signal=is_cancelled,
        notes=f"Detected from billing email: {clean_subject[:100]}",
        created_at=datetime.now(timezone.utc),
    )
