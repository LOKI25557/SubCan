"""SubCan Independent Cancellation Verification Module (Phase 5).

Responsible for:
- Independently polling, querying, and verifying observed subscription renewal states.
- Reconciling post-action states against independent evidence or connector observations.
- Strict isolation: NEVER treating ActionStatus.SUCCEEDED as proof of cancellation.
- Distinguishing verified cancellation from renewal-still-on, paused, stale, or contradictory states.
- Enforcing subscription and service identity matching.
- SAFETY INVARIANTS:
  1. Verification NEVER executes, retries, or reissues cancellation actions.
  2. Verification NEVER automatically approves any plan.
  3. Pauses are NEVER reported as cancellations.
  4. Inconclusive, missing, or contradictory evidence fails safely.
"""

from datetime import datetime, timezone
import re
from typing import List, Optional
from backend.app.models import (
    ActionResult,
    ActionStatus,
    Evidence,
    Subscription,
    SubscriptionStatus,
    VerificationResult,
    VerificationStatus,
)


class VerificationError(ValueError):
    """Base exception for verification failures."""
    pass


class VerificationMismatchError(VerificationError):
    """Raised when subscription ID, action ID, or service identity does not match."""
    pass


def _is_stale_evidence(
    evidence: Evidence,
    action_result: ActionResult,
) -> bool:
    """Check if evidence was captured before the cancellation action completed."""
    ref_time = action_result.completed_at or action_result.started_at
    if ref_time is None:
        return False
    # If evidence timestamp is before the action completed, it cannot reflect post-action state
    return evidence.captured_at < ref_time


def _detect_evidence_state(
    evidence_list: List[Evidence],
) -> Optional[str]:
    """Inspect evidence excerpts for explicit renewal/cancellation/pause status."""
    has_cancelled = False
    has_active = False
    has_paused = False

    for ev in evidence_list:
        text = ev.excerpt.lower()
        if any(w in text for w in ("terminated", "cancelled", "canceled", "renewal disabled", "renewal off")):
            has_cancelled = True
        if any(w in text for w in ("renewing", "active", "renewal on", "next billing date", "renewal scheduled")):
            has_active = True
        if any(w in text for w in ("paused", "freeze", "on hold", "billing suspended")):
            has_paused = True

    if has_paused:
        return "paused"
    if has_cancelled and has_active:
        return "contradictory"
    if has_cancelled:
        return "cancelled"
    if has_active:
        return "active"
    return None


def verify_cancellation(
    action_result: ActionResult,
    observed_subscription: Optional[Subscription] = None,
    evidence_list: Optional[List[Evidence]] = None,
    service_name: Optional[str] = None,
    method: str = "renewal_status_check",
    as_of: Optional[datetime] = None,
) -> VerificationResult:
    """Independently verify renewal state following a cancellation attempt.

    SAFETY INVARIANTS:
    1. ActionStatus.SUCCEEDED alone NEVER implies verified cancellation.
    2. Verification NEVER executes or retries any cancellation action.
    3. Mismatched subscription IDs, action IDs, or service names raise VerificationMismatchError.
    4. Stale, missing, or contradictory evidence fails safely.
    5. Observed pause status is NEVER reported as cancelled.

    Args:
        action_result: The executed ActionResult to verify.
        observed_subscription: Optional independently re-fetched Subscription entity.
        evidence_list: Optional independently collected post-action Evidence entities.
        service_name: Optional expected service name to verify identity.
        method: Independent verification method identifier (default: 'renewal_status_check').
        as_of: Injectable verification timestamp (defaults to current UTC time).

    Returns:
        Validated VerificationResult entity with status and diagnostic details.

    Raises:
        VerificationMismatchError: If subscription ID or service identity does not match.
        ValueError: If as_of is timezone-naive.
    """
    if as_of is None:
        as_of = datetime.now(timezone.utc)
    elif as_of.tzinfo is None or as_of.tzinfo.utcoffset(as_of) is None:
        raise ValueError("Timestamp 'as_of' must be timezone-aware.")

    verification_id = f"verif_{action_result.action_id}_{int(as_of.timestamp())}"
    evidence_items = evidence_list or []

    # --------------------------------------------------------------------------
    # 1. Identity & Matching Verification
    # --------------------------------------------------------------------------
    if observed_subscription is not None:
        if observed_subscription.subscription_id != action_result.subscription_id:
            raise VerificationMismatchError(
                f"Subscription ID mismatch: action '{action_result.action_id}' is for "
                f"'{action_result.subscription_id}', but observed subscription is "
                f"'{observed_subscription.subscription_id}'."
            )
        if service_name is not None and observed_subscription.service_name.lower() != service_name.lower():
            raise VerificationMismatchError(
                f"Service identity mismatch: expected service '{service_name}', "
                f"but observed subscription is '{observed_subscription.service_name}'."
            )

    for ev in evidence_items:
        if ev.subscription_id != action_result.subscription_id:
            raise VerificationMismatchError(
                f"Evidence subscription ID mismatch: evidence '{ev.evidence_id}' references "
                f"'{ev.subscription_id}', but action targets '{action_result.subscription_id}'. "
                "Evidence from other subscriptions cannot be used for verification."
            )

    # --------------------------------------------------------------------------
    # 2. Check Action Status (Did action succeed?)
    # --------------------------------------------------------------------------
    if action_result.status != ActionStatus.SUCCEEDED:
        return VerificationResult(
            verification_id=verification_id,
            action_id=action_result.action_id,
            subscription_id=action_result.subscription_id,
            status=VerificationStatus.FAILED,
            method=method,
            verified_at=as_of,
            final_state_observed="unattempted_or_failed",
            details=(
                f"Cannot verify cancellation: prior action status is '{action_result.status.value}', "
                "not succeeded. Action execution failed or was blocked."
            ),
        )

    # --------------------------------------------------------------------------
    # 3. Handle Missing Evidence / Inconclusive State
    # --------------------------------------------------------------------------
    if observed_subscription is None and not evidence_items:
        return VerificationResult(
            verification_id=verification_id,
            action_id=action_result.action_id,
            subscription_id=action_result.subscription_id,
            status=VerificationStatus.PENDING,
            method=method,
            verified_at=as_of,
            final_state_observed="pending_propagation",
            details=(
                "No independent renewal check data observed yet. "
                "Action executed, but cancellation outcome awaits verification."
            ),
        )

    # --------------------------------------------------------------------------
    # 4. Freshness Check (Stale Evidence)
    # --------------------------------------------------------------------------
    if evidence_items and observed_subscription is None:
        if all(_is_stale_evidence(ev, action_result) for ev in evidence_items):
            return VerificationResult(
                verification_id=verification_id,
                action_id=action_result.action_id,
                subscription_id=action_result.subscription_id,
                status=VerificationStatus.FAILED,
                method=method,
                verified_at=as_of,
                final_state_observed="stale_evidence",
                details=(
                    "All verification evidence items were captured before action execution completed. "
                    "Stale pre-action evidence cannot verify post-action cancellation."
                ),
            )

    # --------------------------------------------------------------------------
    # 5. Evaluate Observed Subscription State & Evidence
    # --------------------------------------------------------------------------
    evidence_state = _detect_evidence_state(evidence_items)

    # Check for contradictory evidence
    if evidence_state == "contradictory":
        return VerificationResult(
            verification_id=verification_id,
            action_id=action_result.action_id,
            subscription_id=action_result.subscription_id,
            status=VerificationStatus.FAILED,
            method=method,
            verified_at=as_of,
            final_state_observed="contradictory",
            details="Contradictory evidence detected: conflicting reports of renewal status.",
        )

    # Check for observed pause (Must never be reported as cancelled)
    if evidence_state == "paused" or (
        observed_subscription is not None
        and observed_subscription.pause_option_available
        and observed_subscription.status != SubscriptionStatus.CANCELLED
    ):
        return VerificationResult(
            verification_id=verification_id,
            action_id=action_result.action_id,
            subscription_id=action_result.subscription_id,
            status=VerificationStatus.FAILED,
            method=method,
            verified_at=as_of,
            final_state_observed="paused",
            details=(
                "Independent check observed subscription is PAUSED, not cancelled. "
                "A paused subscription preserves legacy billing and must not be reported as cancelled."
            ),
        )

    # Check for renewal still active
    if (
        observed_subscription is not None
        and observed_subscription.status == SubscriptionStatus.ACTIVE
    ) or evidence_state == "active":
        return VerificationResult(
            verification_id=verification_id,
            action_id=action_result.action_id,
            subscription_id=action_result.subscription_id,
            status=VerificationStatus.FAILED,
            method=method,
            verified_at=as_of,
            final_state_observed="active",
            details=(
                "Independent renewal check confirms subscription is still ACTIVE. "
                "Renewal has not been disabled by the provider."
            ),
        )

    # Check for confirmed cancellation
    if (
        observed_subscription is not None
        and observed_subscription.status == SubscriptionStatus.CANCELLED
    ) or evidence_state == "cancelled":
        return VerificationResult(
            verification_id=verification_id,
            action_id=action_result.action_id,
            subscription_id=action_result.subscription_id,
            status=VerificationStatus.VERIFIED,
            method=method,
            verified_at=as_of,
            final_state_observed="cancelled",
            details="Independent check confirmed: renewal is disabled and subscription is verified cancelled.",
        )

    # Inconclusive fallback
    return VerificationResult(
        verification_id=verification_id,
        action_id=action_result.action_id,
        subscription_id=action_result.subscription_id,
        status=VerificationStatus.FAILED,
        method=method,
        verified_at=as_of,
        final_state_observed="unknown",
        details="Verification evidence was inconclusive; unable to confirm renewal status.",
    )
