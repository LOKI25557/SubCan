"""SubCan Human-in-the-Loop Approval Module (Phase 3).

Responsible for:
- Validating explicit human approval against exact service name, plan ID, and plan version.
- Enforcing safety invariants: no action can run without verified, active, explicit approval.
- Failing closed: missing, ambiguous, declined, stale, or mismatched approvals are strictly rejected.
- Preventing approval reuse across services or across modified plan versions.
- Enforcing timestamp validity (presence, timezone-awareness, not in the future, not prior to plan creation).
"""

from datetime import datetime, timezone
import re
from typing import Optional
from backend.app.models import (
    Approval,
    ApprovalStatus,
    CancellationPlan,
    Decision,
    EligibilityStatus,
)


class ApprovalValidationError(ValueError):
    """Raised when an approval fails validation checks."""
    pass


def get_approval_rejection_reason(
    plan: CancellationPlan,
    approval: Optional[Approval],
    decision: Optional[Decision] = None,
    expected_action: str = "cancellation",
    as_of: Optional[datetime] = None,
) -> Optional[str]:
    """Inspect approval against plan and return rejection reason if invalid, or None if valid.

    SAFETY CHECKS (Fail-Closed):
    1. Rejects missing (None) approval.
    2. Rejects malformed approval object.
    3. Rejects plans not in AWAITING_APPROVAL status.
    4. Rejects plans where decision eligibility is not CANDIDATE.
    5. Rejects non-APPROVED status (DECLINED, INVALIDATED, AWAITING_APPROVAL).
    6. Rejects plan ID mismatch.
    7. Rejects subscription ID mismatch.
    8. Rejects service name mismatch.
    9. Rejects plan version mismatch (stale approval for an altered plan).
    10. Rejects proposed action mismatch.
    11. Rejects missing, naive, future-dated, or pre-plan timestamps.
    """
    if as_of is None:
        as_of = datetime.now(timezone.utc)
    elif as_of.tzinfo is None or as_of.tzinfo.utcoffset(as_of) is None:
        return "Evaluation timestamp 'as_of' must be timezone-aware."

    # 1. Missing approval check
    if approval is None:
        return "Missing approval: no approval record was provided for the plan."

    # 2. Type / Malformed check
    if not isinstance(approval, Approval):
        return f"Malformed approval: expected Approval instance, received '{type(approval).__name__}'."

    # 3. Plan status check
    if plan.status != ApprovalStatus.AWAITING_APPROVAL:
        return (
            f"Plan is not awaiting approval (current plan status is '{plan.status.value}'). "
            "Only plans in 'awaiting_approval' status can be authorized."
        )

    # 4. Decision check (if decision provided)
    if decision is not None:
        if decision.subscription_id != plan.subscription_id:
            return (
                f"Decision subscription ID '{decision.subscription_id}' "
                f"does not match plan subscription ID '{plan.subscription_id}'."
            )
        if decision.eligibility != EligibilityStatus.CANDIDATE:
            return (
                f"Cannot authorize plan: subscription eligibility is '{decision.eligibility.value}' "
                f"({decision.reason}). Only 'candidate' subscriptions may be approved."
            )

    # 5. Explicit approval status
    if approval.status == ApprovalStatus.DECLINED:
        return "Approval was explicitly declined by the user."
    if approval.status == ApprovalStatus.INVALIDATED:
        return "Approval has been invalidated due to plan changes or policy updates."
    if approval.status == ApprovalStatus.AWAITING_APPROVAL:
        return "Approval record is still in awaiting_approval status; explicit approval is required."
    if approval.status != ApprovalStatus.APPROVED:
        return f"Unsupported approval status '{approval.status}'."

    # 6. Plan ID match
    if approval.plan_id != plan.plan_id:
        return (
            f"Plan ID mismatch: approval is for plan '{approval.plan_id}', "
            f"but target plan is '{plan.plan_id}'."
        )

    # 7. Subscription ID match
    if approval.subscription_id != plan.subscription_id:
        return (
            f"Subscription ID mismatch: approval references '{approval.subscription_id}', "
            f"but plan targets '{plan.subscription_id}'."
        )

    # 8. Service name match
    if approval.service_name != plan.service_name:
        return (
            f"Service name mismatch: approval references service '{approval.service_name}', "
            f"but plan targets '{plan.service_name}'."
        )

    # 9. Exact Plan Version match (Stale approval protection)
    if approval.plan_version != plan.plan_version:
        return (
            f"Plan version mismatch: approval was granted for version '{approval.plan_version}', "
            f"but current plan version is '{plan.plan_version}'. "
            "Material changes have invalidated the previous approval."
        )

    # 10. Expected action match
    if plan.steps:
        first_step = plan.steps[0].lower()
        if expected_action.lower() not in first_step:
            return (
                f"Proposed action mismatch: expected action '{expected_action}', "
                f"but plan step specifies: '{plan.steps[0]}'."
            )

    if approval.notes:
        notes_lower = approval.notes.lower()
        if "action=" in notes_lower or "action:" in notes_lower:
            match = re.search(r"action[:=]\s*(\w+)", notes_lower)
            if match:
                noted_action = match.group(1)
                if noted_action != expected_action.lower():
                    return (
                        f"Approved action mismatch: approval notes specify action '{noted_action}', "
                        f"which conflicts with proposed plan action '{expected_action}'."
                    )

    # 11. Timestamp validation
    if approval.decided_at is None:
        return "Missing approval timestamp: decided_at is required for an approved record."

    if approval.decided_at.tzinfo is None or approval.decided_at.tzinfo.utcoffset(approval.decided_at) is None:
        return "Approval timestamp 'decided_at' must be timezone-aware."

    if approval.decided_at > as_of:
        return (
            f"Future-dated approval timestamp: decided_at ({approval.decided_at.isoformat()}) "
            f"is after evaluation time ({as_of.isoformat()})."
        )

    if approval.decided_at < plan.created_at:
        return (
            f"Stale approval timestamp: decided_at ({approval.decided_at.isoformat()}) "
            f"is prior to plan creation ({plan.created_at.isoformat()})."
        )

    return None


def validate_approval(
    plan: CancellationPlan,
    approval: Optional[Approval],
    decision: Optional[Decision] = None,
    expected_action: str = "cancellation",
    as_of: Optional[datetime] = None,
    raise_on_error: bool = False,
) -> bool:
    """Validate whether an approval record authorizes execution of the given cancellation plan.

    Args:
        plan: Targeted CancellationPlan.
        approval: Approval record to validate (None is safely rejected).
        decision: Optional Decision record associated with the plan.
        expected_action: Expected action string (default: 'cancellation').
        as_of: Injectable validation timestamp (defaults to current UTC time).
        raise_on_error: If True, raises ApprovalValidationError on invalid approval.

    Returns:
        True if the approval is completely valid and authorizes execution; False otherwise.

    Raises:
        ApprovalValidationError: If raise_on_error is True and validation fails.
    """
    rejection_reason = get_approval_rejection_reason(
        plan=plan,
        approval=approval,
        decision=decision,
        expected_action=expected_action,
        as_of=as_of,
    )

    if rejection_reason is not None:
        if raise_on_error:
            raise ApprovalValidationError(rejection_reason)
        return False

    return True


def enforce_approval(
    plan: CancellationPlan,
    approval: Optional[Approval],
    decision: Optional[Decision] = None,
    expected_action: str = "cancellation",
    as_of: Optional[datetime] = None,
) -> None:
    """Strict approval enforcement gate. Raises ApprovalValidationError if invalid."""
    validate_approval(
        plan=plan,
        approval=approval,
        decision=decision,
        expected_action=expected_action,
        as_of=as_of,
        raise_on_error=True,
    )


def create_approval(
    plan: CancellationPlan,
    approved: bool,
    approved_by: str = "user_loki",
    decided_at: Optional[datetime] = None,
    notes: Optional[str] = None,
) -> Approval:
    """Helper factory to create an Approval record bound to the exact plan, service, and version."""
    status = ApprovalStatus.APPROVED if approved else ApprovalStatus.DECLINED
    if decided_at is None:
        decided_at = datetime.now(timezone.utc)
    elif decided_at.tzinfo is None or decided_at.tzinfo.utcoffset(decided_at) is None:
        raise ValueError("decided_at timestamp must be timezone-aware.")

    return Approval(
        approval_id=f"appr_{plan.subscription_id}_{int(decided_at.timestamp())}",
        plan_id=plan.plan_id,
        subscription_id=plan.subscription_id,
        service_name=plan.service_name,
        plan_version=plan.plan_version,
        status=status,
        approved_by=approved_by,
        decided_at=decided_at,
        notes=notes,
    )
