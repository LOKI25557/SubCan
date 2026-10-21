"""SubCan AI-Agent Orchestrator Module (Phase 6).

Responsible for:
- Orchestrating the end-to-end FinTech subscription cancellation lifecycle:
  1. DETECT: Ingest mock subscription data and evidence.
  2. DECIDE: Evaluate 3-calendar-month inactivity rule via rules.py.
  3. PLAN: Generate structured proposals for eligible candidates via planner.py.
  4. APPROVE: Human-in-the-loop explicit approval validation via approvals.py.
  5. EXECUTE: Safe, idempotent action execution via actions.py.
  6. VERIFY: Independent post-action renewal state check via verification.py.
  7. RECONCILE: Assemble auditable AgentRun results and summary.

SAFETY INVARIANTS:
1. Creating or running an orchestration cycle NEVER auto-approves or auto-cancels.
2. Ineligible (NOT_ELIGIBLE) and review-requiring (NEEDS_REVIEW) subscriptions never receive plans.
3. Strict per-subscription isolation: approving one subscription never authorizes actions for another.
4. Independent verification is mandatory; action success alone never marks cancellation as verified.
5. All source fixture data is loaded immutably to prevent unintended mutations.
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import threading
from typing import Dict, List, Optional, Tuple
from pydantic import BaseModel, ConfigDict, Field

from backend.app.actions import execute_plan
from backend.app.approvals import (
    ApprovalValidationError,
    validate_approval,
)
from backend.app.models import (
    ActionResult,
    ActionStatus,
    AgentRun,
    Approval,
    ApprovalStatus,
    CancellationPlan,
    Decision,
    EligibilityStatus,
    Evidence,
    FinalResultStatus,
    Subscription,
    SubscriptionStatus,
    VerificationResult,
    VerificationStatus,
)
from backend.app.planner import create_cancellation_plan
from backend.app.rules import evaluate_eligibility
from backend.app.verification import verify_cancellation


# ==============================================================================
# Domain Context Model & Thread-Safe Run Registry
# ==============================================================================

class SubscriptionRunState(BaseModel):
    """Full lifecycle tracking state for a single subscription within an AgentRun."""
    model_config = ConfigDict(extra="forbid")

    subscription: Subscription
    evidence_list: List[Evidence] = Field(default_factory=list)
    decision: Optional[Decision] = None
    plan: Optional[CancellationPlan] = None
    approval: Optional[Approval] = None
    action_result: Optional[ActionResult] = None
    verification_result: Optional[VerificationResult] = None
    final_status: FinalResultStatus = FinalResultStatus.PENDING
    diagnostic_notes: Optional[str] = None


class AgentRunRegistry:
    """Thread-safe, process-local registry storing agent runs and subscription states."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: Dict[str, AgentRun] = {}
        self._contexts: Dict[str, Dict[str, SubscriptionRunState]] = {}

    def save_run(self, run: AgentRun, context: Dict[str, SubscriptionRunState]) -> None:
        with self._lock:
            self._runs[run.run_id] = run
            self._contexts[run.run_id] = context

    def get_run(self, run_id: str) -> Optional[AgentRun]:
        with self._lock:
            return self._runs.get(run_id)

    def get_context(self, run_id: str) -> Optional[Dict[str, SubscriptionRunState]]:
        with self._lock:
            return self._contexts.get(run_id)

    def clear(self) -> None:
        with self._lock:
            self._runs.clear()
            self._contexts.clear()


_GLOBAL_RUN_REGISTRY = AgentRunRegistry()


def get_agent_run_registry() -> AgentRunRegistry:
    return _GLOBAL_RUN_REGISTRY


def clear_agent_run_registry() -> None:
    _GLOBAL_RUN_REGISTRY.clear()


# ==============================================================================
# Fixture Loader (Immutable & Safe)
# ==============================================================================

def load_fixture_data(
    target_subscription_ids: Optional[List[str]] = None,
) -> Tuple[List[Subscription], Dict[str, List[Evidence]]]:
    """Load subscriptions and associated evidence from fixtures/subscriptions.json immutably."""
    fixtures_path = Path(__file__).resolve().parents[2] / "fixtures" / "subscriptions.json"
    with fixtures_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    subscriptions: List[Subscription] = []
    evidence_map: Dict[str, List[Evidence]] = {}

    # Extract all evidence from scenarios
    for scenario in data.get("scenarios", {}).values():
        for ev_data in scenario.get("evidence", []):
            ev = Evidence.model_validate(ev_data)
            evidence_map.setdefault(ev.subscription_id, []).append(ev)

    for sub_data in data.get("subscriptions", []):
        sub = Subscription.model_validate(deepcopy(sub_data))
        if target_subscription_ids is None or sub.subscription_id in target_subscription_ids:
            subscriptions.append(sub)

    return subscriptions, evidence_map


# ==============================================================================
# Orchestration Lifecycle Core Functions
# ==============================================================================

def initialize_orchestration_run(
    subscriptions: List[Subscription],
    evidence_map: Optional[Dict[str, List[Evidence]]] = None,
    run_id: Optional[str] = None,
    as_of: Optional[datetime] = None,
    registry: Optional[AgentRunRegistry] = None,
) -> Tuple[AgentRun, Dict[str, SubscriptionRunState]]:
    """Initialize an AgentRun by executing the DETECT, DECIDE, and PLAN phases.

    SAFETY:
    - Plans are generated ONLY for subscriptions whose decision is CANDIDATE.
    - NEVER executes cancellation or mutates subscription data.
    - All plans remain strictly in AWAITING_APPROVAL status.
    """
    if as_of is None:
        as_of = datetime.now(timezone.utc)
    elif as_of.tzinfo is None or as_of.tzinfo.utcoffset(as_of) is None:
        raise ValueError("Timestamp 'as_of' must be timezone-aware.")

    active_registry = registry or _GLOBAL_RUN_REGISTRY
    evidence_dict = evidence_map or {}
    run_identifier = run_id or f"run_{int(as_of.timestamp())}"

    sub_states: Dict[str, SubscriptionRunState] = {}
    final_results: Dict[str, FinalResultStatus] = {}
    candidates_count = 0
    kept_count = 0
    blocked_count = 0

    for sub in subscriptions:
        ev_list = evidence_dict.get(sub.subscription_id, [])

        # Step 1: DECIDE (Evaluate 3-month inactivity)
        decision = evaluate_eligibility(
            subscription=sub,
            evidence_list=ev_list,
            as_of=as_of,
        )

        plan: Optional[CancellationPlan] = None
        final_status: FinalResultStatus
        diag_notes: Optional[str] = None

        # Step 2: PLAN (Proposal generation for candidates only)
        if decision.eligibility == EligibilityStatus.CANDIDATE:
            plan = create_cancellation_plan(
                subscription=sub,
                decision=decision,
                evidence_list=ev_list,
                created_at=as_of,
            )
            final_status = FinalResultStatus.PENDING
            diag_notes = "Plan generated and awaiting explicit user approval."
            candidates_count += 1
        elif decision.eligibility == EligibilityStatus.NOT_ELIGIBLE:
            final_status = FinalResultStatus.KEPT
            diag_notes = f"Not eligible for cancellation ({decision.reason}). Subscription kept active."
            kept_count += 1
        else:  # NEEDS_REVIEW
            final_status = FinalResultStatus.BLOCKED
            diag_notes = f"Manual review required ({decision.reason}). Automated planning halted."
            blocked_count += 1

        final_results[sub.subscription_id] = final_status
        sub_states[sub.subscription_id] = SubscriptionRunState(
            subscription=sub,
            evidence_list=ev_list,
            decision=decision,
            plan=plan,
            final_status=final_status,
            diagnostic_notes=diag_notes,
        )

    run_status = "in_progress" if candidates_count > 0 else "completed"
    summary = (
        f"Evaluated {len(subscriptions)} subscription(s): {candidates_count} candidate(s) awaiting approval, "
        f"{kept_count} kept active, {blocked_count} requiring manual review."
    )

    agent_run = AgentRun(
        run_id=run_identifier,
        started_at=as_of,
        completed_at=as_of if run_status == "completed" else None,
        status=run_status,
        subscription_ids=[s.subscription_id for s in subscriptions],
        final_results=final_results,
        summary=summary,
    )

    active_registry.save_run(agent_run, sub_states)
    return agent_run, sub_states


def process_subscription_approval(
    run_id: str,
    plan_id: str,
    approval: Approval,
    idempotency_key: Optional[str] = None,
    observed_subscription: Optional[Subscription] = None,
    post_action_evidence: Optional[List[Evidence]] = None,
    as_of: Optional[datetime] = None,
    registry: Optional[AgentRunRegistry] = None,
) -> SubscriptionRunState:
    """Process explicit human approval for a specific subscription plan in a run.

    Coordinates:
    - Approval validation (approvals.py).
    - Safe action execution (actions.py).
    - Independent verification check (verification.py).
    - Updating run status and final reconciliation.

    SAFETY:
    - Strictly bound to exact plan_id and subscription_id.
    - Other subscriptions in the run remain untouched.
    """
    if as_of is None:
        as_of = datetime.now(timezone.utc)
    elif as_of.tzinfo is None or as_of.tzinfo.utcoffset(as_of) is None:
        raise ValueError("Timestamp 'as_of' must be timezone-aware.")

    active_registry = registry or _GLOBAL_RUN_REGISTRY
    run = active_registry.get_run(run_id)
    contexts = active_registry.get_context(run_id)

    if run is None or contexts is None:
        raise ValueError(f"Run ID '{run_id}' not found in registry.")

    # Locate the target subscription state
    target_sub_id = approval.subscription_id
    state = contexts.get(target_sub_id)

    if state is None or state.plan is None:
        raise ValueError(
            f"No cancellation plan found for subscription '{target_sub_id}' in run '{run_id}'."
        )

    plan = state.plan
    state.approval = approval

    # Ensure plan_id matches exact plan and approval
    if plan.plan_id != plan_id or approval.plan_id != plan_id:
        state.final_status = FinalResultStatus.BLOCKED
        state.diagnostic_notes = (
            f"Plan ID mismatch: subscription has plan '{plan.plan_id}', received '{plan_id}' "
            f"(approval references '{approval.plan_id}')."
        )
        run.final_results[target_sub_id] = FinalResultStatus.BLOCKED
        _reconcile_run_summary(run, contexts, as_of=as_of)
        return state

    # --------------------------------------------------------------------------
    # Case 1: User explicitly declined cancellation
    # --------------------------------------------------------------------------
    if approval.status == ApprovalStatus.DECLINED:
        plan.status = ApprovalStatus.DECLINED
        state.final_status = FinalResultStatus.KEPT
        state.diagnostic_notes = "Cancellation declined by user. Subscription kept active."
        run.final_results[target_sub_id] = FinalResultStatus.KEPT
        _reconcile_run_summary(run, contexts, as_of=as_of)
        return state

    # --------------------------------------------------------------------------
    # Idempotent replay check: If action was already executed for this state,
    # defer directly to execute_plan to handle replay or conflict.
    # --------------------------------------------------------------------------
    if state.action_result is not None:
        action_result = execute_plan(
            plan=plan,
            approval=approval,
            idempotency_key=idempotency_key,
            decision=state.decision,
            action_type="cancellation",
            as_of=as_of,
        )
        return state

    # --------------------------------------------------------------------------
    # Case 2: Validate explicit approval
    # --------------------------------------------------------------------------
    is_valid = validate_approval(
        plan=plan,
        approval=approval,
        decision=state.decision,
        expected_action="cancellation",
        as_of=as_of,
    )

    if not is_valid:
        state.final_status = FinalResultStatus.BLOCKED
        state.diagnostic_notes = "Approval validation failed closed (mismatched, stale, or malformed)."
        run.final_results[target_sub_id] = FinalResultStatus.BLOCKED
        _reconcile_run_summary(run, contexts, as_of=as_of)
        return state

    # --------------------------------------------------------------------------
    # Case 3: Execute Action
    # --------------------------------------------------------------------------
    action_result = execute_plan(
        plan=plan,
        approval=approval,
        idempotency_key=idempotency_key,
        decision=state.decision,
        action_type="cancellation",
        as_of=as_of,
    )
    state.action_result = action_result

    # Mark plan as approved once successfully authorized and executed
    plan.status = ApprovalStatus.APPROVED

    # --------------------------------------------------------------------------
    # Case 4: Independent Verification Check
    # --------------------------------------------------------------------------
    verification_time = (
        action_result.completed_at + timedelta(milliseconds=100)
        if action_result.completed_at is not None
        else as_of
    )
    verification_result = verify_cancellation(
        action_result=action_result,
        observed_subscription=observed_subscription,
        evidence_list=post_action_evidence,
        service_name=plan.service_name,
        as_of=verification_time,
    )
    state.verification_result = verification_result

    # --------------------------------------------------------------------------
    # Case 5: Reconcile Final State
    # --------------------------------------------------------------------------
    if verification_result.status == VerificationStatus.VERIFIED:
        state.final_status = FinalResultStatus.VERIFIED_CANCELLED
        state.diagnostic_notes = "Action executed and cancellation independently verified."
    elif verification_result.status == VerificationStatus.FAILED:
        state.final_status = FinalResultStatus.FAILED
        state.diagnostic_notes = (
            f"Action attempted, but verification failed: {verification_result.details}"
        )
    else:  # PENDING
        state.final_status = FinalResultStatus.PENDING
        state.diagnostic_notes = (
            "Action executed, awaiting independent verification evidence."
        )

    run.final_results[target_sub_id] = state.final_status
    _reconcile_run_summary(run, contexts, as_of=as_of)
    return state


def _reconcile_run_summary(
    run: AgentRun,
    contexts: Dict[str, SubscriptionRunState],
    as_of: Optional[datetime] = None,
) -> None:
    """Update high-level run status and narrative summary based on subscription states."""
    all_statuses = [ctx.final_status for ctx in contexts.values()]
    has_pending = any(s == FinalResultStatus.PENDING for s in all_statuses)

    cancelled_count = sum(1 for s in all_statuses if s == FinalResultStatus.VERIFIED_CANCELLED)
    kept_count = sum(1 for s in all_statuses if s == FinalResultStatus.KEPT)
    failed_count = sum(1 for s in all_statuses if s == FinalResultStatus.FAILED)
    blocked_count = sum(1 for s in all_statuses if s == FinalResultStatus.BLOCKED)

    if not has_pending:
        run.status = "completed"
        run.completed_at = as_of or datetime.now(timezone.utc)
    else:
        run.status = "in_progress"
        run.completed_at = None

    run.summary = (
        f"Run {run.status}: {cancelled_count} verified cancelled, {kept_count} kept, "
        f"{failed_count} failed, {blocked_count} blocked/review required."
    )


def run_orchestration_cycle(
    subscription_ids: Optional[List[str]] = None,
    as_of: Optional[datetime] = None,
    registry: Optional[AgentRunRegistry] = None,
) -> AgentRun:
    """Run full initial analysis and proposal cycle across target subscriptions.

    Loads authorized synthetic data, computes decisions, and builds proposals
    awaiting user approval. Does NOT execute cancellations.
    """
    subscriptions, evidence_map = load_fixture_data(subscription_ids)
    run, _ = initialize_orchestration_run(
        subscriptions=subscriptions,
        evidence_map=evidence_map,
        as_of=as_of,
        registry=registry,
    )
    return run
