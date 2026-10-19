"""SubCan FastAPI Application (Phase 7: API Endpoint Implementation).

Detect. Decide. Approve. Cancel. Verify.

Exposes RESTful endpoints for:
1. Health & System Status: GET /health
2. Orchestration Runs: POST /runs, GET /runs/{run_id}, GET /runs/{run_id}/subscriptions
3. Human-in-the-Loop Approvals: POST /plans/{plan_id}/approve, POST /plans/{plan_id}/decline
4. Independent Verification: POST /actions/{action_id}/verify
5. Reporting & Reconciliation: GET /runs/{run_id}/report

SAFETY INVARIANTS:
1. Creating a run initiates analysis and proposal creation only; it NEVER executes cancellation.
2. Only an explicit, valid, current approval can authorize an action.
3. Strict per-subscription isolation: approving one plan never affects any other subscription.
4. Action execution success is NEVER proof of cancellation; independent verification is mandatory.
5. Missing, stale, or contradictory evidence fails closed.
6. Process-local state limitation: runtime state is held in process-local memory via thread-safe registries.
"""

from datetime import datetime, timezone
import threading
from typing import Any, Dict, List, Optional, Tuple
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from backend.app.actions import (
    IdempotencyConflictError,
    get_action_registry,
)
from backend.app.agent import (
    SubscriptionRunState,
    _reconcile_run_summary,
    get_agent_run_registry,
    initialize_orchestration_run,
    load_fixture_data,
    process_subscription_approval,
)
from backend.app.models import (
    ActionResult,
    ActionStatus,
    AgentRun,
    Approval,
    ApprovalStatus,
    CancellationPlan,
    Decision,
    Evidence,
    FinalResultStatus,
    HealthCheckResponse,
    Subscription,
    VerificationResult,
    VerificationStatus,
)
from backend.app.verification import verify_cancellation


# ==============================================================================
# API Request & Response Schemas
# ==============================================================================

class ErrorDetail(BaseModel):
    """RFC-7807 compatible error envelope detail."""
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    details: Dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ErrorEnvelope(BaseModel):
    """Standardized top-level API error envelope."""
    model_config = ConfigDict(extra="forbid")

    error: ErrorDetail


class CreateRunRequest(BaseModel):
    """Payload to initiate a new orchestration run."""
    model_config = ConfigDict(extra="forbid")

    subscription_ids: List[str] = Field(
        ...,
        description="List of target synthetic subscription IDs to evaluate.",
    )
    rule_version: Optional[str] = Field(
        default="1.0.0",
        description="Version string of the decision rule set.",
    )
    dry_run: Optional[bool] = Field(
        default=False,
        description="Safety flag; planning never executes cancellations regardless of flag.",
    )
    as_of: Optional[datetime] = Field(
        default=None,
        description="Optional evaluation timestamp for deterministic testing.",
    )


class RunSubscriptionDetailItem(BaseModel):
    """Detailed domain context of a single subscription within an agent run."""
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


class RunSubscriptionsResponse(BaseModel):
    """Structured response containing all subscription details in a run."""
    model_config = ConfigDict(extra="forbid")

    run_id: str
    items: List[RunSubscriptionDetailItem]


class ApprovePlanRequest(BaseModel):
    """Payload for explicit human approval of a cancellation plan."""
    model_config = ConfigDict(extra="forbid")

    plan_id: str
    subscription_id: str
    service_name: str
    plan_version: str
    approved_by: str = Field(default="user_loki", description="Identity of approving user.")
    decided_at: Optional[datetime] = Field(default=None, description="Approval timestamp.")
    notes: Optional[str] = Field(default=None, description="Optional user confirmation notes.")
    idempotency_key: Optional[str] = Field(default=None, description="Process-local idempotency key.")
    observed_subscription: Optional[Subscription] = Field(
        default=None, description="Independent post-action observation (for testing/connectors)."
    )
    post_action_evidence: Optional[List[Evidence]] = Field(
        default=None, description="Independent post-action evidence items."
    )


class DeclinePlanRequest(BaseModel):
    """Payload to explicitly decline cancellation of a proposed plan."""
    model_config = ConfigDict(extra="forbid")

    plan_id: str
    plan_version: str
    declined_by: str = Field(default="user_loki", description="Identity of declining user.")
    decided_at: Optional[datetime] = Field(default=None, description="Decision timestamp.")
    notes: Optional[str] = Field(default=None, description="Reason for declining.")


class VerifyActionRequest(BaseModel):
    """Payload to trigger independent post-action cancellation verification."""
    model_config = ConfigDict(extra="forbid")

    observed_subscription: Optional[Subscription] = Field(
        default=None, description="Independently re-fetched subscription entity."
    )
    evidence_list: Optional[List[Evidence]] = Field(
        default=None, description="Post-action independent evidence excerpts."
    )
    method: str = Field(default="renewal_status_check", description="Verification method identifier.")
    as_of: Optional[datetime] = Field(default=None, description="Verification reference timestamp.")


class RunReportItem(BaseModel):
    """Itemized audit summary for a subscription in a run report."""
    model_config = ConfigDict(extra="forbid")

    subscription_id: str
    service_name: str
    final_status: FinalResultStatus
    evidence_count: int
    approval_id: Optional[str] = None
    action_id: Optional[str] = None
    verification_id: Optional[str] = None


class RunReportResponse(BaseModel):
    """Reconciliation and audit report for an agent run."""
    model_config = ConfigDict(extra="forbid")

    run_id: str
    generated_at: datetime
    total_evaluated: int
    total_cancelled_verified: int
    total_kept: int
    total_monthly_savings_usd: float
    items: List[RunReportItem]


# ==============================================================================
# FastAPI Application & Error Handlers
# ==============================================================================

app = FastAPI(
    title="SubCan: An AI-Powered Subscription Cancellation Agent",
    description=(
        "Detect. Decide. Approve. Cancel. Verify.\n\n"
        "SubCan is an AI-assisted FinTech workflow that identifies streaming "
        "subscriptions potentially unused for at least three calendar months, collects "
        "evidence, explains cancellation consequences, requests explicit user approval, "
        "executes only approved actions, and independently verifies the final renewal state."
    ),
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
)


@app.exception_handler(HTTPException)
async def custom_http_exception_handler(request: Request, exc: HTTPException):
    code_map = {
        400: "BAD_REQUEST",
        404: "RESOURCE_NOT_FOUND",
        409: "STATE_CONFLICT",
        422: "VALIDATION_ERROR",
        500: "INTERNAL_SERVER_ERROR",
    }
    code = code_map.get(exc.status_code, "ERROR")
    if isinstance(exc.detail, dict):
        code = exc.detail.get("code", code)
        msg = exc.detail.get("message", str(exc.detail))
        details = exc.detail.get("details", {})
    else:
        msg = str(exc.detail)
        details = {}

    envelope = ErrorEnvelope(
        error=ErrorDetail(
            code=code,
            message=msg,
            details=details,
            timestamp=datetime.now(timezone.utc),
        )
    )
    return JSONResponse(status_code=exc.status_code, content=envelope.model_dump(mode="json"))


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    envelope = ErrorEnvelope(
        error=ErrorDetail(
            code="VALIDATION_ERROR",
            message="Request schema validation failed.",
            details={"errors": exc.errors()},
            timestamp=datetime.now(timezone.utc),
        )
    )
    return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content=envelope.model_dump(mode="json"))


# ==============================================================================
# Registry Lookup Helpers (Process-Local & Thread-Safe)
# ==============================================================================

def find_plan_context(plan_id: str) -> Optional[Tuple[str, str, SubscriptionRunState]]:
    """Locate (run_id, subscription_id, SubscriptionRunState) containing plan_id."""
    registry = get_agent_run_registry()
    with registry._lock:
        for run_id, sub_states in registry._contexts.items():
            for sub_id, state in sub_states.items():
                if state.plan is not None and state.plan.plan_id == plan_id:
                    return run_id, sub_id, state
    return None


def find_action_context(action_id: str) -> Optional[Tuple[str, str, SubscriptionRunState]]:
    """Locate (run_id, subscription_id, SubscriptionRunState) associated with action_id."""
    registry = get_agent_run_registry()
    with registry._lock:
        for run_id, sub_states in registry._contexts.items():
            for sub_id, state in sub_states.items():
                if state.action_result is not None and state.action_result.action_id == action_id:
                    return run_id, sub_id, state
    return None


# ==============================================================================
# API Endpoints
# ==============================================================================

@app.get(
    "/health",
    response_model=HealthCheckResponse,
    tags=["System"],
    summary="Application Health Check",
    description="Returns the operational health status, application title, version, phase, and timestamp.",
)
def get_health() -> HealthCheckResponse:
    """Return health check response."""
    return HealthCheckResponse(
        status="healthy",
        app_title="SubCan: An AI-Powered Subscription Cancellation Agent",
        version="0.1.0",
        phase="Phase 0: Foundation & Team Independence",
        timestamp=datetime.now(timezone.utc),
    )


@app.post(
    "/runs",
    response_model=AgentRun,
    status_code=status.HTTP_201_CREATED,
    tags=["Runs"],
    summary="Initiate Subscription Cancellation Analysis Run",
    description=(
        "Initiates a new AI-agent orchestration run across identified subscriptions.\n"
        "Evaluates inactivity rules (>= 3 calendar months) and generates proposals awaiting approval.\n"
        "SAFETY: Never triggers cancellation execution."
    ),
)
def create_run(req: CreateRunRequest) -> AgentRun:
    """Create and initialize a new agent run."""
    if not req.subscription_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "BAD_REQUEST", "message": "subscription_ids list must not be empty."},
        )

    subscriptions, evidence_map = load_fixture_data(target_subscription_ids=req.subscription_ids)
    found_ids = {s.subscription_id for s in subscriptions}
    missing_ids = set(req.subscription_ids) - found_ids
    if missing_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "BAD_REQUEST",
                "message": f"Unknown or invalid subscription IDs: {sorted(list(missing_ids))}",
            },
        )

    as_of = req.as_of or datetime.now(timezone.utc)
    run, _ = initialize_orchestration_run(
        subscriptions=subscriptions,
        evidence_map=evidence_map,
        as_of=as_of,
    )
    return run


@app.get(
    "/runs/{run_id}",
    response_model=AgentRun,
    tags=["Runs"],
    summary="Get Agent Run Status",
    description="Retrieves the current execution status and aggregated summary of an agent run.",
)
def get_run(run_id: str) -> AgentRun:
    """Retrieve run record by ID."""
    run = get_agent_run_registry().get_run(run_id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "RESOURCE_NOT_FOUND", "message": f"Run ID '{run_id}' not found."},
        )
    return run


@app.get(
    "/runs/{run_id}/subscriptions",
    response_model=RunSubscriptionsResponse,
    tags=["Runs"],
    summary="Get Detailed Subscriptions in a Run",
    description="Returns detailed subscription states, evidence, evaluations, and plan details for a specific run.",
)
def get_run_subscriptions(run_id: str) -> RunSubscriptionsResponse:
    """Retrieve all detailed subscription contexts within a specific run."""
    run = get_agent_run_registry().get_run(run_id)
    contexts = get_agent_run_registry().get_context(run_id)
    if run is None or contexts is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "RESOURCE_NOT_FOUND", "message": f"Run ID '{run_id}' not found."},
        )

    items = [
        RunSubscriptionDetailItem(
            subscription=state.subscription,
            evidence_list=state.evidence_list,
            decision=state.decision,
            plan=state.plan,
            approval=state.approval,
            action_result=state.action_result,
            verification_result=state.verification_result,
            final_status=state.final_status,
            diagnostic_notes=state.diagnostic_notes,
        )
        for state in contexts.values()
    ]
    return RunSubscriptionsResponse(run_id=run.run_id, items=items)


@app.post(
    "/plans/{plan_id}/approve",
    response_model=Approval,
    status_code=status.HTTP_200_OK,
    tags=["Approvals"],
    summary="Approve Cancellation Plan for Execution",
    description=(
        "Explicitly approves a single, specific cancellation plan for execution.\n"
        "Validates against exact service name, plan version, and subscription.\n"
        "SAFETY: Approving one plan never authorizes actions for other subscriptions."
    ),
)
def approve_plan(plan_id: str, req: ApprovePlanRequest) -> Approval:
    """Validate explicit human approval and coordinate execution & verification."""
    located = find_plan_context(plan_id)
    if located is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "RESOURCE_NOT_FOUND", "message": f"Plan ID '{plan_id}' does not exist."},
        )

    run_id, sub_id, state = located
    plan = state.plan
    assert plan is not None

    if plan_id != req.plan_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "BAD_REQUEST",
                "message": f"Path plan_id '{plan_id}' does not match body plan_id '{req.plan_id}'.",
            },
        )

    if req.subscription_id != plan.subscription_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "BAD_REQUEST",
                "message": f"Subscription ID mismatch: expected '{plan.subscription_id}', received '{req.subscription_id}'.",
            },
        )

    if req.service_name.lower() != plan.service_name.lower():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "BAD_REQUEST",
                "message": f"Service name mismatch: expected '{plan.service_name}', received '{req.service_name}'.",
            },
        )

    if req.plan_version != plan.plan_version:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "BAD_REQUEST",
                "message": f"Plan version mismatch: expected '{plan.plan_version}', received '{req.plan_version}'. Stale approval rejected.",
            },
        )

    # State transition check
    is_idempotent_replay = state.action_result is not None and req.idempotency_key is not None
    if plan.status != ApprovalStatus.AWAITING_APPROVAL and not is_idempotent_replay:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "STATE_CONFLICT",
                "message": f"Plan '{plan_id}' is not in 'awaiting_approval' status (current status: '{plan.status.value}').",
            },
        )

    decided_at = req.decided_at or datetime.now(timezone.utc)
    approval = Approval(
        approval_id=f"appr_{plan.subscription_id}_{int(decided_at.timestamp())}",
        plan_id=plan.plan_id,
        subscription_id=plan.subscription_id,
        service_name=plan.service_name,
        plan_version=plan.plan_version,
        status=ApprovalStatus.APPROVED,
        approved_by=req.approved_by,
        decided_at=decided_at,
        notes=req.notes,
    )

    try:
        updated_state = process_subscription_approval(
            run_id=run_id,
            plan_id=plan.plan_id,
            approval=approval,
            idempotency_key=req.idempotency_key,
            observed_subscription=req.observed_subscription,
            post_action_evidence=req.post_action_evidence,
            as_of=decided_at,
        )
    except IdempotencyConflictError as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "IDEMPOTENCY_CONFLICT", "message": str(e)},
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "APPROVAL_ERROR", "message": str(e)},
        )

    if updated_state.final_status == FinalResultStatus.BLOCKED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "APPROVAL_REJECTED",
                "message": updated_state.diagnostic_notes or "Approval validation failed closed.",
            },
        )

    return approval


@app.post(
    "/plans/{plan_id}/decline",
    response_model=Approval,
    status_code=status.HTTP_200_OK,
    tags=["Approvals"],
    summary="Decline Cancellation Plan",
    description="Declines cancellation of a proposed plan, keeping the subscription active.",
)
def decline_plan(plan_id: str, req: DeclinePlanRequest) -> Approval:
    """Register explicit user decline for a cancellation plan."""
    located = find_plan_context(plan_id)
    if located is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "RESOURCE_NOT_FOUND", "message": f"Plan ID '{plan_id}' does not exist."},
        )

    run_id, sub_id, state = located
    plan = state.plan
    assert plan is not None

    if plan_id != req.plan_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "BAD_REQUEST",
                "message": f"Path plan_id '{plan_id}' does not match body plan_id '{req.plan_id}'.",
            },
        )

    if req.plan_version != plan.plan_version:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "BAD_REQUEST",
                "message": f"Plan version mismatch: expected '{plan.plan_version}', received '{req.plan_version}'.",
            },
        )

    if plan.status != ApprovalStatus.AWAITING_APPROVAL:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "STATE_CONFLICT",
                "message": f"Plan '{plan_id}' is not in 'awaiting_approval' status (current status: '{plan.status.value}').",
            },
        )

    decided_at = req.decided_at or datetime.now(timezone.utc)
    approval = Approval(
        approval_id=f"appr_{plan.subscription_id}_{int(decided_at.timestamp())}",
        plan_id=plan.plan_id,
        subscription_id=plan.subscription_id,
        service_name=plan.service_name,
        plan_version=plan.plan_version,
        status=ApprovalStatus.DECLINED,
        approved_by=req.declined_by,
        decided_at=decided_at,
        notes=req.notes,
    )

    process_subscription_approval(
        run_id=run_id,
        plan_id=plan.plan_id,
        approval=approval,
        as_of=decided_at,
    )

    return approval


@app.post(
    "/actions/{action_id}/verify",
    response_model=VerificationResult,
    status_code=status.HTTP_200_OK,
    tags=["Verification"],
    summary="Independently Verify Action Execution Outcome",
    description=(
        "Triggers independent verification of the subscription status following an action execution.\n"
        "SAFETY: Action success is never treated as proof of cancellation. Verification is independent."
    ),
)
def verify_action(action_id: str, req: VerifyActionRequest) -> VerificationResult:
    """Perform independent verification check for an executed cancellation action."""
    action = get_action_registry().get(action_id)
    if action is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "RESOURCE_NOT_FOUND", "message": f"Action ID '{action_id}' not found."},
        )

    if action.status in (ActionStatus.PENDING, ActionStatus.EXECUTING):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "BAD_REQUEST",
                "message": f"Action '{action_id}' has not completed execution (current status: '{action.status.value}').",
            },
        )

    located = find_action_context(action_id)
    service_name = located[2].plan.service_name if located and located[2].plan else None

    as_of = req.as_of or datetime.now(timezone.utc)
    verif = verify_cancellation(
        action_result=action,
        observed_subscription=req.observed_subscription,
        evidence_list=req.evidence_list,
        service_name=service_name,
        method=req.method,
        as_of=as_of,
    )

    # Sync back to run context if present
    if located is not None:
        run_id, sub_id, state = located
        state.verification_result = verif
        if verif.status == VerificationStatus.VERIFIED:
            state.final_status = FinalResultStatus.VERIFIED_CANCELLED
            state.diagnostic_notes = "Action executed and cancellation independently verified."
        elif verif.status == VerificationStatus.FAILED:
            state.final_status = FinalResultStatus.FAILED
            state.diagnostic_notes = f"Verification failed: {verif.details}"
        else:
            state.final_status = FinalResultStatus.PENDING
            state.diagnostic_notes = "Awaiting independent verification evidence."

        run = get_agent_run_registry().get_run(run_id)
        contexts = get_agent_run_registry().get_context(run_id)
        if run and contexts:
            run.final_results[sub_id] = state.final_status
            _reconcile_run_summary(run, contexts, as_of=as_of)

    return verif


@app.get(
    "/runs/{run_id}/report",
    response_model=RunReportResponse,
    tags=["Reporting"],
    summary="Get Agent Run Audit and Reconciliation Report",
    description="Generates a full audit trail and reconciliation report for a completed run.",
)
def get_run_report(run_id: str) -> RunReportResponse:
    """Generate comprehensive audit report for a specified run."""
    run = get_agent_run_registry().get_run(run_id)
    contexts = get_agent_run_registry().get_context(run_id)
    if run is None or contexts is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "RESOURCE_NOT_FOUND", "message": f"Run ID '{run_id}' not found."},
        )

    items: List[RunReportItem] = []
    for sub_id, state in contexts.items():
        items.append(
            RunReportItem(
                subscription_id=state.subscription.subscription_id,
                service_name=state.subscription.service_name,
                final_status=state.final_status,
                evidence_count=len(state.evidence_list),
                approval_id=state.approval.approval_id if state.approval else None,
                action_id=state.action_result.action_id if state.action_result else None,
                verification_id=state.verification_result.verification_id if state.verification_result else None,
            )
        )

    total_evaluated = len(contexts)
    total_cancelled_verified = sum(1 for s in contexts.values() if s.final_status == FinalResultStatus.VERIFIED_CANCELLED)
    total_kept = sum(1 for s in contexts.values() if s.final_status == FinalResultStatus.KEPT)
    total_monthly_savings_usd = round(
        sum(
            s.subscription.monthly_cost or 0.0
            for s in contexts.values()
            if s.final_status == FinalResultStatus.VERIFIED_CANCELLED
        ),
        2,
    )

    return RunReportResponse(
        run_id=run.run_id,
        generated_at=datetime.now(timezone.utc),
        total_evaluated=total_evaluated,
        total_cancelled_verified=total_cancelled_verified,
        total_kept=total_kept,
        total_monthly_savings_usd=total_monthly_savings_usd,
        items=items,
    )
