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

import backend.app.config
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import os
import re
import secrets
import threading
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
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
from backend.app.connectors import (
    fetch_subscriptions,
    load_connector_data,
)
from backend.app.models import (
    ActionResult,
    ActionStatus,
    AgentRun,
    Approval,
    ApprovalStatus,
    CancellationPlan,
    CandidateReviewStatus,
    Decision,
    DiscoverySource,
    EmailAccountStatus,
    Evidence,
    FinalResultStatus,
    HealthCheckResponse,
    Subscription,
    SubscriptionCandidate,
    SubscriptionCreateRequest,
    SubscriptionStatus,
    SubscriptionUpdateRequest,
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


class DatabaseAuditRequest(BaseModel):
    """Payload to trigger database audit and reconciliation."""
    model_config = ConfigDict(extra="forbid")

    dry_run: Optional[bool] = Field(
        default=True,
        description="If True, only reports findings without applying changes. If False, executes cleanup and reconciliation.",
    )


# ==============================================================================
# FastAPI Application & Error Handlers
# ==============================================================================

@asynccontextmanager
async def app_lifespan(app: FastAPI):
    """Initialize database schema and ensure clean real-user state on boot."""
    from backend.app.database import initialize_schema, cleanup_demo_and_mock_data
    initialize_schema()
    cleanup_demo_and_mock_data(user_id="user_default")
    yield


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
    lifespan=app_lifespan,
)

# CORS Configuration
raw_origins = os.getenv(
    "CORS_ORIGINS",
    "https://subcan.onrender.com,http://localhost:8000,http://127.0.0.1:8000,http://localhost:3000,http://127.0.0.1:3000,http://localhost:5500,http://127.0.0.1:5500",
)
allowed_origins = [o.strip() for o in raw_origins.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins if allowed_origins else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def add_no_cache_for_dashboard(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/dashboard"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


app.mount(
    "/dashboard",
    StaticFiles(directory=Path(__file__).resolve().parents[2] / "frontend", html=True),
    name="dashboard",
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
    safe_errors = []
    for err in exc.errors():
        safe_err = dict(err)
        if "ctx" in safe_err and isinstance(safe_err["ctx"], dict):
            safe_err["ctx"] = {k: str(v) for k, v in safe_err["ctx"].items()}
        safe_errors.append(safe_err)
    envelope = ErrorEnvelope(
        error=ErrorDetail(
            code="VALIDATION_ERROR",
            message="Request schema validation failed.",
            details={"errors": safe_errors},
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
        for run_id, sub_states in reversed(list(registry._contexts.items())):
            for sub_id, state in sub_states.items():
                if state.plan is not None and state.plan.plan_id == plan_id:
                    return run_id, sub_id, state
    return None


def find_action_context(action_id: str) -> Optional[Tuple[str, str, SubscriptionRunState]]:
    """Locate (run_id, subscription_id, SubscriptionRunState) associated with action_id."""
    registry = get_agent_run_registry()
    with registry._lock:
        for run_id, sub_states in reversed(list(registry._contexts.items())):
            for sub_id, state in sub_states.items():
                if state.action_result is not None and state.action_result.action_id == action_id:
                    return run_id, sub_id, state
    return None


def get_current_user_id(request: Request) -> str:
    """Extract authenticated user / tenant identity from request headers or query params."""
    user_id = request.headers.get("X-User-ID") or request.query_params.get("user_id")
    if user_id and user_id.strip():
        cleaned = "".join(c for c in user_id.strip() if c.isalnum() or c in ("_", "-"))
        if cleaned:
            return cleaned[:50]
    return "user_default"


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


@app.get(
    "/ready",
    tags=["System"],
    summary="Readiness Check",
    description="Validates that database persistence, connector registry, and email subsystem are operational.",
)
def get_readiness() -> Dict[str, Any]:
    """Check readiness of persistence and subsystem dependencies."""
    from backend.app.database import get_db_session
    checks = {}
    is_ready = True

    # 1. Database check
    try:
        with get_db_session() as conn:
            conn.execute("SELECT 1").fetchone()
            checks["database"] = "ok"
    except Exception as e:
        checks["database"] = f"error: {str(e)}"
        is_ready = False

    # 2. Connector registry check
    try:
        from backend.app.connectors import get_default_registry
        reg = get_default_registry()
        checks["connectors"] = f"ok ({len(reg.list_connectors())} registered)"
    except Exception as e:
        checks["connectors"] = f"error: {str(e)}"
        is_ready = False

    # 3. Environment & OAuth mode
    from backend.app.email.oauth import is_live_oauth_configured
    checks["oauth_mode"] = "live" if is_live_oauth_configured() else "local-demo"

    if not is_ready:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "SERVICE_UNAVAILABLE", "message": "Readiness checks failed", "details": checks},
        )

    return {
        "status": "ready",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
    }


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
def create_run(req: CreateRunRequest, request: Request) -> AgentRun:
    """Create and initialize a new agent run across connectors, email, and manual subscriptions."""
    user_id = get_current_user_id(request)
    if not req.subscription_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "BAD_REQUEST", "message": "subscription_ids list must not be empty."},
        )

    as_of = req.as_of or datetime.now(timezone.utc)
    try:
        subscriptions, evidence_map = load_connector_data(
            target_subscription_ids=req.subscription_ids,
            as_of=as_of,
        )
    except Exception:
        subscriptions, evidence_map = load_fixture_data(target_subscription_ids=req.subscription_ids)

    # Check for any subscription IDs stored in SQLite database (manual entry or email discovery)
    found_ids = {s.subscription_id for s in subscriptions}
    missing_ids = set(req.subscription_ids) - found_ids
    if missing_ids:
        from backend.app.database import get_subscription_from_db
        still_missing = []
        for m_id in sorted(list(missing_ids)):
            db_sub = get_subscription_from_db(m_id, user_id=user_id)
            if db_sub:
                subscriptions.append(db_sub)
                if m_id not in evidence_map:
                    evidence_map[m_id] = []
                    if db_sub.last_used_at:
                        evidence_map[m_id].append(
                            Evidence(
                                evidence_id=f"ev_usage_{m_id}",
                                subscription_id=m_id,
                                evidence_type="usage_history",
                                excerpt=f"Activity recorded on {db_sub.last_used_at.isoformat()}",
                                captured_at=db_sub.last_used_at if db_sub.last_used_at.tzinfo else datetime.now(timezone.utc),
                                confidence=db_sub.confidence or 0.9,
                            )
                        )
            else:
                still_missing.append(m_id)
        if still_missing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "code": "BAD_REQUEST",
                    "message": f"Unknown or invalid subscription IDs: {still_missing}",
                },
            )

    run, _ = initialize_orchestration_run(
        subscriptions=subscriptions,
        evidence_map=evidence_map,
        as_of=as_of,
        user_id=user_id,
    )
    return run


@app.get(
    "/subscriptions",
    response_model=List[Subscription],
    tags=["Subscriptions"],
    summary="List available subscriptions in unified inventory",
    description="Returns connector-provided, email-discovered, and manually entered subscriptions with optional pagination.",
)
def list_subscriptions(
    request: Request,
    include_archived: bool = False,
    status: Optional[str] = None,
    source: Optional[str] = None,
    limit: Optional[int] = None,
    offset: int = 0,
) -> List[Subscription]:
    """Expose unified subscription inventory for genuine user-confirmed and manual subscriptions."""
    user_id = get_current_user_id(request)
    from backend.app.database import list_subscriptions_from_db

    # Only load synthetic connector fixtures when explicitly requested via source="demo" or source="connector"
    if source in ("demo", "connector"):
        from backend.app.connectors import fetch_subscriptions
        from backend.app.agent import load_fixture_data
        try:
            connector_subs = fetch_subscriptions()
        except Exception:
            connector_subs, _ = load_fixture_data()
        result = list(connector_subs)
    else:
        # Real-data-only mode: only genuine database records for the requesting user
        db_records = list_subscriptions_from_db(
            user_id=user_id,
            include_archived=include_archived,
        )
        if source:
            if source in ("gmail", "email"):
                result = [s for s in db_records if s.discovery_source in ("gmail", "email")]
            else:
                result = [s for s in db_records if s.discovery_source == source]
        else:
            result = [s for s in db_records if s.discovery_source in ("email", "gmail", "manual")]

    if status:
        result = [s for s in result if s.status.value == status]

    # Optional pagination
    if offset > 0:
        result = result[offset:]
    if limit is not None and limit > 0:
        result = result[:limit]

    return result


@app.post(
    "/subscriptions",
    response_model=Subscription,
    status_code=status.HTTP_201_CREATED,
    tags=["Subscriptions"],
    summary="Create subscription manually",
    description="Allows users to manually track a subscription not automatically discovered.",
)
def create_manual_subscription(req: SubscriptionCreateRequest, request: Request) -> Subscription:
    """Manually register a new subscription into the unified inventory."""
    user_id = get_current_user_id(request)
    import secrets
    clean_name = re.sub(r"[^a-zA-Z0-9]", "_", req.service_name).lower()
    sub_id = f"sub_man_{clean_name}_{secrets.token_hex(4)}"

    # Check for potential duplicates conservatively
    from backend.app.database import list_subscriptions_from_db, save_subscription
    existing_user_subs = list_subscriptions_from_db(user_id=user_id, include_archived=False)
    similar = [
        s for s in existing_user_subs
        if s.service_name.lower() == req.service_name.strip().lower()
    ]
    note_prefix = ""
    if similar:
        note_prefix = f"[Notice: Existing subscription '{similar[0].subscription_id}' also found for {req.service_name}] "

    combined_notes = (note_prefix + (req.notes or "")).strip() or None

    sub = Subscription(
        subscription_id=sub_id,
        service_name=req.service_name.strip(),
        status=req.status or SubscriptionStatus.ACTIVE,
        renewal_date=req.renewal_date,
        last_used_at=req.last_used_at,
        monthly_cost=req.monthly_cost,
        currency=req.currency or "INR",
        original_amount=req.original_amount or req.monthly_cost,
        original_currency=req.original_currency or req.currency or "INR",
        billing_cycle=req.billing_cycle or "monthly",
        cancellation_terms_url=req.cancellation_terms_url,
        category=req.category,
        user_id=user_id,
        discovery_source=DiscoverySource.MANUAL.value,
        confidence=1.0,
        notes=combined_notes,
        is_archived=False,
        manual_action_url=req.manual_action_url,
        manual_action_instructions=req.manual_action_instructions,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    save_subscription(sub)
    return sub


@app.get(
    "/subscriptions/{subscription_id}",
    response_model=Subscription,
    tags=["Subscriptions"],
    summary="Get single subscription",
    description="Retrieve detailed record for a specific subscription.",
)
def get_single_subscription(subscription_id: str, request: Request) -> Subscription:
    """Retrieve a subscription by ID."""
    from backend.app.database import get_subscription_from_db
    user_id = get_current_user_id(request)
    sub = get_subscription_from_db(subscription_id, user_id=user_id)
    if not sub:
        try:
            from backend.app.connectors import get_subscription as get_conn_sub
            sub = get_conn_sub(subscription_id)
        except Exception:
            sub = None
    if not sub:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "RESOURCE_NOT_FOUND", "message": f"Subscription '{subscription_id}' not found."},
        )
    if sub.user_id and sub.user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "RESOURCE_NOT_FOUND", "message": f"Subscription '{subscription_id}' not found."},
        )
    return sub


@app.put(
    "/subscriptions/{subscription_id}",
    response_model=Subscription,
    tags=["Subscriptions"],
    summary="Update subscription details",
    description="Update permitted details of an existing subscription.",
)
def update_subscription(
    subscription_id: str,
    req: SubscriptionUpdateRequest,
    request: Request,
) -> Subscription:
    """Update details of an existing subscription."""
    from backend.app.database import get_subscription_from_db, save_subscription
    user_id = get_current_user_id(request)
    sub = get_subscription_from_db(subscription_id, user_id=user_id)
    if not sub:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "RESOURCE_NOT_FOUND", "message": f"Subscription '{subscription_id}' not found."},
        )
    if sub.user_id and sub.user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "RESOURCE_NOT_FOUND", "message": f"Subscription '{subscription_id}' not found."},
        )

    data = sub.model_dump()
    for field, val in req.model_dump(exclude_unset=True).items():
        if val is not None:
            data[field] = val
    data["updated_at"] = datetime.now(timezone.utc)
    updated_sub = Subscription(**data)
    save_subscription(updated_sub)
    return updated_sub


@app.delete(
    "/subscriptions/{subscription_id}",
    status_code=status.HTTP_200_OK,
    tags=["Subscriptions"],
    summary="Archive subscription",
    description="Safely archives a subscription from active monitoring.",
)
def archive_subscription(subscription_id: str, request: Request) -> Dict[str, Any]:
    """Archive a subscription from active inventory."""
    from backend.app.database import archive_subscription_in_db, get_subscription_from_db
    user_id = get_current_user_id(request)
    sub = get_subscription_from_db(subscription_id, user_id=user_id)
    if not sub:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "RESOURCE_NOT_FOUND", "message": f"Subscription '{subscription_id}' not found."},
        )
    if sub.user_id and sub.user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "RESOURCE_NOT_FOUND", "message": f"Subscription '{subscription_id}' not found."},
        )
    archive_subscription_in_db(subscription_id, user_id=user_id)
    return {"archived": True, "status": "archived", "subscription_id": subscription_id}


# ==============================================================================
# Email Discovery Endpoints
# ==============================================================================

@app.get(
    "/api/email/oauth/url",
    tags=["Email Discovery"],
    summary="Get Google OAuth Authorization URL",
    description="Generates a Google OAuth authorization URL with PKCE and state protection.",
)
def get_email_oauth_url(
    request: Request,
    account_name: Optional[str] = None,
    account_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Return OAuth authorization URL and state token."""
    from backend.app.email import generate_oauth_url, is_live_oauth_configured
    user_id = get_current_user_id(request)
    url, state = generate_oauth_url(user_id=user_id, account_name=account_name, account_id=account_id)
    is_live = is_live_oauth_configured()
    return {
        "url": url,
        "state": state,
        "is_live_configured": is_live,
        "mode": "live" if is_live else "demo",
        "provider": "gmail",
    }


@app.get(
    "/api/email/accounts",
    response_model=List[EmailAccountStatus],
    tags=["Email Discovery"],
    summary="List Connected Email Accounts",
    description="Returns all connected email accounts for the current user.",
)
def list_email_accounts_endpoint(request: Request) -> List[EmailAccountStatus]:
    """List all email accounts connected by the user."""
    from backend.app.database import list_email_accounts
    user_id = get_current_user_id(request)
    return list_email_accounts(user_id=user_id)


@app.patch(
    "/api/email/accounts/{account_id}/nickname",
    tags=["Email Discovery"],
    summary="Update Email Account Nickname",
    description="Explicitly update the nickname/description of a registered email account.",
)
def update_email_account_nickname(
    account_id: str,
    payload: Dict[str, Any],
    request: Request,
) -> Dict[str, Any]:
    """Update custom nickname for an email account."""
    from backend.app.database import update_email_account_name, get_email_account
    user_id = get_current_user_id(request)
    nickname = payload.get("nickname") or payload.get("account_name")
    if not nickname or not str(nickname).strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_NICKNAME", "message": "Nickname cannot be empty."},
        )
    clean_name = str(nickname).strip()
    updated = update_email_account_name(user_id=user_id, account_id=account_id, account_name=clean_name)
    if not updated:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "ACCOUNT_NOT_FOUND", "message": "Email account not found or access denied."},
        )
    account = get_email_account(user_id=user_id, account_id=account_id)
    return {
        "success": True,
        "account_id": account_id,
        "account_name": clean_name,
        "email_address": account.email_address if account else None,
    }


@app.get(
    "/api/email/oauth/callback",
    tags=["Email Discovery"],
    summary="Handle Google OAuth Callback",
    description="Exchanges authorization code for tokens and registers connected email account.",
)
def handle_email_oauth_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
) -> Any:
    """Exchange OAuth code and connect mailbox."""
    from backend.app.email import exchange_code_for_tokens
    user_id = get_current_user_id(request)

    if error:
        accept = request.headers.get("accept", "")
        if "text/html" in accept:
            from fastapi.responses import RedirectResponse
            return RedirectResponse(
                url=f"/dashboard/#email-error?error={urllib.parse.quote(error)}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "OAUTH_DENIED", "message": f"Google authorization denied or failed: {error}"},
        )

    if not code or not state:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "INVALID_CALLBACK", "message": "Missing 'code' or 'state' parameter in OAuth callback."},
        )

    try:
        account_status = exchange_code_for_tokens(code=code, state=state, user_id=user_id, error=error)
        accept = request.headers.get("accept", "")
        if "text/html" in accept:
            from fastapi.responses import RedirectResponse
            return RedirectResponse(url="/dashboard/#email-connected", status_code=status.HTTP_303_SEE_OTHER)
        res = account_status.model_dump()
        res["connected"] = account_status.is_connected
        res["message"] = f"Connected {account_status.email_address} for user {user_id}"
        return res
    except Exception as e:
        accept = request.headers.get("accept", "")
        if "text/html" in accept:
            from fastapi.responses import RedirectResponse
            return RedirectResponse(
                url=f"/dashboard/#email-error?error={urllib.parse.quote(str(e))}",
                status_code=status.HTTP_303_SEE_OTHER,
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "OAUTH_ERROR", "message": str(e)},
        )


@app.get(
    "/api/email/status",
    response_model=EmailAccountStatus,
    tags=["Email Discovery"],
    summary="Get Connected Email Account Status",
    description="Returns connection status, sync status, and candidate count.",
)
def get_email_account_status(request: Request, account_id: Optional[str] = None) -> EmailAccountStatus:
    """Get current email account connection status."""
    from backend.app.database import get_email_account
    from backend.app.config import is_live_oauth_configured
    user_id = get_current_user_id(request)
    account = get_email_account(user_id, account_id=account_id)
    if account:
        return account
    is_live = is_live_oauth_configured()
    return EmailAccountStatus(
        user_id=user_id,
        account_id=account_id,
        is_connected=False,
        is_live_configured=is_live,
        mode="live" if is_live else "demo",
    )


@app.post(
    "/api/email/disconnect",
    tags=["Email Discovery"],
    summary="Disconnect Email Account",
    description="Revokes stored OAuth credentials and disconnects mailbox.",
)
def disconnect_email(request: Request, account_id: Optional[str] = None) -> Dict[str, Any]:
    """Disconnect email account and revoke tokens."""
    from backend.app.email import disconnect_account
    user_id = get_current_user_id(request)
    disconnect_account(user_id, account_id=account_id)
    return {"disconnected": True, "status": "disconnected", "user_id": user_id, "account_id": account_id}


@app.post(
    "/api/email/sync",
    tags=["Email Discovery"],
    summary="Synchronize Billing Emails",
    description="Searches mailbox for billing messages and extracts candidate subscriptions.",
)
def sync_email(request: Request, max_messages: int = 100, account_id: Optional[str] = None) -> Dict[str, Any]:
    """Execute targeted mailbox search and candidate extraction."""
    from backend.app.database import get_email_account
    from backend.app.email import sync_user_mailbox
    user_id = get_current_user_id(request)
    try:
        candidates = sync_user_mailbox(user_id=user_id, account_id=account_id, max_messages=max_messages)
        account = get_email_account(user_id, account_id=account_id)
        return {
            "synced_count": len(candidates),
            "candidates": [c.model_dump() for c in candidates],
            "messages_discovered": getattr(account, "messages_discovered", 0) if account else len(candidates),
            "messages_retrieved": getattr(account, "messages_retrieved", 0) if account else len(candidates),
            "messages_parsed": getattr(account, "messages_parsed", 0) if account else 0,
            "messages_skipped": getattr(account, "messages_skipped", 0) if account else 0,
            "messages_insufficient_evidence": getattr(account, "messages_insufficient_evidence", 0) if account else 0,
            "candidates_created": len(candidates),
            "duplicates_skipped": getattr(account, "duplicates_skipped", 0) if account else 0,
            "parsing_failures": getattr(account, "parsing_failures", 0) if account else 0,
            "is_partial_scan": getattr(account, "is_partial_scan", False) if account else False,
            "scan_limit": getattr(account, "scan_limit", max_messages) if account else max_messages,
            "scan_date_range": getattr(account, "scan_date_range", None) if account else None,
            "skip_reasons": getattr(account, "skip_reasons", {}) if account else {},
            "failure_reasons": getattr(account, "failure_reasons", {}) if account else {},
        }
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "EMAIL_NOT_CONNECTED", "message": str(e)},
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "EMAIL_SYNC_ERROR", "message": str(e)},
        )


@app.get(
    "/api/email/candidates",
    response_model=List[SubscriptionCandidate],
    tags=["Email Discovery"],
    summary="List Discovered Candidates",
    description="Returns subscription candidates extracted from emails for review.",
)
def list_candidates(
    request: Request,
    review_status: Optional[str] = None,
    account_id: Optional[str] = None,
) -> List[SubscriptionCandidate]:
    """List subscription candidates awaiting review."""
    from backend.app.database import list_subscription_candidates
    user_id = get_current_user_id(request)
    return list_subscription_candidates(user_id=user_id, account_id=account_id, review_status=review_status)


@app.post(
    "/api/email/candidates/{candidate_id}/confirm",
    response_model=Subscription,
    tags=["Email Discovery"],
    summary="Confirm Candidate Into Inventory",
    description="Promotes an email candidate into the unified subscription inventory.",
)
def confirm_candidate(candidate_id: str, request: Request) -> Subscription:
    """Confirm candidate and promote to active inventory."""
    from backend.app.email import confirm_candidate_into_inventory
    user_id = get_current_user_id(request)
    try:
        sub, _ = confirm_candidate_into_inventory(candidate_id=candidate_id, user_id=user_id)
        return sub
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "CANDIDATE_NOT_FOUND", "message": str(e)},
        )


@app.post(
    "/api/email/candidates/{candidate_id}/dismiss",
    tags=["Email Discovery"],
    summary="Dismiss Candidate",
    description="Dismisses a false-positive candidate subscription.",
)
def dismiss_candidate_endpoint(candidate_id: str, request: Request) -> Dict[str, Any]:
    """Dismiss a candidate subscription."""
    from backend.app.email import dismiss_candidate
    user_id = get_current_user_id(request)
    try:
        success = dismiss_candidate(candidate_id=candidate_id, user_id=user_id)
        if not success:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": "CANDIDATE_NOT_FOUND", "message": f"Candidate '{candidate_id}' not found."},
            )
        return {"status": "dismissed", "candidate_id": candidate_id}
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "CANDIDATE_NOT_FOUND", "message": str(e)},
        )


@app.get(
    "/api/email/diagnostics",
    tags=["Email Discovery"],
    summary="Get Detailed Email Discovery Diagnostics",
    description="Returns sanitized diagnostic information on OAuth validity, connection status, sync metrics, and error state.",
)
def get_email_diagnostics(request: Request, account_id: Optional[str] = None) -> Dict[str, Any]:
    """Retrieve sanitized diagnostic status for live or demo discovery."""
    from backend.app.config import is_live_oauth_configured, validate_oauth_config
    from backend.app.database import get_email_account, list_subscriptions_from_db
    user_id = get_current_user_id(request)
    is_live = is_live_oauth_configured()
    oauth_valid, oauth_err = validate_oauth_config()
    account = get_email_account(user_id, account_id=account_id)

    db_subs = list_subscriptions_from_db(user_id=user_id, account_id=account_id, include_archived=True)
    email_subs_count = sum(1 for s in db_subs if s.discovery_source == "email")
    manual_subs_count = sum(1 for s in db_subs if s.discovery_source == "manual")

    return {
        "user_id": user_id,
        "account_id": account_id,
        "mode": "live" if is_live else "demo",
        "is_live_configured": is_live,
        "oauth_valid": oauth_valid,
        "oauth_error": oauth_err if not oauth_valid else None,
        "is_connected": account.is_connected if account else False,
        "email_address": account.email_address if account and account.is_connected else None,
        "account_name": getattr(account, "account_name", None) if account else None,
        "provider": account.provider if account else "gmail",
        "last_synced_at": account.last_synced_at.isoformat() if account and account.last_synced_at else None,
        "sync_status": account.sync_status if account else "idle",
        "candidate_count": account.candidate_count if account else 0,
        "candidates_parsed": getattr(account, "candidate_count", 0) if account else 0,
        "messages_discovered": getattr(account, "messages_discovered", 0) if account else 0,
        "messages_retrieved": getattr(account, "messages_retrieved", 0) if account else 0,
        "messages_parsed": getattr(account, "messages_parsed", 0) if account else 0,
        "messages_skipped": getattr(account, "messages_skipped", 0) if account else 0,
        "messages_insufficient_evidence": getattr(account, "messages_insufficient_evidence", 0) if account else 0,
        "duplicates_skipped": getattr(account, "duplicates_skipped", 0) if account else 0,
        "parsing_failures": getattr(account, "parsing_failures", 0) if account else 0,
        "is_partial_scan": getattr(account, "is_partial_scan", False) if account else False,
        "scan_limit": getattr(account, "scan_limit", 100) if account else 100,
        "scan_date_range": getattr(account, "scan_date_range", None) if account else None,
        "skip_reasons": getattr(account, "skip_reasons", {}) if account else {},
        "failure_reasons": getattr(account, "failure_reasons", {}) if account else {},
        "error_message": account.error_message if account else None,
        "inventory_summary": {
            "email_discovered_count": email_subs_count,
            "manual_count": manual_subs_count,
            "demo_mock_count": sum(1 for s in db_subs if (s.discovery_source or "connector") == "connector"),
            "total_monitored_count": email_subs_count + manual_subs_count,
        },
    }


@app.post(
    "/api/demo/reset",
    tags=["Email Discovery"],
    summary="Reset Demo Data",
    description="Development-only endpoint to safely clear synthetic demo candidates without affecting manual or genuine Gmail subscriptions.",
)
def reset_demo_endpoint(request: Request) -> Dict[str, Any]:
    """Safely clear demo candidates and reset demo data."""
    from backend.app.database import reset_demo_data
    user_id = get_current_user_id(request)
    return reset_demo_data(user_id=user_id)


@app.post(
    "/api/database/audit",
    tags=["Database Maintenance"],
    summary="Audit and Reconcile Database",
    description="Inspects database records, identifies mock/synthetic fixtures and false positives, and reconciles candidates.",
)
def audit_database_endpoint(
    request: Request,
    req: Optional[DatabaseAuditRequest] = None,
) -> Dict[str, Any]:
    """Execute database audit and reconciliation."""
    from backend.app.database import audit_and_reconcile_database
    user_id = get_current_user_id(request)
    dry_run = True if req is None or req.dry_run is None else req.dry_run
    return audit_and_reconcile_database(user_id=user_id, dry_run=dry_run)


@app.post(
    "/api/database/repair",
    tags=["Database Maintenance"],
    summary="Repair Mixed-User Data Collisions",
    description="Inspects database, reassigns misattributed candidates and subscriptions based on email provenance, with automatic backup.",
)
def repair_database_endpoint(
    request: Request,
    req: Optional[DatabaseAuditRequest] = None,
) -> Dict[str, Any]:
    """Execute database data collision repair and isolation enforcement."""
    from backend.app.database import repair_mixed_data
    dry_run = True if req is None or req.dry_run is None else req.dry_run
    return repair_mixed_data(dry_run=dry_run)


# ==============================================================================
# User Privacy & Compliance Endpoints (GDPR / CCPA)
# ==============================================================================

@app.get(
    "/api/user/export",
    tags=["User Privacy & Compliance"],
    summary="Export User Account Data",
    description="Exports all user-scoped subscriptions, email metadata, and candidate records per GDPR/CCPA portability requirements.",
)
def export_user_account_data(request: Request) -> Dict[str, Any]:
    """Export user data for data portability compliance."""
    from backend.app.database import export_user_data
    user_id = get_current_user_id(request)
    return export_user_data(user_id=user_id)


@app.delete(
    "/api/user/data",
    tags=["User Privacy & Compliance"],
    summary="Purge User Account Data",
    description="Purges all user-scoped subscriptions, candidate records, and stored OAuth tokens for account deletion.",
)
def purge_user_account_data(request: Request) -> Dict[str, Any]:
    """Purge all user data for right-to-be-forgotten compliance."""
    from backend.app.database import delete_user_data
    user_id = get_current_user_id(request)
    return delete_user_data(user_id=user_id)


# ==============================================================================
# Restricted Website & Manual Action Guides
# ==============================================================================

@app.get(
    "/services/{service_id}/manual-guide",
    tags=["Connectors"],
    summary="Get Manual Action Instructions for Restricted Services",
    description="Returns official cancellation portal URL and step-by-step instructions when automation is restricted.",
)
def get_manual_action_guide(service_id: str) -> Dict[str, Any]:
    """Provide safe manual fallback instructions for providers that block automation."""
    s_key = service_id.lower().replace("-", "_")
    service_guides = {
        "bloomberg": {
            "service_name": "Bloomberg Terminal / Professional",
            "official_url": "https://service.bloomberg.com/portal/subscriptions",
            "requires_manual_action": True,
            "restricted_reason": "Bloomberg requires dedicated corporate representative contact or physical terminal decommission.",
            "steps": [
                "Log in to the Bloomberg Professional Client Portal via authorized corporate credentials.",
                "Navigate to 'Account Administration' -> 'Subscription & Terminals'.",
                "Submit a terminal decommission request to your dedicated Bloomberg account executive.",
                "Confirm receipt of the written contract termination confirmation.",
            ],
            "data_loss_warning": "Immediate loss of historical terminal logs, specialized analytics, and Bloomberg Vault archives.",
        },
        "adobe": {
            "service_name": "Adobe Creative Cloud",
            "official_url": "https://account.adobe.com/plans",
            "requires_manual_action": True,
            "restricted_reason": "Adobe restricts automated cancellation via CAPTCHA and early-termination fee disclosures.",
            "steps": [
                "Log into your Adobe Account Management console at account.adobe.com/plans.",
                "Find your active Creative Cloud plan and select 'Manage plan'.",
                "Click 'Cancel your plan' and follow the step-by-step confirmation prompts.",
                "Review any applicable early cancellation fees before finalizing.",
            ],
            "data_loss_warning": "Cloud storage allocation drops to 2 GB; files exceeding this limit may be deleted after 30 days.",
        },
        "wsj": {
            "service_name": "The Wall Street Journal",
            "official_url": "https://customercenter.wsj.com/cancel",
            "requires_manual_action": True,
            "restricted_reason": "WSJ enforces telephone or chat-based customer retention flow.",
            "steps": [
                "Visit customercenter.wsj.com and sign into your subscriber profile.",
                "Select 'Manage Subscription' -> 'Cancel Online' (where available) or initiate live support chat.",
                "Decline retention offers and explicitly state your request to cancel renewal.",
                "Save the confirmation email and cancellation reference code.",
            ],
            "data_loss_warning": "Immediate loss of subscriber-only archives, newsletters, and digital edition access.",
        },
        "gym_in_person": {
            "service_name": "Physical Health Club / Gym",
            "official_url": "https://mygym.example.com/membership",
            "requires_manual_action": True,
            "restricted_reason": "Facility requires certified mail notice or in-person cancellation form.",
            "steps": [
                "Obtain the facility's official Membership Cancellation Request form.",
                "Complete the form including your member barcode and effective date.",
                "Deliver in person to the club manager or send via USPS Certified Mail with return receipt.",
                "Retain proof of delivery and your postmarked receipt.",
            ],
            "data_loss_warning": "Locker clearance required within 48 hours; key fob access deactivated at cycle end.",
        },
    }

    if s_key in service_guides:
        return service_guides[s_key]

    return {
        "service_name": service_id,
        "official_url": f"https://www.{service_id.lower()}.com/account",
        "requires_manual_action": True,
        "restricted_reason": "Automated cancellation not supported by provider API.",
        "steps": [
            f"Navigate to the official {service_id} account management portal.",
            "Log in using your verified registered credentials.",
            "Go to 'Subscription' or 'Billing' settings and choose 'Cancel Subscription'.",
            "Complete on-screen confirmation steps and retain the cancellation receipt number.",
        ],
        "data_loss_warning": "Service access and stored cloud resources will terminate at current billing cycle end.",
    }


@app.get(
    "/runs/{run_id}",
    response_model=AgentRun,
    tags=["Runs"],
    summary="Get Agent Run Status",
    description="Retrieves the current execution status and aggregated summary of an agent run.",
)
def get_run(run_id: str, request: Request) -> AgentRun:
    """Retrieve run record by ID."""
    user_id = get_current_user_id(request)
    run = get_agent_run_registry().get_run(run_id)
    if run is None or (run.user_id and run.user_id != user_id):
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
def get_run_subscriptions(run_id: str, request: Request) -> RunSubscriptionsResponse:
    """Retrieve all detailed subscription contexts within a specific run."""
    user_id = get_current_user_id(request)
    run = get_agent_run_registry().get_run(run_id)
    contexts = get_agent_run_registry().get_context(run_id)
    if run is None or contexts is None or (run.user_id and run.user_id != user_id):
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
def approve_plan(plan_id: str, req: ApprovePlanRequest, request: Request) -> Approval:
    """Validate explicit human approval and coordinate execution & verification."""
    user_id = get_current_user_id(request)
    located = find_plan_context(plan_id)
    if located is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "RESOURCE_NOT_FOUND", "message": f"Plan ID '{plan_id}' does not exist."},
        )

    run_id, sub_id, state = located
    run = get_agent_run_registry().get_run(run_id)
    if run and run.user_id and run.user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "RESOURCE_NOT_FOUND", "message": f"Plan ID '{plan_id}' does not exist."},
        )

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
def decline_plan(plan_id: str, req: DeclinePlanRequest, request: Request) -> Approval:
    """Register explicit user decline for a cancellation plan."""
    user_id = get_current_user_id(request)
    located = find_plan_context(plan_id)
    if located is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "RESOURCE_NOT_FOUND", "message": f"Plan ID '{plan_id}' does not exist."},
        )

    run_id, sub_id, state = located
    run = get_agent_run_registry().get_run(run_id)
    if run and run.user_id and run.user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "RESOURCE_NOT_FOUND", "message": f"Plan ID '{plan_id}' does not exist."},
        )

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
    located = find_action_context(action_id)
    if action is None and located is not None:
        action = located[2].action_result
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
            get_agent_run_registry().save_run(run, contexts)

    return verif


@app.get(
    "/runs/{run_id}/report",
    response_model=RunReportResponse,
    tags=["Reporting"],
    summary="Get Agent Run Audit and Reconciliation Report",
    description="Generates a full audit trail and reconciliation report for a completed run.",
)
def get_run_report(run_id: str, request: Request) -> RunReportResponse:
    """Generate comprehensive audit report for a specified run."""
    user_id = get_current_user_id(request)
    run = get_agent_run_registry().get_run(run_id)
    contexts = get_agent_run_registry().get_context(run_id)
    if run is None or contexts is None or (run.user_id and run.user_id != user_id):
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


@app.get(
    "/runs/{run_id}/audit",
    response_model=List[Dict[str, Any]],
    tags=["Reporting"],
    summary="Get Immutable Audit Events for a Run",
    description="Returns chronological audit trail events persisted in the durable SQLite store.",
)
def get_run_audit_trail(run_id: str, request: Request) -> List[Dict[str, Any]]:
    """Retrieve persisted audit events for a specified run."""
    from backend.app.database import list_audit_events
    user_id = get_current_user_id(request)
    run = get_agent_run_registry().get_run(run_id)
    if run is not None and run.user_id and run.user_id != user_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "RESOURCE_NOT_FOUND", "message": f"Run ID '{run_id}' not found."},
        )
    return list_audit_events(run_id, user_id=user_id)


@app.get(
    "/api/test-report",
    response_model=List[Any],
    tags=["Testing"],
    summary="Get Official Acceptance Test Results",
    description="Runs and retrieves the official acceptance test scorecard covering TC1-TC4 and edge cases.",
)
def get_official_test_report() -> List[Any]:
    """Execute and return official test suite results."""
    from backend.app.acceptance import run_official_test_suite
    return [r.model_dump() for r in run_official_test_suite()]

