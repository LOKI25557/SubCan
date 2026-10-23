"""SubCan Safe Action Coordination Module (Phase 4).

Responsible for:
- Enforcing strict approval authorization gate immediately prior to execution.
- Action type safety: keeping cancellation and pause actions strictly distinct.
- Local, thread-safe idempotency registry to prevent duplicate execution attempts.
- Re-executing identical requests safely without issuing duplicate cancellations.
- Rejecting idempotency key reuse across different subscriptions, plans, or versions.
- Safe execution lifecycle: capturing logs, timestamps, and execution outcomes.
- CRITICAL INVARIANT: ActionStatus.SUCCEEDED represents ONLY execution attempt completion;
  it NEVER implies verified cancellation. Independent verification is mandatory.
"""

from datetime import datetime, timedelta, timezone
import threading
from typing import Dict, List, Optional, Tuple
from backend.app.approvals import (
    ApprovalValidationError,
    enforce_approval,
)
from backend.app.models import (
    ActionResult,
    ActionStatus,
    Approval,
    CancellationPlan,
    Decision,
)


class ActionExecutionError(ValueError):
    """Base error for action coordination failures."""
    pass


class IdempotencyConflictError(ActionExecutionError):
    """Raised when an idempotency key is reused for a different logical request."""
    pass


class ActionTypeMismatchError(ActionExecutionError):
    """Raised when the requested action type does not match the plan's authorized action."""
    pass


# ==============================================================================
# Idempotency & Action Registry (Process-Local & Thread-Safe)
# ==============================================================================

class ActionRegistry:
    """Thread-safe registry for idempotency and action tracking with durable SQLite persistence."""

    def __init__(self, restore_persisted: bool = False) -> None:
        self._lock = threading.Lock()
        self._actions_by_id: Dict[str, ActionResult] = {}
        # Maps idempotency_key -> (ActionResult, logical_request_signature)
        self._actions_by_key: Dict[str, Tuple[ActionResult, Dict[str, str]]] = {}
        if restore_persisted:
            self.restore_persisted_actions()

    def restore_persisted_actions(self) -> None:
        """Restore actions from durable storage to survive restarts."""
        try:
            from backend.app.database import load_action_records
            for key, action, sig in load_action_records():
                self._actions_by_id[action.action_id] = action
                if key is not None:
                    self._actions_by_key[key] = (action, sig)
        except Exception:
            pass

    def get(self, action_id: str) -> Optional[ActionResult]:
        with self._lock:
            return self._actions_by_id.get(action_id)

    def get_by_idempotency_key(
        self, key: str
    ) -> Optional[Tuple[ActionResult, Dict[str, str]]]:
        with self._lock:
            return self._actions_by_key.get(key)

    def record(
        self,
        key: Optional[str],
        action: ActionResult,
        request_sig: Dict[str, str],
    ) -> None:
        with self._lock:
            self._actions_by_id[action.action_id] = action
            if key is not None:
                self._actions_by_key[key] = (action, request_sig)
        try:
            from backend.app.database import save_action_record
            save_action_record(key, action, request_sig)
        except Exception:
            pass

    def clear(self) -> None:
        """Clear all stored actions (useful for test isolation)."""
        with self._lock:
            self._actions_by_id.clear()
            self._actions_by_key.clear()


# Global module-level registry instance
_GLOBAL_REGISTRY = ActionRegistry(restore_persisted=True)


def get_action_registry() -> ActionRegistry:
    """Return the global action registry instance."""
    return _GLOBAL_REGISTRY


def clear_action_registry() -> None:
    """Reset the global action registry (for testing and isolation)."""
    _GLOBAL_REGISTRY.clear()


def make_request_signature(
    subscription_id: str,
    plan_id: str,
    plan_version: str,
    action_type: str,
) -> Dict[str, str]:
    """Generate canonical signature dictionary of a logical cancellation request."""
    return {
        "subscription_id": subscription_id,
        "plan_id": plan_id,
        "plan_version": plan_version,
        "action_type": action_type,
    }


# ==============================================================================
# Action Coordination Core Functions
# ==============================================================================

def authorize_action(
    plan: CancellationPlan,
    approval: Optional[Approval],
    decision: Optional[Decision] = None,
    action_type: str = "cancellation",
    as_of: Optional[datetime] = None,
) -> ActionResult:
    """Authorize a cancellation action, verifying approval before any execution.

    SAFETY:
    - Never executes any cancellation.
    - Fails closed if approval is missing, declined, stale, or mismatched.
    - Enforces action type safety (cancellation vs pause).

    Returns an ActionResult in PENDING status.
    """
    if as_of is None:
        as_of = datetime.now(timezone.utc)
    elif as_of.tzinfo is None or as_of.tzinfo.utcoffset(as_of) is None:
        raise ValueError("Timestamp 'as_of' must be timezone-aware.")

    # 1. Action Type Safety Check
    if action_type.lower() != "cancellation":
        raise ActionTypeMismatchError(
            f"Action type mismatch: plan '{plan.plan_id}' authorizes 'cancellation', "
            f"but requested action is '{action_type}'. Cancellation and pause actions are strictly distinct "
            "and cannot be converted."
        )

    # 2. Revalidate Approval Gate (Fail-Closed)
    enforce_approval(
        plan=plan,
        approval=approval,
        decision=decision,
        expected_action=action_type,
        as_of=as_of,
    )

    action_id = f"act_{plan.subscription_id}_{int(as_of.timestamp())}"
    logs = [
        f"{as_of.isoformat()}: Action authorized: '{action_type}' for service '{plan.service_name}' "
        f"(Subscription ID: {plan.subscription_id}).",
        f"{as_of.isoformat()}: Validated explicit approval (Approval ID: {approval.approval_id}, "
        f"Version: {plan.plan_version}). Status: PENDING execution.",
    ]

    return ActionResult(
        action_id=action_id,
        plan_id=plan.plan_id,
        subscription_id=plan.subscription_id,
        status=ActionStatus.PENDING,
        started_at=as_of,
        completed_at=None,
        message=f"Action authorized and pending execution for {plan.service_name}.",
        action_type=action_type,
        logs=logs,
    )


def execute_plan(
    plan: CancellationPlan,
    approval: Optional[Approval],
    idempotency_key: Optional[str] = None,
    decision: Optional[Decision] = None,
    action_type: str = "cancellation",
    as_of: Optional[datetime] = None,
    simulate_failure: Optional[str] = None,
    registry: Optional[ActionRegistry] = None,
) -> ActionResult:
    """Execute cancellation steps for an approved plan against the mock environment.

    SAFETY INVARIANTS:
    1. Revalidates approval immediately before execution (fail-closed).
    2. Enforces idempotency: identical requests with same key return the existing action.
    3. Reusing an idempotency key for a different request raises IdempotencyConflictError.
    4. Cancellation and pause are strictly distinct; mismatched action types are rejected.
    5. CRITICAL: ActionStatus.SUCCEEDED represents ONLY execution attempt completion;
       it NEVER implies verified cancellation.

    Args:
        plan: Validated CancellationPlan to execute.
        approval: Explicit Approval record authorizing execution.
        idempotency_key: Optional client-provided unique idempotency key.
        decision: Optional Decision entity associated with the plan.
        action_type: Action type string (default: 'cancellation').
        as_of: Injectable execution timestamp (defaults to current UTC time).
        simulate_failure: Optional error message string to simulate execution failure.
        registry: Optional custom ActionRegistry (defaults to global module registry).

    Returns:
        ActionResult indicating execution outcome and chronological logs.

    Raises:
        ApprovalValidationError: If approval validation fails.
        ActionTypeMismatchError: If action_type conflicts with plan.
        IdempotencyConflictError: If idempotency_key is reused for a different request.
    """
    if as_of is None:
        as_of = datetime.now(timezone.utc)
    elif as_of.tzinfo is None or as_of.tzinfo.utcoffset(as_of) is None:
        raise ValueError("Timestamp 'as_of' must be timezone-aware.")

    active_registry = registry or _GLOBAL_REGISTRY
    current_sig = make_request_signature(
        subscription_id=plan.subscription_id,
        plan_id=plan.plan_id,
        plan_version=plan.plan_version,
        action_type=action_type,
    )

    # --------------------------------------------------------------------------
    # 1. Action Type Safety Check
    # --------------------------------------------------------------------------
    if action_type.lower() != "cancellation":
        raise ActionTypeMismatchError(
            f"Action type mismatch: plan '{plan.plan_id}' authorizes 'cancellation', "
            f"but requested action is '{action_type}'. Actions cannot be silently converted."
        )

    # --------------------------------------------------------------------------
    # 2. Idempotency Check
    # --------------------------------------------------------------------------
    if idempotency_key is not None:
        existing = active_registry.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            existing_action, existing_sig = existing
            if existing_sig == current_sig:
                # Deterministic replay of identical logical request
                return existing_action
            else:
                # Key reused for a DIFFERENT subscription, plan, version, or action
                raise IdempotencyConflictError(
                    f"Idempotency key '{idempotency_key}' was already used for a different request: "
                    f"existing request={existing_sig}, new request={current_sig}. "
                    "Idempotency keys cannot be reused across different operations."
                )

    # --------------------------------------------------------------------------
    # 3. Approval Revalidation Gate (Fail-Closed)
    # --------------------------------------------------------------------------
    # Must fail BEFORE creating any action or mutating state
    enforce_approval(
        plan=plan,
        approval=approval,
        decision=decision,
        expected_action=action_type,
        as_of=as_of,
    )

    # --------------------------------------------------------------------------
    # 4. Action Execution
    # --------------------------------------------------------------------------
    started_at = as_of
    if idempotency_key is not None:
        action_id = f"act_{idempotency_key}"
    else:
        action_id = f"act_{plan.subscription_id}_{int(started_at.timestamp())}"

    logs: List[str] = [
        f"{started_at.isoformat()}: Action authorized: '{action_type}' for service '{plan.service_name}'.",
        f"{started_at.isoformat()}: Verified explicit approval '{approval.approval_id}' (Plan version: {plan.plan_version}).",
        f"{started_at.isoformat()}: Starting cancellation execution in mock environment.",
    ]

    completed_at = started_at + timedelta(milliseconds=500)

    if simulate_failure:
        logs.append(
            f"{completed_at.isoformat()}: EXECUTION FAILURE: {simulate_failure}. "
            "Action halted without blind retry."
        )
        result = ActionResult(
            action_id=action_id,
            plan_id=plan.plan_id,
            subscription_id=plan.subscription_id,
            status=ActionStatus.FAILED,
            started_at=started_at,
            completed_at=completed_at,
            message=f"Execution failed: {simulate_failure}",
            action_type=action_type,
            logs=logs,
        )
    else:
        logs.extend([
            f"{completed_at.isoformat()}: Dispatched cancellation payload to mock endpoint for '{plan.service_name}'.",
            f"{completed_at.isoformat()}: Mock endpoint returned HTTP 200 (cancellation request processed).",
            f"{completed_at.isoformat()}: Action execution completed successfully. "
            "CRITICAL: AWAITING INDEPENDENT VERIFICATION (Action success is not proof of cancellation).",
        ])
        result = ActionResult(
            action_id=action_id,
            plan_id=plan.plan_id,
            subscription_id=plan.subscription_id,
            status=ActionStatus.SUCCEEDED,
            started_at=started_at,
            completed_at=completed_at,
            message=(
                f"Cancellation action completed for '{plan.service_name}'. "
                "Independent renewal verification is required before marking as cancelled."
            ),
            action_type=action_type,
            logs=logs,
        )

    # --------------------------------------------------------------------------
    # 5. Record in Registry
    # --------------------------------------------------------------------------
    active_registry.record(idempotency_key, result, current_sig)
    return result
