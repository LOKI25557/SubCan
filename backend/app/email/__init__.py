"""SubCan Email Discovery & Mailbox Synchronization Package.

Public API:
- generate_oauth_url
- exchange_code_for_tokens
- disconnect_account
- sync_user_mailbox
- confirm_candidate_into_inventory
- dismiss_candidate
- parse_billing_email
"""

from backend.app.email.oauth import (
    disconnect_account,
    exchange_code_for_tokens,
    generate_oauth_url,
    get_valid_access_token,
    is_live_oauth_configured,
    refresh_access_token,
)
from backend.app.email.parser import (
    extract_merchant,
    extract_monetary_amount,
    parse_billing_email,
    sanitize_email_content,
)
from backend.app.email.service import (
    confirm_candidate_into_inventory,
    dismiss_candidate,
    group_and_deduplicate_candidates,
    sync_user_mailbox,
)

__all__ = [
    "generate_oauth_url",
    "exchange_code_for_tokens",
    "disconnect_account",
    "is_live_oauth_configured",
    "get_valid_access_token",
    "refresh_access_token",
    "sync_user_mailbox",
    "group_and_deduplicate_candidates",
    "confirm_candidate_into_inventory",
    "dismiss_candidate",
    "parse_billing_email",
    "sanitize_email_content",
    "extract_merchant",
    "extract_monetary_amount",
]
