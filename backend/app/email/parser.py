"""Safe billing email parsing and subscription candidate extraction.

Safety & Sanitization Invariants:
1. Email body content is treated as UNTRUSTED DATA.
2. Strips all active executable markup (<script>, <iframe>, onclick, etc.).
3. Never allows email content to execute commands or override system policies.
4. Identifies receipt dates, renewal terms, monetary values, and cancellation signals.
5. Distinguishes genuinely irrelevant emails from parser errors with evidence-based reasoning.
6. Never infers an active paid subscription from a brand mention, newsletter, GitHub notification, or unrelated email.
"""

from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.utils import parseaddr
import html
import re
from typing import Any, Dict, List, Optional, Tuple

from backend.app.models import (
    CandidateReviewStatus,
    EmailEvidenceCategory,
    SubscriptionCandidate,
    SubscriptionStatus,
)


@dataclass
class EmailParseResult:
    """Structured outcome of an email billing evaluation."""
    status: str  # "candidate", "skipped", "failed"
    candidate: Optional[SubscriptionCandidate] = None
    reason: Optional[str] = None  # safe, redacted skip or failure classification
    details: Optional[str] = None  # sanitized explanation


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
    (r"hotstar", "Disney+ Hotstar", "streaming"),
    (r"disney\+|disney plus", "Disney+", "streaming"),
    (r"jiocinema|jio cinema", "JioCinema", "streaming"),
    (r"apple music|apple tv|apple services|apple\.com/bill|icloud\+", "Apple Services", "streaming"),
    (r"hulu", "Hulu", "streaming"),
    (r"youtube premium|youtube music", "YouTube Premium", "streaming"),
    (r"github", "GitHub", "developer"),
    (r"cursor", "Cursor", "developer"),
    (r"nytimes|new york times", "The New York Times", "news"),
    (r"dropbox", "Dropbox", "cloud_storage"),
    (r"google ai plus|google one", "Google One", "cloud_storage"),
    (r"google workspace|gsuite|g suite", "Google Workspace", "productivity"),
    (r"chatgpt|openai", "OpenAI ChatGPT Plus", "ai_tools"),
    (r"microsoft 365|office 365", "Microsoft 365", "productivity"),
    (r"microsoft corporation|microsoft", "Microsoft", "productivity"),
    (r"playstation plus|ps plus", "PlayStation Plus", "gaming"),
    (r"nintendo switch online", "Nintendo", "gaming"),
    (r"paramount\+|paramount plus", "Paramount+", "streaming"),
    (r"peacock", "Peacock", "streaming"),
    (r"hbo max|\bmax\b", "Max", "streaming"),
    (r"audible", "Audible", "audiobooks"),
    (r"coursera", "Coursera", "education"),
    (r"duolingo", "Duolingo", "education"),
    (r"udemy", "Udemy", "education"),
    (r"overleaf", "Overleaf", "developer"),
    (r"mcafee", "McAfee", "security"),
    (r"deepseek", "DeepSeek", "ai_tools"),
    (r"hostinger", "Hostinger", "hosting"),
    (r"vercel", "Vercel", "developer"),
    (r"swiggy one", "Swiggy One", "food_delivery"),
    (r"zomato gold", "Zomato Gold", "food_delivery"),
    (r"chess\.com|chesscom", "Chess.com", "gaming"),
    (r"unacademy", "Unacademy", "education"),
    (r"linkedin premium|linkedin learning", "LinkedIn", "productivity"),
    (r"notion", "Notion", "productivity"),
    (r"figma", "Figma", "design"),
    (r"canva", "Canva", "design"),
    (r"slack", "Slack", "productivity"),
    (r"zoom", "Zoom", "productivity"),
    (r"claude\.ai|anthropic", "Anthropic Claude", "ai_tools"),
    (r"midjourney", "Midjourney", "ai_tools"),
    (r"nordvpn", "NordVPN", "security"),
    (r"expressvpn", "ExpressVPN", "security"),
    (r"1password", "1Password", "security"),
    (r"dashlane", "Dashlane", "security"),
    (r"patreon", "Patreon", "creators"),
    (r"substack", "Substack", "newsletters"),
    (r"medium membership", "Medium", "news"),
    (r"strava", "Strava", "fitness"),
    (r"peloton", "Peloton", "fitness"),
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
    r"subscription has expired",
    r"protection has ended",
    r"protection has expired",
    r"refund has been processed",
    r"refund has been approved",
    r"will not be renewed",
    r"you're no longer on",
    r"no longer on standard",
    r"now on spotify's free",
    r"account has been downgraded",
    r"downgraded to free",
]

# Payment processing platforms that act as intermediaries
PAYMENT_PROCESSORS = [
    "stripe.com",
    "paypal.com",
    "paddle.com",
    "fastspring.com",
    "square.com",
    "braintreepayments.com",
    "cleverbridge.com",
    "2checkout.com",
]


def sanitize_email_content(raw_html_or_text: Optional[str]) -> str:
    """Sanitize raw email HTML/text to prevent XSS and code injection.

    Removes script tags, iframe tags, object tags, and event handlers.
    Safely unescapes entities into plain readable text while preserving table and paragraph breaks.
    """
    if not raw_html_or_text:
        return ""

    # Remove script, iframe, and style blocks entirely
    cleaned = re.sub(r"(?is)<script.*?>.*?</script>", " ", raw_html_or_text)
    cleaned = re.sub(r"(?is)<iframe.*?>.*?</iframe>", " ", cleaned)
    cleaned = re.sub(r"(?is)<style.*?>.*?</style>", " ", cleaned)
    cleaned = re.sub(r"(?is)<!--.*?-->", " ", cleaned)

    # Convert block line-break tags to spaces/newlines so table cells and lines do not merge
    cleaned = re.sub(r"(?i)<br\s*/?>", " \n", cleaned)
    cleaned = re.sub(r"(?i)</(?:p|div|tr|td|li|h[1-6])>", " \n", cleaned)

    # Remove remaining HTML tags while preserving text
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)

    # Decode HTML entities
    decoded = html.unescape(cleaned)

    # Normalize whitespace
    normalized = re.sub(r"[ \t]+", " ", decoded)
    normalized = re.sub(r"\n\s*\n+", "\n", normalized).strip()
    return normalized


def extract_sender_info(sender: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Extract display name, email address, and root domain from sender header.

    Example: "Netflix <info@mailer.netflix.com>" -> ("Netflix", "info@mailer.netflix.com", "netflix")
    """
    if not sender:
        return None, None, None
    display_name, email_addr = parseaddr(sender)
    display_name = display_name.strip("\"' ")
    email_addr = email_addr.strip().lower()
    root_domain = None
    if "@" in email_addr:
        domain_part = email_addr.split("@", 1)[1]
        parts = domain_part.split(".")
        if len(parts) >= 2:
            # Handle multi-part ccTLDs like .co.uk, .com.au
            if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "edu", "gov") and len(parts[-1]) == 2:
                root_domain = parts[-3]
            else:
                root_domain = parts[-2]
    return (display_name or None, email_addr or None, root_domain)


def _match_merchant_in_text(pattern: str, text: str) -> bool:
    """Match merchant pattern handling word boundaries and trailing special characters like '+'."""
    sub_patterns = pattern.split("|")
    compiled_subs = []
    for sp in sub_patterns:
        sp_clean = sp.strip()
        leading_b = r"\b" if re.match(r"^\w", sp_clean.replace("\\", "")) else ""
        trailing_b = r"\b" if re.search(r"\w$", sp_clean.replace("\\", "")) else r"(?!\w)"
        compiled_subs.append(f"{leading_b}{sp_clean}{trailing_b}")
    full_regex = "(?:" + "|".join(compiled_subs) + ")"
    return bool(re.search(full_regex, text, re.IGNORECASE))


def extract_product_name(subject: str, sender: str, body: str, merchant: str, default_category: Optional[str]) -> str:
    """Extract an authentic product or plan name from email content when available."""
    # 1. Google Play line item (e.g. 'Microsoft 365 Personal with trial', 'Google AI Plus (400 GB)')
    gplay_item = re.search(r"Item:?\s*(?:Price)?[\s\n\r]+([^\n\r]+?)(?:\s+[₹\u20b9$€£]|\s+Auto-renewing|\s+Total:|\n|$)", body, re.IGNORECASE)
    if gplay_item:
        val = gplay_item.group(1).strip()
        if len(val) >= 3 and not re.match(r"^[₹\u20b9$€£0-9., ]+$", val):
            # Clean up trailing company name like '(Microsoft Corporation)' or '(by Google LLC)'
            val_clean = re.sub(r"\s*\((?:by\s+)?[A-Za-z0-9\s.,-]+(?:Corporation|Inc|LLC|Ltd|Limited|Ireland|Commerce)\)\s*$", "", val, flags=re.IGNORECASE).strip()
            return val_clean

    # 2. ChatGPT / OpenAI Plan
    if "openai" in merchant.lower() or "chatgpt" in merchant.lower():
        if "chatgpt plus" in body.lower() or "plus subscription" in body.lower() or "plus" in subject.lower():
            return "ChatGPT Plus"

    # 3. Spotify
    if "spotify" in merchant.lower():
        spot_item = re.search(r"Item\(s\)[\s\n\r]+([^\n\r]+)", body, re.IGNORECASE)
        if spot_item:
            return spot_item.group(1).strip()
        if '"premium standard"' in subject.lower():
            return "Premium Standard"
        if "premium individual" in body.lower():
            return "Premium Individual"

    # 4. Explicit "Plan:" or "Plan Name:"
    plan_match = re.search(r"(?:plan|subscription\s+to|membership)[\s:]+([A-Za-z0-9\s()+-]{3,40}?)(?:\s+plan|\s+was|\s+is|\.|\n|<|$)", body, re.IGNORECASE)
    if plan_match:
        val = plan_match.group(1).strip()
        if len(val) >= 3 and val.lower() not in ["your", "a", "the", "active"]:
            return val

    return default_category or "subscription"


def extract_merchant(subject: str, sender: str, body: str) -> Tuple[Optional[str], Optional[str]]:
    """Identify subscription provider name and category with payment processor awareness."""
    combined = f"{sender} {subject} {body[:800]}".lower()

    # 1. Special handling for Google Play receipts where underlying service is sold
    if "google play" in combined or "googleplay" in sender.lower():
        # Check for provider or item in body
        provider_match = re.search(r"from\s+([A-Za-z0-9\s&.,-]{2,40}?)\s+on\s+Google\s+Play", body, re.IGNORECASE)
        if provider_match:
            p_name = provider_match.group(1).lower()
            if "microsoft" in p_name:
                return "Microsoft 365", "productivity"
            if "google digital" in p_name or "google ireland" in p_name or "google llc" in p_name:
                if "google ai plus" in body.lower():
                    return "Google One", "cloud_storage"
                return "Google One", "cloud_storage"
        item_match = re.search(r"Item:?\s*(?:Price)?[\s\n\r]+([^\n\r]+?)(?:\s+[₹\u20b9$€£]|\s+Auto-renewing|\s+Total:|\n|$)", body, re.IGNORECASE)
        if item_match:
            item_text = item_match.group(1).lower()
            if "microsoft" in item_text:
                return "Microsoft 365", "productivity"
            if "google ai plus" in item_text or "google one" in item_text:
                return "Google One", "cloud_storage"

    # 2. Match against known subscription providers
    for pattern, name, category in KNOWN_MERCHANT_PATTERNS:
        if _match_merchant_in_text(pattern, combined):
            return name, category

    # 3. Check for third-party billing processor with named merchant in subject/body
    display_name, email_addr, root_domain = extract_sender_info(sender)
    if email_addr and any(proc in email_addr for proc in PAYMENT_PROCESSORS):
        proc_match = re.search(
            r"(?:receipt\s+from|payment\s+to|subscription\s+to|invoice\s+from|billed\s+by)\s+([A-Za-z0-9\s&.-]{2,35}?)(?:\s+for|\s+of|\.|\n|<|$)",
            f"{subject} {body[:400]}",
            re.IGNORECASE,
        )
        if proc_match:
            merchant_name = proc_match.group(1).strip()
            if merchant_name and merchant_name.lower() not in ["your", "a", "the", "stripe", "paypal"]:
                return merchant_name, "general"

    # 4. Use sender display name if authentic and non-generic
    if display_name:
        generic_names = {
            "billing", "receipts", "support", "no-reply", "noreply", "team",
            "customer support", "notification", "notifications", "service",
            "mailer", "info", "accounts", "orders"
        }
        if display_name.lower() not in generic_names and len(display_name) >= 3:
            return display_name, "general"

    # 5. Fallback heuristic: Extract name from root domain
    if root_domain:
        generic_domains = {
            "gmail", "yahoo", "outlook", "hotmail", "mail", "icloud",
            "proton", "protonmail", "sendgrid", "mailgun", "postmarkapp"
        }
        if root_domain.lower() not in generic_domains and len(root_domain) >= 3:
            return root_domain.capitalize(), "general"

    return None, None


def extract_monetary_amount(text: str) -> Tuple[Optional[float], Optional[str]]:
    """Extract price amount and currency symbol from email body with multi-currency support.

    Prioritizes recurring subscription terms (e.g. 'currently ₹689.00/month') and substantive
    plan line items before falling back to zero-dollar initial trial charges.
    Strips tracking URLs, rejects survey/quote statistics, and requires billing context for amounts.
    """
    if not text:
        return None, None

    # Step 0: Strip URLs to prevent token pollution (e.g. MNrs2A7K in tracking URLs)
    clean_text = re.sub(r"https?://\S+", " ", text)

    # Step 0b: Mask survey / research / statistical quotes that quote losses or third-party averages
    # e.g. "Scam victims lose an average of $1,471. McAfee Research †" or "victims lost on average $1,471 per incident"
    clean_text = re.sub(
        r"(?:victims?\s+lost?|lost?\s+(?:on\s+)?average|lose\s+an\s+average\s+of|average\s+of|research\s+†|survey\s+shows|study\s+by|study\s+shows)[\s\w,]*?[₹\u20b9$€£]\s*[\d,.]+(?:\s*per\s+incident)?",
        " ",
        clean_text,
        flags=re.IGNORECASE,
    )
    clean_text = re.sub(
        r"[₹\u20b9$€£]\s*[\d,.]+\s*per\s+incident",
        " ",
        clean_text,
        flags=re.IGNORECASE,
    )

    # Priority 0: Explicit recurring subscription price statements (e.g. Google Play trials: "subscription cost (currently ₹689.00/month)")
    recurring_patterns = [
        # INR
        (r"(?:subscription\s+cost|automatically\s+charged|regular\s+price|renews\s+at|will\s+be\s+charged)[\s:(]*(?:currently\s*)?(?:[₹\u20b9]|\bINR\b|\bRs\.?|\bRs\b)\s*(\d+(?:,\d{2,3})*(?:\.\d{2})?)\s*/\s*(?:month|mo|year|yr|week)", "INR"),
        (r"(\d+(?:,\d{2,3})*(?:\.\d{2})?)\s*(?:INR|Rs\.?)\s*/\s*(?:month|mo|year|yr|week)", "INR"),
        # USD
        (r"(?:subscription\s+cost|automatically\s+charged|regular\s+price|renews\s+at|will\s+be\s+charged)[\s:(]*(?:currently\s*)?(?:[$]|\bUSD\b)\s*(\d+(?:,\d{3})*(?:\.\d{2})?)\s*/\s*(?:month|mo|year|yr|week)", "USD"),
        # EUR
        (r"(?:subscription\s+cost|automatically\s+charged|regular\s+price|renews\s+at|will\s+be\s+charged)[\s:(]*(?:currently\s*)?(?:[€]|\bEUR\b)\s*(\d+(?:\.\d{3})*(?:[.,]\d{2})?)\s*/\s*(?:month|mo|year|yr|week)", "EUR"),
        # GBP
        (r"(?:subscription\s+cost|automatically\s+charged|regular\s+price|renews\s+at|will\s+be\s+charged)[\s:(]*(?:currently\s*)?(?:[£]|\bGBP\b)\s*(\d+(?:,\d{3})*(?:\.\d{2})?)\s*/\s*(?:month|mo|year|yr|week)", "GBP"),
    ]
    for pattern, curr in recurring_patterns:
        m = re.search(pattern, clean_text, re.IGNORECASE)
        if m:
            val_str = m.group(1).replace(",", "").strip()
            try:
                amt = float(val_str)
                if amt > 0:
                    return amt, curr
            except ValueError:
                pass

    # Priority 0.5: Plan line items e.g. "ChatGPT Plus Subscription ₹1694.07" or "Spotify Premium ₹199.00"
    plan_line_patterns = [
        (r"(?:subscription|plan|membership)[\s\n\r]+(?:[₹\u20b9]|\bINR\b|\bRs\.?|\bRs\b)\s*(\d+(?:,\d{2,3})*(?:\.\d{2})?)", "INR"),
        (r"(?:subscription|plan|membership)[\s\n\r]+(\d+(?:,\d{2,3})*(?:\.\d{2})?)\s*(?:[₹\u20b9]|\bINR\b|\bRs\.?|\bRs\b)", "INR"),
        (r"(?:subscription|plan|membership)[\s\n\r]+(?:[$]|\bUSD\b)\s*(\d+(?:,\d{3})*(?:\.\d{2})?)", "USD"),
        (r"(?:subscription|plan|membership)[\s\n\r]+(?:[€]|\bEUR\b)\s*(\d+(?:\.\d{3})*(?:[.,]\d{2})?)", "EUR"),
        (r"(?:subscription|plan|membership)[\s\n\r]+(?:[£]|\bGBP\b)\s*(\d+(?:,\d{3})*(?:\.\d{2})?)", "GBP"),
    ]
    for pattern, curr in plan_line_patterns:
        m = re.search(pattern, clean_text, re.IGNORECASE)
        if m:
            val_str = m.group(1).replace(",", "").strip()
            try:
                amt = float(val_str)
                if amt > 0:
                    return amt, curr
            except ValueError:
                pass

    # Priority 1: Amounts explicitly labeled as total, billed, charged, paid, or subtotal
    priority_patterns = [
        # INR (₹, Rs., INR) with Indian comma formatting and optional /-
        (r"(?:total|amount\s+billed|amount\s+charged|amount\s+paid|billed|charged|payment\s+of|subtotal|recurring\s+charge|order\s+total)[\s:]*(?:[₹\u20b9]|\bINR\b|\bRs\.?|\bRs\b)\s*(\d+(?:,\d{2,3})*(?:\.\d{2})?(?:\s*/-)?)", "INR"),
        # Amount followed by INR
        (r"(?:total|amount\s+billed|amount\s+charged|amount\s+paid|billed|charged|payment\s+of|subtotal|recurring\s+charge|order\s+total)[\s:]*(\d+(?:,\d{2,3})*(?:\.\d{2})?)\s*(?:INR|Rs\.?)\b", "INR"),
        # USD ($)
        (r"(?:total|amount\s+billed|amount\s+charged|amount\s+paid|billed|charged|payment\s+of|subtotal|recurring\s+charge|order\s+total)[\s:]*(?:[$]|\bUSD\b)\s*(\d+(?:,\d{3})*(?:\.\d{2})?)", "USD"),
        # EUR (€)
        (r"(?:total|amount\s+billed|amount\s+charged|amount\s+paid|billed|charged|payment\s+of|subtotal|recurring\s+charge|order\s+total|montant\s*(?:pr[eé]lev[eé]|pay[eé]|factur[eé])?|betrag|importe|pago)[\s:]*(?:[€]|\bEUR\b)\s*(\d+(?:\.\d{3})*(?:[.,]\d{2})?)", "EUR"),
        (r"(?:total|amount\s+billed|amount\s+charged|amount\s+paid|billed|charged|payment\s+of|subtotal|recurring\s+charge|order\s+total|montant\s*(?:pr[eé]lev[eé]|pay[eé]|factur[eé])?|betrag|importe|pago)[\s:]*(\d+(?:\.\d{3})*(?:[.,]\d{2})?)\s*(?:[€]|\bEUR\b)", "EUR"),
        # GBP (£)
        (r"(?:total|amount\s+billed|amount\s+charged|amount\s+paid|billed|charged|payment\s+of|subtotal|recurring\s+charge|order\s+total)[\s:]*(?:[£]|\bGBP\b)\s*(\d+(?:,\d{3})*(?:\.\d{2})?)", "GBP"),
        # CAD
        (r"(?:total|amount\s+billed|amount\s+charged|amount\s+paid|billed|charged|payment\s+of|subtotal)[\s:]*(?:CAD|C\$)\s*(\d+(?:,\d{3})*(?:\.\d{2})?)", "CAD"),
        # AUD
        (r"(?:total|amount\s+billed|amount\s+charged|amount\s+paid|billed|charged|payment\s+of|subtotal)[\s:]*(?:AUD|A\$)\s*(\d+(?:,\d{3})*(?:\.\d{2})?)", "AUD"),
        # JPY (¥)
        (r"(?:total|amount\s+billed|amount\s+charged|amount\s+paid|billed|charged|payment\s+of|subtotal)[\s:]*(?:[¥]|\bJPY\b)\s*(\d+(?:,\d{3})*)", "JPY"),
        # Amount followed by currency code e.g. "Total: 1499.00 INR" or "Total: 14.99 USD" or "14,99 EUR"
        (r"(?:total|amount\s+billed|amount\s+charged|amount\s+paid|billed|charged|payment\s+of|subtotal)[\s:]*(\d+(?:[.,]\d{2})?)\s*(USD|EUR|GBP|CAD|AUD|INR|JPY)\b", None),
        # Generic "Total: 14.99" without currency symbol -> preserve currency as None
        (r"(?:total|amount\s+billed|amount\s+charged|amount\s+paid|billed|charged|payment\s+of)[\s:]*(\d+(?:\.\d{2})?)", None),
    ]

    zero_match: Optional[Tuple[float, Optional[str]]] = None
    for pattern, curr in priority_patterns:
        m = re.search(pattern, clean_text, re.IGNORECASE)
        if m:
            if curr is None and m.lastindex >= 2:
                curr = m.group(2).upper()
            val_raw = m.group(1).replace("/-", "").strip()
            if curr == "EUR" and "," in val_raw:
                val_str = val_raw.replace(".", "").replace(",", ".")
            else:
                val_str = val_raw.replace(",", "")
            try:
                parsed_val = float(val_str)
                if parsed_val > 0:
                    return parsed_val, curr
                elif zero_match is None:
                    zero_match = (0.0, curr)
            except ValueError:
                pass

    # Priority 2: Standalone currency patterns with STRICT billing context requirement
    # Context words that MUST appear within 60 characters of a standalone price
    billing_ctx_re = re.compile(
        r"(?:price|cost|fee|charge|billed|total|amount|pay|paid|rate|renew|subscription|order|subtotal|membership|per\s+(?:month|mo|year|yr)|/month|/mo|/year|/yr|montant|pr[eé]lev[eé]|factur|betrag|par\s+mois|mois|pro\s+monat)",
        re.IGNORECASE,
    )

    def _has_billing_context(match_start: int, match_end: int) -> bool:
        # If the input text is a standalone amount snippet or line under 35 chars, allow it
        stripped = clean_text.strip()
        if len(stripped) <= 35:
            return True
        start_ctx = max(0, match_start - 60)
        end_ctx = min(len(clean_text), match_end + 60)
        window = clean_text[start_ctx:end_ctx]
        return bool(billing_ctx_re.search(window))

    # INR
    for inr_m in re.finditer(r"(?:[₹\u20b9]|\bINR\b|\bRs\.?|\bRs\b)\s*(\d+(?:,\d{2,3})*(?:\.\d{2})?)(?:\s*/-)?", clean_text, re.IGNORECASE):
        if _has_billing_context(inr_m.start(), inr_m.end()):
            try:
                amt = float(inr_m.group(1).replace(",", ""))
                if amt > 0:
                    return amt, "INR"
            except ValueError:
                pass

    # USD
    for usd_m in re.finditer(r"(?:\$|\bUSD\b)\s*(\d+(?:,\d{3})*(?:\.\d{2})?)", clean_text, re.IGNORECASE):
        if _has_billing_context(usd_m.start(), usd_m.end()):
            try:
                amt = float(usd_m.group(1).replace(",", ""))
                if amt > 0:
                    return amt, "USD"
            except ValueError:
                pass

    # EUR (both prefix €14,99 and suffix 14,99 €)
    for eur_m in re.finditer(r"(?:(?:€|\bEUR\b)\s*(\d+(?:\.\d{3})*(?:[.,]\d{2})?)|(\d+(?:\.\d{3})*(?:[.,]\d{2})?)\s*(?:€|\bEUR\b))", clean_text, re.IGNORECASE):
        if _has_billing_context(eur_m.start(), eur_m.end()):
            val_raw = eur_m.group(1) or eur_m.group(2)
            val_str = val_raw.replace(".", "").replace(",", ".")
            try:
                amt = float(val_str)
                if amt > 0:
                    return amt, "EUR"
            except ValueError:
                pass

    # GBP
    for gbp_m in re.finditer(r"(?:£|\bGBP\b)\s*(\d+(?:,\d{3})*(?:\.\d{2})?)", clean_text, re.IGNORECASE):
        if _has_billing_context(gbp_m.start(), gbp_m.end()):
            try:
                amt = float(gbp_m.group(1).replace(",", ""))
                if amt > 0:
                    return amt, "GBP"
            except ValueError:
                pass

    # CAD
    for cad_m in re.finditer(r"(?:\bCAD\b|C\$)\s*(\d+(?:,\d{3})*(?:\.\d{2})?)", clean_text, re.IGNORECASE):
        if _has_billing_context(cad_m.start(), cad_m.end()):
            try:
                amt = float(cad_m.group(1).replace(",", ""))
                if amt > 0:
                    return amt, "CAD"
            except ValueError:
                pass

    # AUD
    for aud_m in re.finditer(r"(?:\bAUD\b|A\$)\s*(\d+(?:,\d{3})*(?:\.\d{2})?)", clean_text, re.IGNORECASE):
        if _has_billing_context(aud_m.start(), aud_m.end()):
            try:
                amt = float(aud_m.group(1).replace(",", ""))
                if amt > 0:
                    return amt, "AUD"
            except ValueError:
                pass

    # Return zero total if explicit total: 0 was found and no other positive price existed
    if zero_match is not None:
        return zero_match

    return None, None


def extract_billing_interval(text: str) -> Optional[str]:
    """Detect whether subscription is monthly, annual, quarterly, or weekly.

    Returns None when the email text does not explicitly specify an interval,
    preserving honest unknown state rather than inventing assumptions.
    """
    lower = text.lower()
    if any(k in lower for k in [
        "/year", "/yr", "per year", "annual", "annually", "yearly", "billed annually",
        "12 months", "1 year", "par an", "annuel", "pro jahr", "jährlich", "al año", "anual"
    ]):
        return "annual"
    if any(k in lower for k in ["quarterly", "/quarter", "every 3 months", "3 months", "trimestriel"]):
        return "quarterly"
    if any(k in lower for k in ["weekly", "/week", "per week", "every week", "hebdomadaire"]):
        return "weekly"
    if any(k in lower for k in [
        "/month", "/mo", "per month", "monthly", "billed monthly", "every month", "each month",
        "1 month", "par mois", "mensuel", "pro monat", "monatlich", "al mes", "mensual"
    ]):
        return "monthly"
    return None


def detect_cancellation_signal(text: str) -> bool:
    """Check if message represents a subscription cancellation or refund confirmation."""
    lower = text.lower()
    return any(re.search(sig, lower) for sig in CANCELLATION_SIGNALS)


def extract_dates(text: str) -> Tuple[Optional[date], Optional[date], Optional[date]]:
    """Extract (renewal_date, trial_end_date, purchase_date) from email body.

    Distinguishes future renewal/next-billing dates and trial end dates from past purchase dates.
    Never falls back to arbitrary transaction dates as renewal dates.
    """
    if not text:
        return None, None, None

    clean_text = re.sub(r"https?://\S+", " ", text)

    month_names = {
        "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
        "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
        # French
        "janvier": 1, "fevrier": 2, "février": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
        "juillet": 7, "aout": 8, "août": 8, "septembre": 9, "octobre": 10, "novembre": 11,
        "decembre": 12, "décembre": 12,
        # German
        "januar": 1, "februar": 2, "märz": 3, "maerz": 3, "oktober": 10, "dezember": 12,
        # Spanish
        "enero": 1, "febrero": 2, "marzo": 3, "mayo": 5, "junio": 6, "agosto": 8,
        "septiembre": 9, "noviembre": 11, "diciembre": 12,
    }
    month_regex = "|".join(month_names.keys())

    def _parse_date_string(s: str) -> Optional[date]:
        if not s:
            return None
        # 1. ISO YYYY-MM-DD
        iso_m = re.search(r"\b(202[4-9])-([01]\d)-([0-3]\d)\b", s)
        if iso_m:
            try:
                return date(int(iso_m.group(1)), int(iso_m.group(2)), int(iso_m.group(3)))
            except ValueError:
                pass

        # 2. Month DD, YYYY (e.g., November 15, 2026 or Nov 15th, 2026)
        us_m = re.search(r"\b(" + month_regex + r")\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(202[4-9])\b", s, re.IGNORECASE)
        if us_m:
            try:
                m = month_names[us_m.group(1).lower()]
                d = int(us_m.group(2))
                y = int(us_m.group(3))
                return date(y, m, d)
            except (KeyError, ValueError):
                pass

        # 3. DD Month YYYY (e.g., 15 November 2026 or 15th of Nov, 2026)
        uk_m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(" + month_regex + r"),?\s+(202[4-9])\b", s, re.IGNORECASE)
        if uk_m:
            try:
                d = int(uk_m.group(1))
                m = month_names[uk_m.group(2).lower()]
                y = int(uk_m.group(3))
                return date(y, m, d)
            except (KeyError, ValueError):
                pass

        # 4. YYYY/MM/DD
        slash_m = re.search(r"\b(202[4-9])/([01]?\d)/([0-3]?\d)\b", s)
        if slash_m:
            try:
                return date(int(slash_m.group(1)), int(slash_m.group(2)), int(slash_m.group(3)))
            except ValueError:
                pass

        return None

    # Step A: Trial expiration context
    trial_end_date: Optional[date] = None
    trial_context_pattern = (
        r"(?:trial\s+(?:will\s+)?end\s+(?:on)?|trial\s+ends\s+(?:on)?|trial\s+ending\s+(?:on)?|end\s+of\s+your\s+trial\s+(?:on)?|"
        r"free\s+trial\s+(?:valid\s+)?until|starting\s+(?:on\s+)?([^\n\r<]{1,40}?),\s+you'll\s+be\s+automatically\s+charged)[\s:]+([^\n\r<]{1,60})"
    )
    t_m = re.search(trial_context_pattern, clean_text, re.IGNORECASE)
    if t_m:
        trial_str = t_m.group(1) if t_m.group(1) else t_m.group(2)
        trial_end_date = _parse_date_string(trial_str)

    # Also check starting on <Date>, you'll be charged
    if not trial_end_date:
        start_m = re.search(r"starting\s+(?:on\s+)?(" + month_regex + r"\s+\d{1,2},?\s+202[4-9]|\d{1,2}\s+" + month_regex + r"\s+202[4-9]|202[4-9]-[01]\d-[0-3]\d)", clean_text, re.IGNORECASE)
        if start_m:
            trial_end_date = _parse_date_string(start_m.group(1))

    # Step B: Renewal / Next billing date context
    renewal_date: Optional[date] = None
    renewal_context_pattern = (
        r"(?:next\s+(?:billing|payment|renewal|charge|invoice)\s*(?:date|on)?|"
        r"renews?\s*(?:on)?|renewing\s*(?:on)?|renewal\s*(?:date|on)?|"
        r"will\s*renew\s*(?:on)?|valid\s*until|access\s*until|ends\s*on|"
        r"date\s+de\s+facturation|prochaine\s+(?:date\s+de\s+)?facturation|"
        r"nächste\s+abrechnung|fecha\s+de\s+facturación|fecha\s+de\s+facturacion)[\s:]+([^\n\r<]{1,60})"
    )
    renewal_ctx = re.search(renewal_context_pattern, clean_text, re.IGNORECASE)
    if renewal_ctx:
        renewal_date = _parse_date_string(renewal_ctx.group(1))

    # If trial_end_date was found and no separate renewal_date was found, the trial end date is the renewal date
    if not renewal_date and trial_end_date:
        renewal_date = trial_end_date

    # Step C: Purchase / Order date context
    purchase_date: Optional[date] = None
    purchase_pattern = (
        r"(?:order\s+date|date\s+of\s+purchase|purchase\s+date|payment\s+date|billed\s+on|transaction\s+date)[\s:]+([^\n\r<]{1,60})"
    )
    p_ctx = re.search(purchase_pattern, clean_text, re.IGNORECASE)
    if p_ctx:
        purchase_date = _parse_date_string(p_ctx.group(1))

    return renewal_date, trial_end_date, purchase_date


def classify_irrelevant_email(
    subject: str,
    sender: str,
    body: str,
) -> Optional[Tuple[str, str]]:
    """Determine if an email is genuinely irrelevant to subscription tracking.

    Returns (skip_reason, safe_details) if irrelevant, or None if potentially relevant.
    """
    clean_sub = subject.lower()
    clean_send = sender.lower()
    clean_body = body.lower()

    # 1. GitHub Activity Notifications (issues, PRs, comments, releases, workflows)
    is_github_sender = "notifications@github.com" in clean_send or "noreply@github.com" in clean_send
    is_github_subject = clean_sub.startswith("[github]") or clean_sub.startswith("re: [") or any(
        k in clean_sub for k in [
            "commented on issue", "opened pull request", "closed issue",
            "merged pull request", "review requested", "mentioned you",
            "run failed", "run succeeded", "assigned you"
        ]
    )
    is_github_billing = (
        any(k in clean_sub for k in ["payment receipt", "payment confirmation", "invoice", "receipt #", "copilot subscription receipt"])
        or "billing@github.com" in clean_send
    )
    if (is_github_sender or is_github_subject) and not is_github_billing:
        return ("github_activity_notification", "GitHub notification or repository activity")

    # 2. Account Security & Verification
    security_patterns = [
        "security alert", "new sign-in", "new login", "verification code",
        "one-time passcode", "one-time password", "password reset", "reset your password",
        "two-factor authentication", "confirm your email", "authorize this device",
        "login attempt", "verify your account", "access code", "passcode is"
    ]
    if any(p in clean_sub for p in security_patterns):
        if not any(c in clean_sub for c in ["cancellation", "cancelled", "canceled"]):
            return ("account_security_or_auth", "Account security or verification email")

    # 3. Newsletters & Editorial Digests
    newsletter_senders = ["newsletter@", "newsletters@", "digest@", "editorial@", "dailybriefing@", "weekly@", "roundup@"]
    newsletter_sub_patterns = [
        "daily digest", "weekly digest", "weekly briefing", "top stories",
        "today's highlights", "curated stories", "front page", "the daily",
        "weekend edition", "evening edition", "morning briefing", "daily newsletter"
    ]
    is_newsletter = any(ns in clean_send for ns in newsletter_senders) or any(np in clean_sub for np in newsletter_sub_patterns)
    has_receipt_signal = any(r in clean_sub for r in ["receipt", "invoice", "payment", "renewal confirmation", "billed", "order receipt", "order confirmation"])
    if is_newsletter and not has_receipt_signal:
        return ("newsletter_or_digest", "Newsletter, editorial digest, or daily briefing")

    # 4. Platform Terms of Service, Privacy, or Settings Reminders (e.g. Google Play quarterly reminders)
    policy_patterns = [
        "terms of service", "terms and policies", "terms and policy",
        "privacy settings work", "privacy policy update", "changes to your settings",
        "purchase verification setting", "changes to purchase verification", "play games profile"
    ]
    if any(pp in clean_sub for pp in policy_patterns):
        return ("terms_or_policy_update", "Platform terms, privacy, or policy update reminder")

    # 5. Marketing & Promotional Offers / Rewards
    marketing_patterns = [
        "save 50%", "save 20%", "save 30%", "save 40%", "save up to", "special discount",
        "limited time offer", "upgrade today", "exclusive deal", "try premium free",
        "try 30 days free", "start your free trial", "start free trial",
        "black friday", "cyber monday", "holiday deal", "coupon code", "promo code",
        "don't miss out", "unlock unlimited", "introducing our new plan", "upgrade now",
        "take 50% off", "flash sale", "microsoft rewards", "win a subscription",
        "gifted you a free", "enjoy your free", "free diamond membership",
        "subscription at ₹", "subscription at rs", "save with a udem",
        "a great start to career", "a full year of career", "last two days to win",
        "explore your new google ai plus plan", "action required: please confirm your subscription",
        "personal plan savings", "chance at free tickets", "free tickets & prizes",
        "want to save big", "save big on your", "how to save big",
    ]
    is_marketing_sub = any(mp in clean_sub for mp in marketing_patterns)
    is_promo_sender = any(ps in clean_send for ps in ["promotions@", "marketing@", "deals@", "offers@"])
    is_abandon_cart = "abandon_cart" in clean_body or "your subscription is waiting" in clean_sub
    has_paid_transaction = (
        any(t in clean_sub for t in ["receipt", "invoice", "payment confirmation", "payment received", "renewal confirmation", "order receipt", "your order confirmation for"])
        or any(t in clean_body[:500] for t in [
            "your payment was successful", "payment of", "thank you for your payment",
            "we charged your", "was billed to your", "order confirmation #", "signed up for a trial subscription",
            "successfully subscribed to"
        ])
    )
    if (is_marketing_sub or is_promo_sender or is_abandon_cart) and not has_paid_transaction:
        return ("marketing_promotional", "Marketing offer, rewards promotion, or educational discount campaign")

    # 6. General Platform Pricing Announcements without receipts
    announcement_patterns = [
        "billing adjustment announcement", "api new peak/off-peak", "official release schedule and billing", "api billing"
    ]
    if any(ap in clean_sub for ap in announcement_patterns):
        return ("newsletter_or_digest", "Platform pricing or schedule announcement")

    # 7. Shipping & Physical Delivery Updates
    shipping_patterns = [
        "your package has shipped", "has shipped", "out for delivery", "delivered: ",
        "tracking number", "carrier:", "track your package", "shipment update",
        "package delivered", "arriving today", "arriving tomorrow", "order confirmation."
    ]
    if any(sp in clean_sub for sp in shipping_patterns):
        # Unless it explicitly states it is a digital software/streaming service
        if not any(ds in clean_sub for ds in ["premium", "software", "license", "cloud"]):
            return ("shipping_tracking", "Physical order delivery or shipment tracking notification")

    # 8. One-time non-recurring delivery/rides (Uber trip, DoorDash food delivery, etc.)
    onetime_patterns = [
        "your uber trip", "your trip with uber", "your ride with lyft",
        "doordash order", "instacart delivery", "grubhub order", "uber eats order"
    ]
    if any(op in clean_sub for op in onetime_patterns):
        return ("one_time_transaction", "One-time food delivery or rideshare receipt")

    return None


def evaluate_billing_email(
    message_id: str,
    subject: str,
    sender: str,
    raw_content: str,
    received_at: Optional[datetime] = None,
    user_id: str = "user_default",
    account_id: Optional[str] = None,
    account_email: Optional[str] = None,
) -> EmailParseResult:
    """Evaluate a single email message with comprehensive skip and failure classification."""
    try:
        clean_text = sanitize_email_content(raw_content)
        clean_subject = sanitize_email_content(subject)
        # Sanitize sender header safely without stripping email angle brackets e.g. <user@domain.com>
        clean_sender = re.sub(r"(?is)<script.*?>.*?</script>", " ", sender or "")
        clean_sender = re.sub(r"(?is)<iframe.*?>.*?</iframe>", " ", clean_sender)
        clean_sender = html.unescape(clean_sender).strip()

        # Step 1: Check for known irrelevant categories
        skip_classification = classify_irrelevant_email(clean_subject, clean_sender, clean_text)
        if skip_classification:
            reason, details = skip_classification
            return EmailParseResult(status="skipped", reason=reason, details=details)

        # Step 2: Check for broad billing relevance keywords
        billing_keywords = [
            "receipt", "invoice", "subscription", "billed", "payment",
            "renewal", "membership", "plan", "trial", "cancelled", "canceled",
            "charge", "order confirmation", "autopay", "auto-renewal",
            "recurring", "credit card", "tax invoice", "account renewed",
            "protection has ended", "protection ended", "protection expired", "protection is ending",
            # Multilingual
            "facture", "facturation", "recu", "reçu", "abonnement", "paiement",
            "prelevement", "prélèvement", "renouvellement", "rechnung", "quittung",
            "zahlung", "recibo", "suscripcion", "suscripción", "pago"
        ]
        relevance_text = f"{clean_subject} {clean_sender} {clean_text[:600]}".lower()
        if not any(k in relevance_text for k in billing_keywords):
            return EmailParseResult(
                status="skipped",
                reason="not_billing_related",
                details="No billing, payment, or subscription keywords detected",
            )

        # Step 3: Identify merchant provider
        merchant, category = extract_merchant(clean_subject, clean_sender, clean_text)
        if not merchant:
            return EmailParseResult(
                status="skipped",
                reason="no_merchant_detected",
                details="Billing keywords present but provider/merchant could not be identified",
            )

        # Step 4: Extract billing facts
        amount, currency = extract_monetary_amount(clean_text)
        interval = extract_billing_interval(clean_text)
        renewal_date, trial_end_date, purchase_date = extract_dates(clean_text)
        is_cancelled = detect_cancellation_signal(f"{clean_subject} {clean_text}")

        # Check for trial, payment failure, refund, downgrade, or expiration signals
        is_trial = bool(re.search(r"signed\s+up\s+for\s+a\s+trial|trial\s+subscription|trial\s+will\s+end|with\s+trial|3-month\s+offer", f"{clean_subject} {clean_text}", re.IGNORECASE))
        is_refund = bool(re.search(r"refund\s+has\s+been\s+approved|refund\s+(?:has\s+been\s+|was\s+)?processed|refund\s+confirmation|your\s+refund\s+request|refund\s+approved", f"{clean_subject} {clean_text}", re.IGNORECASE)) or ("refund" in clean_subject.lower() and "approved" in clean_subject.lower())
        is_expired = bool(re.search(r"protection\s+has\s+ended|protection\s+has\s+expired|subscription\s+has\s+expired|subscription\s+has\s+ended|membership\s+has\s+ended", f"{clean_subject} {clean_text}", re.IGNORECASE))
        is_downgrade = bool(re.search(r"you're\s+no\s+longer\s+on|no\s+longer\s+on\s+standard|downgraded\s+to\s+free|now\s+on\s+spotify's\s+free", f"{clean_subject} {clean_text}", re.IGNORECASE))
        is_payment_failed = bool(re.search(r"payment\s+failed|unable\s+to\s+process|could\s+not\s+be\s+processed|failed\s+payment|update\s+your\s+(?:plus\s+)?payment\s+method", f"{clean_subject} {clean_text}", re.IGNORECASE)) or is_downgrade

        # Determine evidence category and evidence-backed lifecycle status
        if is_refund:
            evidence_cat = EmailEvidenceCategory.REFUND
            lifecycle = SubscriptionStatus.REFUNDED.value
            is_cancelled = True
        elif is_downgrade:
            evidence_cat = EmailEvidenceCategory.CANCELLATION_OR_EXPIRATION
            lifecycle = SubscriptionStatus.PAYMENT_FAILED.value
            is_cancelled = True
        elif is_expired:
            evidence_cat = EmailEvidenceCategory.CANCELLATION_OR_EXPIRATION
            lifecycle = SubscriptionStatus.EXPIRED.value
            is_cancelled = True
        elif is_cancelled:
            evidence_cat = EmailEvidenceCategory.CANCELLATION_OR_EXPIRATION
            lifecycle = SubscriptionStatus.CANCELLED.value
        elif is_payment_failed:
            evidence_cat = EmailEvidenceCategory.PAYMENT_FAILURE
            lifecycle = SubscriptionStatus.PAYMENT_FAILED.value
        elif is_trial:
            evidence_cat = EmailEvidenceCategory.TRIAL_OR_CREATION
            lifecycle = SubscriptionStatus.TRIAL.value
        elif renewal_date and renewal_date >= date.today():
            evidence_cat = EmailEvidenceCategory.UPCOMING_RENEWAL
            lifecycle = SubscriptionStatus.ACTIVE.value
        elif any(k in clean_subject.lower() for k in ["upcoming changes", "upcoming renewal", "auto-renewal", "renewal notice"]) or any(k in clean_text[:400].lower() for k in ["automatically renew", "automatic renewal"]):
            evidence_cat = EmailEvidenceCategory.UPCOMING_RENEWAL
            lifecycle = SubscriptionStatus.ACTIVE.value
        elif amount is not None:
            evidence_cat = EmailEvidenceCategory.CONFIRMED_BILLING
            lifecycle = SubscriptionStatus.ACTIVE.value
        else:
            evidence_cat = EmailEvidenceCategory.CONFIRMED_BILLING
            lifecycle = SubscriptionStatus.UNKNOWN.value

        # Step 5: Verify substantive billing evidence to prevent false positives from brand mentions
        has_authoritative_billing = (
            amount is not None
            or renewal_date is not None
            or trial_end_date is not None
            or is_cancelled
            or is_trial
            or is_payment_failed
            or is_refund
            or is_expired
            or is_downgrade
            or any(k in clean_subject.lower() for k in [
                "receipt", "invoice", "tax invoice", "payment confirmation", "order receipt",
                "order confirmation", "was renewed", "renewed", "renewal confirmation", "subscription renewed",
                "upcoming changes to your", "upcoming changes"
            ])
            or any(k in clean_text[:400].lower() for k in [
                "has successfully renewed", "has been renewed", "successfully renewed",
                "automatically renew", "will renew", "automatic renewal"
            ])
        )
        if not has_authoritative_billing:
            return EmailParseResult(
                status="skipped",
                reason="insufficient_billing_evidence",
                details="Brand recognized but message lacks payment, receipt, or renewal evidence",
            )

        # Step 6: Evidence-based confidence scoring
        # Base confidence for verified billing intent + recognized merchant
        confidence = 0.55
        if amount is not None:
            confidence += 0.15
        if renewal_date is not None or is_cancelled or is_refund:
            confidence += 0.15
        if interval is not None:
            confidence += 0.05

        _, _, sender_domain = extract_sender_info(clean_sender)
        if sender_domain and (merchant.lower() in sender_domain.lower() or "googleplay" in sender_domain.lower()):
            confidence += 0.05

        if is_payment_failed or lifecycle in (SubscriptionStatus.EXPIRED.value, SubscriptionStatus.PAYMENT_FAILED.value):
            confidence = min(0.65, confidence)

        confidence = min(0.95, round(confidence, 2))

        clean_uid = re.sub(r"[^a-zA-Z0-9]", "_", user_id).lower()
        clean_acct = re.sub(r"[^a-zA-Z0-9]", "_", account_id or "default").lower()
        clean_merchant = re.sub(r"[^a-zA-Z0-9]", "_", merchant).lower()
        import hashlib
        msg_hash = hashlib.sha256(message_id.encode()).hexdigest()[:8]
        candidate_id = f"cand_{clean_uid}_{clean_acct}_{clean_merchant}_{msg_hash}"

        snippet = clean_text[:250] + ("…" if len(clean_text) > 250 else "")

        # Extract authentic product name
        resolved_product = extract_product_name(clean_subject, clean_sender, clean_text, merchant, category)

        notes_desc = f"Detected from billing email: {clean_subject[:100]}"
        if is_refund:
            notes_desc += " (Refund confirmed - subscription terminated)"
        elif is_trial:
            notes_desc += f" (Trial enrollment - recurring subscription after {trial_end_date or 'trial'})"
        elif is_payment_failed:
            notes_desc += " (Payment failure notice - verify account status)"
        elif is_downgrade:
            notes_desc += " (Payment failure/downgrade notice - verify account status)"
        elif is_expired:
            notes_desc += " (Protection/subscription ended notice)"

        cancellation_date = None
        if is_refund or is_cancelled or is_expired or is_downgrade:
            cancellation_date = received_at.date() if received_at else date.today()

        cand = SubscriptionCandidate(
            candidate_id=candidate_id,
            user_id=user_id,
            account_id=account_id,
            account_email=account_email,
            merchant_name=merchant,
            product_name=resolved_product,
            estimated_cost=amount,
            currency=currency if amount is not None else None,
            original_amount=amount,
            original_currency=currency if amount is not None else None,
            billing_interval=interval or "monthly",
            detected_renewal_date=renewal_date,
            detected_last_used=None,  # Email receipts establish billing, never user streaming/usage
            review_status=CandidateReviewStatus.PENDING,
            confidence=confidence,
            source_message_id=message_id,
            source_subject=clean_subject[:200],
            source_sender=clean_sender[:100],
            source_date=received_at or datetime.now(timezone.utc),
            email_snippet=snippet,
            cancellation_signal=is_cancelled,
            notes=notes_desc,
            evidence_category=evidence_cat.value,
            lifecycle_status=lifecycle,
            trial_end_date=trial_end_date,
            purchase_date=purchase_date,
            cancellation_date=cancellation_date,
            last_payment_date=purchase_date,
            created_at=datetime.now(timezone.utc),
        )
        return EmailParseResult(status="candidate", candidate=cand)

    except Exception as exc:
        return EmailParseResult(
            status="failed",
            reason="parse_exception",
            details="Internal parser exception during email evaluation",
        )


def parse_billing_email(
    message_id: str,
    subject: str,
    sender: str,
    raw_content: str,
    received_at: Optional[datetime] = None,
    user_id: str = "user_default",
    account_id: Optional[str] = None,
    account_email: Optional[str] = None,
) -> Optional[SubscriptionCandidate]:
    """Parse a single email message into a candidate subscription if billing evidence found."""
    result = evaluate_billing_email(
        message_id=message_id,
        subject=subject,
        sender=sender,
        raw_content=raw_content,
        received_at=received_at,
        user_id=user_id,
        account_id=account_id,
        account_email=account_email,
    )
    return result.candidate
