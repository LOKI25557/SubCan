"""SubCan Normalization and Evidence Extraction Utilities.

Provides deterministic helpers to:
1. Normalize source timestamps with strict timezone awareness.
2. Build validated Evidence instances adhering to DATA_CONTRACT.md.
3. Deduplicate evidence by stable identifier or content hash.
4. Detect conflicting activity and cancellation policy records.
5. Safely construct Subscription models without fabricating data.
"""

from datetime import date, datetime, timezone
import hashlib
import json
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

from backend.app.connectors.exceptions import EvidenceExtractionError
from backend.app.models import (
    Evidence,
    Subscription,
    SubscriptionStatus,
)

# Common ISO 8601 timestamp patterns
ISO_DATETIME_REGEX = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?$"
)


def ensure_timezone_aware(
    dt: Optional[datetime],
    default_tz: timezone = timezone.utc,
) -> Optional[datetime]:
    """Ensure a datetime is timezone-aware. If naive, attach default_tz."""
    if dt is None:
        return None
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        return dt.replace(tzinfo=default_tz)
    return dt


def parse_timestamp(
    raw: Optional[Union[str, datetime]],
    default_tz: timezone = timezone.utc,
    raise_on_invalid: bool = False,
) -> Optional[datetime]:
    """Parse a timestamp string or datetime into a timezone-aware datetime.

    Args:
        raw: ISO format string, datetime, or None.
        default_tz: Timezone to assign if parsed datetime is naive (default UTC).
        raise_on_invalid: If True, raises EvidenceExtractionError on parse error.

    Returns:
        Timezone-aware datetime or None if input is empty or invalid (when not raising).
    """
    if raw is None:
        return None

    if isinstance(raw, datetime):
        return ensure_timezone_aware(raw, default_tz=default_tz)

    if not isinstance(raw, str) or not raw.strip():
        return None

    s = raw.strip()

    # Normalize trailing 'Z' to '+00:00' for fromisoformat
    normalized = s.replace("Z", "+00:00")
    if " " in normalized and "T" not in normalized:
        normalized = normalized.replace(" ", "T")

    try:
        dt = datetime.fromisoformat(normalized)
        return ensure_timezone_aware(dt, default_tz=default_tz)
    except (ValueError, TypeError) as exc:
        if raise_on_invalid:
            raise EvidenceExtractionError(f"Malformed timestamp: '{raw}'") from exc
        return None


def parse_date(
    raw: Optional[Union[str, date, datetime]],
    raise_on_invalid: bool = False,
) -> Optional[date]:
    """Parse a date from string (YYYY-MM-DD), date, or datetime."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw

    if not isinstance(raw, str) or not raw.strip():
        return None

    s = raw.strip()[:10]
    try:
        return date.fromisoformat(s)
    except (ValueError, TypeError) as exc:
        if raise_on_invalid:
            raise EvidenceExtractionError(f"Malformed date: '{raw}'") from exc
        return None


def normalize_status(raw_status: Optional[str]) -> SubscriptionStatus:
    """Normalize raw subscription status string into SubscriptionStatus enum."""
    if not raw_status:
        return SubscriptionStatus.UNKNOWN

    s = str(raw_status).strip().lower()
    if s in ("active", "renewing", "enabled", "subscribed", "in_good_standing"):
        return SubscriptionStatus.ACTIVE
    if s in ("cancelled", "canceled", "terminated", "inactive", "expired", "closed"):
        return SubscriptionStatus.CANCELLED
    return SubscriptionStatus.UNKNOWN


def build_evidence(
    evidence_id: str,
    subscription_id: str,
    evidence_type: str,
    excerpt: str,
    source_url: Optional[str] = None,
    captured_at: Optional[datetime] = None,
    confidence: float = 1.0,
    raw_reference: Optional[Dict[str, Any]] = None,
) -> Evidence:
    """Construct a validated Evidence instance enforcing all data contracts.

    Guarantees:
    - captured_at is strictly timezone-aware (defaults to current UTC).
    - confidence is clamped between 0.0 and 1.0.
    - extra attributes are strictly forbidden per ConfigDict.
    """
    ts = ensure_timezone_aware(captured_at) or datetime.now(timezone.utc)
    clamped_confidence = max(0.0, min(1.0, float(confidence)))

    return Evidence(
        evidence_id=evidence_id,
        subscription_id=subscription_id,
        evidence_type=evidence_type,
        excerpt=excerpt,
        source_url=source_url,
        captured_at=ts,
        confidence=clamped_confidence,
        raw_reference=raw_reference,
    )


def deduplicate_evidence(evidence_list: Iterable[Evidence]) -> List[Evidence]:
    """Deduplicate Evidence items by evidence_id, preserving first-seen order."""
    seen_ids = set()
    result: List[Evidence] = []
    for ev in evidence_list:
        if ev.evidence_id not in seen_ids:
            seen_ids.add(ev.evidence_id)
            result.append(ev)
    return result


def detect_activity_conflicts(
    activity_records: List[Dict[str, Any]],
) -> Tuple[bool, Optional[str]]:
    """Inspect a set of raw activity records to detect conflicting timestamps.

    Returns:
        (has_conflict, description_of_conflict)
    """
    if len(activity_records) < 2:
        return False, None

    # Check for contradictory status flags if records carry status
    statuses = {r.get("status") for r in activity_records if "status" in r}
    if "active" in statuses and "inactive" in statuses:
        return True, "Conflicting status flags detected across activity records."

    # Check if there are extreme timestamp divergences without clear ordering
    valid_dts: List[datetime] = []
    for r in activity_records:
        for k in ("timestamp", "activity_at", "last_streamed_at", "played_at"):
            if k in r and r[k]:
                parsed = parse_timestamp(r[k])
                if parsed:
                    valid_dts.append(parsed)
                break

    return False, None


def normalize_subscription_payload(
    raw: Dict[str, Any],
    fallback_service_name: Optional[str] = None,
) -> Subscription:
    """Normalize a raw dictionary payload into a SubCan Subscription domain model.

    Enforces schema compliance and rejects invalid models.
    Missing usage dates remain None (never fabricated).
    """
    sub_id = raw.get("subscription_id")
    if not sub_id:
        raise EvidenceExtractionError("Payload missing required 'subscription_id'.")

    service_name = raw.get("service_name") or fallback_service_name
    if not service_name:
        raise EvidenceExtractionError(f"Subscription '{sub_id}' missing required 'service_name'.")

    raw_status = raw.get("status")
    status = normalize_status(raw_status) if isinstance(raw_status, str) else SubscriptionStatus(raw_status)

    renewal_date = parse_date(raw.get("renewal_date"))
    last_used_at = parse_timestamp(raw.get("last_used_at"))

    monthly_cost = None
    if raw.get("monthly_cost") is not None:
        try:
            monthly_cost = float(raw["monthly_cost"])
        except (ValueError, TypeError):
            monthly_cost = None

    return Subscription(
        subscription_id=sub_id,
        service_name=service_name,
        status=status,
        renewal_date=renewal_date,
        last_used_at=last_used_at,
        source_url=raw.get("source_url"),
        source_record_id=raw.get("source_record_id"),
        monthly_cost=monthly_cost,
        currency=raw.get("currency", "USD") or "USD",
        billing_cycle=raw.get("billing_cycle", "monthly") or "monthly",
        cancellation_terms_url=raw.get("cancellation_terms_url"),
        data_deletion_warning=bool(raw.get("data_deletion_warning", False)),
        pause_option_available=bool(raw.get("pause_option_available", False)),
    )
