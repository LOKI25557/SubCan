"""SubCan Decision Rules Module (Phase 1).

Responsible for:
- Deterministic evaluation of subscription cancellation eligibility.
- Enforcing the strict 3 CALENDAR MONTHS inactivity threshold (not a hard-coded 90 days).
- Handling edge cases: month length variations, leap years, year boundaries.
- Safe handling of missing, malformed, stale, future-dated, and contradictory evidence.
- Consequence disclosures (data loss warnings, pause alternatives).
- Preserving safety invariants: candidate status is ONLY eligibility for a proposal,
  NEVER automatic authorization to cancel.
"""

import calendar
from datetime import datetime, timezone
import re
from typing import List, Optional, Tuple
from backend.app.models import (
    Decision,
    EligibilityStatus,
    Evidence,
    Subscription,
    SubscriptionStatus,
)

RULE_VERSION = "1.0.0"

# ISO datetime regex for parsing timestamps embedded in evidence excerpts
ISO_DATETIME_PATTERN = re.compile(
    r"\b(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)\b"
)


def calculate_calendar_months_threshold(as_of: datetime, months: int = 3) -> datetime:
    """Calculate the exact date and time `months` calendar months prior to `as_of`.

    Handles:
    - Month length differences (e.g. 31st to 30th/28th/29th).
    - Leap years (February 29 in leap years vs February 28 in common years).
    - Year boundary transitions (e.g., February minus 3 months -> November of prior year).
    - Preserves timezone, hour, minute, second, and microsecond.

    Args:
        as_of: Timezone-aware reference datetime.
        months: Number of calendar months to subtract (default 3).

    Returns:
        Timezone-aware datetime representing the exact 3-calendar-month boundary.

    Raises:
        ValueError: If as_of is timezone-naive.
    """
    if as_of.tzinfo is None or as_of.tzinfo.utcoffset(as_of) is None:
        raise ValueError("Evaluation timestamp 'as_of' must be timezone-aware.")

    year = as_of.year
    month = as_of.month - months

    while month <= 0:
        month += 12
        year -= 1

    # Clamp the day to the maximum days available in the target year/month
    max_days = calendar.monthrange(year, month)[1]
    day = min(as_of.day, max_days)

    return as_of.replace(year=year, month=month, day=day)


def _extract_activity_timestamp(evidence: Evidence) -> Optional[datetime]:
    """Extract playback or streaming activity timestamp from evidence payload or excerpt.

    Args:
        evidence: Validated Evidence instance.

    Returns:
        Timezone-aware datetime if detected and parseable, otherwise None.
    """
    # 1. Check raw_reference dictionary if connector provided structured fields
    if evidence.raw_reference and isinstance(evidence.raw_reference, dict):
        for key in ("last_streamed_at", "playback_timestamp", "activity_at", "timestamp"):
            val = evidence.raw_reference.get(key)
            if isinstance(val, str):
                try:
                    dt = datetime.fromisoformat(val.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return dt
                except ValueError:
                    pass

    # 2. Extract from excerpt text via regex pattern
    match = ISO_DATETIME_PATTERN.search(evidence.excerpt)
    if match:
        iso_str = match.group(1).replace("Z", "+00:00").replace(" ", "T")
        try:
            dt = datetime.fromisoformat(iso_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            pass

    return None


def _detect_consequences(
    subscription: Subscription,
    evidence_list: List[Evidence],
) -> Tuple[bool, bool, List[str]]:
    """Inspect subscription and evidence for data deletion risks and pause options.

    Returns:
        (data_deletion_risk, pause_alternative_available, consequences_list)
    """
    data_deletion_risk = subscription.data_deletion_warning
    pause_alternative_available = subscription.pause_option_available
    consequences: List[str] = []

    # Check evidence for explicit terms and policies
    for ev in evidence_list:
        excerpt_lower = ev.excerpt.lower()
        if ev.evidence_type in ("cancellation_terms", "terms_of_service"):
            if any(term in excerpt_lower for term in ("purge", "permanently delete", "data loss", "irreversible")):
                data_deletion_risk = True
        elif ev.evidence_type in ("pause_policy", "billing_policy"):
            if any(term in excerpt_lower for term in ("pause", "freeze", "hold", "zero charge")):
                pause_alternative_available = True

    if data_deletion_risk:
        consequences.append(
            "WARNING: Immediate and irreversible data deletion. Cloud library, playlists, and user assets "
            "will be purged immediately upon cancellation."
        )

    if pause_alternative_available:
        consequences.append(
            "ALTERNATIVE AVAILABLE: Provider offers a temporary billing pause/freeze option "
            "without losing account history or legacy rates. A paused subscription is NOT cancelled."
        )

    if subscription.renewal_date:
        consequences.append(
            f"Service access will terminate at the end of the current billing cycle (renewal date: {subscription.renewal_date})."
        )

    return data_deletion_risk, pause_alternative_available, consequences


def evaluate_eligibility(
    subscription: Subscription,
    evidence_list: Optional[List[Evidence]] = None,
    as_of: Optional[datetime] = None,
    rule_version: str = RULE_VERSION,
) -> Decision:
    """Evaluate whether a subscription is eligible for a cancellation proposal.

    SAFETY INVARIANTS:
    1. A 'candidate' decision only indicates eligibility for a cancellation proposal.
       It is NOT authorization to cancel.
    2. Missing usage history is never invented; missing data requires human review.
    3. Inactivity strictly requires being unused for at least three full calendar months.
    4. Conflicting or future-dated evidence requires human review.

    Args:
        subscription: Validated Subscription domain entity.
        evidence_list: Optional list of supporting Evidence entities.
        as_of: Injectable evaluation reference timestamp (defaults to current UTC time).
        rule_version: Rule engine version string (defaults to '1.0.0').

    Returns:
        Validated Decision entity containing eligibility, transparent rationale,
        uncertainty score, consequence disclosures, and rule version.
    """
    if as_of is None:
        as_of = datetime.now(timezone.utc)
    elif as_of.tzinfo is None or as_of.tzinfo.utcoffset(as_of) is None:
        raise ValueError("Evaluation timestamp 'as_of' must be timezone-aware.")

    evidence_items = evidence_list or []
    threshold = calculate_calendar_months_threshold(as_of, months=3)
    decision_id = f"dec_{subscription.subscription_id}_{int(as_of.timestamp())}"

    data_deletion_risk, pause_available, consequences = _detect_consequences(
        subscription, evidence_items
    )

    # --------------------------------------------------------------------------
    # 1. State Check: Already Cancelled
    # --------------------------------------------------------------------------
    if subscription.status == SubscriptionStatus.CANCELLED:
        return Decision(
            decision_id=decision_id,
            subscription_id=subscription.subscription_id,
            eligibility=EligibilityStatus.NOT_ELIGIBLE,
            reason="Subscription is already recorded as cancelled; no cancellation proposal required.",
            uncertainty=0.0,
            rule_version=rule_version,
            evaluated_at=as_of,
            data_deletion_risk=False,
            pause_alternative_available=False,
            consequences=[],
        )

    # --------------------------------------------------------------------------
    # 2. State Check: Unknown Subscription Status
    # --------------------------------------------------------------------------
    if subscription.status == SubscriptionStatus.UNKNOWN:
        return Decision(
            decision_id=decision_id,
            subscription_id=subscription.subscription_id,
            eligibility=EligibilityStatus.NEEDS_REVIEW,
            reason="Subscription status is unknown; requires human review to verify active billing before planning.",
            uncertainty=0.6,
            rule_version=rule_version,
            evaluated_at=as_of,
            data_deletion_risk=data_deletion_risk,
            pause_alternative_available=pause_available,
            consequences=consequences,
        )

    # --------------------------------------------------------------------------
    # 3. Usage Activity Extraction & Future-Dating Check
    # --------------------------------------------------------------------------
    sub_last_used: Optional[datetime] = subscription.last_used_at
    if sub_last_used is not None:
        if sub_last_used.tzinfo is None:
            sub_last_used = sub_last_used.replace(tzinfo=timezone.utc)
        if sub_last_used > as_of:
            return Decision(
                decision_id=decision_id,
                subscription_id=subscription.subscription_id,
                eligibility=EligibilityStatus.NEEDS_REVIEW,
                reason=(
                    f"Future-dated usage detected: subscription last_used_at ({sub_last_used.isoformat()}) "
                    f"is in the future relative to evaluation time ({as_of.isoformat()}). Requires investigation."
                ),
                uncertainty=1.0,
                rule_version=rule_version,
                evaluated_at=as_of,
                data_deletion_risk=data_deletion_risk,
                pause_alternative_available=pause_available,
                consequences=consequences,
            )

    # Extract activity dates from evidence items
    evidence_activities: List[Tuple[Evidence, datetime]] = []
    for ev in evidence_items:
        if ev.evidence_type in ("usage_history", "usage_activity", "playback_logs"):
            act_dt = _extract_activity_timestamp(ev)
            if act_dt is not None:
                if act_dt > as_of:
                    return Decision(
                        decision_id=decision_id,
                        subscription_id=subscription.subscription_id,
                        eligibility=EligibilityStatus.NEEDS_REVIEW,
                        reason=(
                            f"Future-dated evidence detected: evidence '{ev.evidence_id}' reports activity "
                            f"at {act_dt.isoformat()}, which is after evaluation time ({as_of.isoformat()})."
                        ),
                        uncertainty=1.0,
                        rule_version=rule_version,
                        evaluated_at=as_of,
                        data_deletion_risk=data_deletion_risk,
                        pause_alternative_available=pause_available,
                        consequences=consequences,
                    )
                evidence_activities.append((ev, act_dt))

    # --------------------------------------------------------------------------
    # 4. Check for Missing Usage History
    # --------------------------------------------------------------------------
    if sub_last_used is None and not evidence_activities:
        return Decision(
            decision_id=decision_id,
            subscription_id=subscription.subscription_id,
            eligibility=EligibilityStatus.NEEDS_REVIEW,
            reason=(
                "Missing usage history: no last playback date in subscription record and no activity evidence found. "
                "Per safety invariants, missing usage history must never be assumed inactive."
            ),
            uncertainty=1.0,
            rule_version=rule_version,
            evaluated_at=as_of,
            data_deletion_risk=data_deletion_risk,
            pause_alternative_available=pause_available,
            consequences=consequences,
        )

    # --------------------------------------------------------------------------
    # 5. Check for Contradictory Usage Evidence
    # --------------------------------------------------------------------------
    # Determine the most recent activity across subscription and evidence
    all_dates: List[datetime] = []
    if sub_last_used is not None:
        all_dates.append(sub_last_used)

    for ev, act_dt in evidence_activities:
        all_dates.append(act_dt)

    most_recent_activity = max(all_dates)

    # Contradiction: If subscription says inactive (<= threshold), but high-confidence evidence
    # says active (> threshold), or vice-versa.
    if sub_last_used is not None:
        sub_is_inactive = sub_last_used <= threshold
        for ev, act_dt in evidence_activities:
            ev_is_inactive = act_dt <= threshold
            if sub_is_inactive != ev_is_inactive and ev.confidence >= 0.7:
                return Decision(
                    decision_id=decision_id,
                    subscription_id=subscription.subscription_id,
                    eligibility=EligibilityStatus.NEEDS_REVIEW,
                    reason=(
                        f"Contradictory usage evidence: subscription record indicates activity at {sub_last_used.isoformat()} "
                        f"({'inactive' if sub_is_inactive else 'active'}), but evidence '{ev.evidence_id}' indicates activity "
                        f"at {act_dt.isoformat()} ({'inactive' if ev_is_inactive else 'active'}, confidence: {ev.confidence})."
                    ),
                    uncertainty=0.8,
                    rule_version=rule_version,
                    evaluated_at=as_of,
                    data_deletion_risk=data_deletion_risk,
                    pause_alternative_available=pause_available,
                    consequences=consequences,
                )

    # --------------------------------------------------------------------------
    # 6. Evaluation Against 3 Calendar Months Threshold
    # --------------------------------------------------------------------------
    is_inactive_3_months = most_recent_activity <= threshold

    # Calculate uncertainty based on evidence confidences
    if evidence_activities:
        avg_conf = sum(ev.confidence for ev, _ in evidence_activities) / len(evidence_activities)
        uncertainty = round(max(0.0, min(1.0, 1.0 - avg_conf)), 2)
    else:
        # Based solely on subscription metadata
        uncertainty = 0.1

    if is_inactive_3_months:
        return Decision(
            decision_id=decision_id,
            subscription_id=subscription.subscription_id,
            eligibility=EligibilityStatus.CANDIDATE,
            reason=(
                f"Inactive for at least three calendar months: last detected activity was on "
                f"{most_recent_activity.isoformat()} (threshold: {threshold.isoformat()}). "
                f"Eligible for cancellation proposal generation, subject to explicit human approval."
            ),
            uncertainty=uncertainty,
            rule_version=rule_version,
            evaluated_at=as_of,
            data_deletion_risk=data_deletion_risk,
            pause_alternative_available=pause_available,
            consequences=consequences,
        )
    else:
        return Decision(
            decision_id=decision_id,
            subscription_id=subscription.subscription_id,
            eligibility=EligibilityStatus.NOT_ELIGIBLE,
            reason=(
                f"Active within the last three calendar months: last detected activity was on "
                f"{most_recent_activity.isoformat()} (threshold: {threshold.isoformat()}). "
                f"Subscription is actively used; not eligible for cancellation."
            ),
            uncertainty=uncertainty,
            rule_version=rule_version,
            evaluated_at=as_of,
            data_deletion_risk=data_deletion_risk,
            pause_alternative_available=pause_available,
            consequences=[],
        )
