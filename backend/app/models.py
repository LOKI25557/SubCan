"""Shared Pydantic domain models and standardized enums for SubCan.

Phase 0: Foundation & Team Independence.
Provides validated schemas shared between:
- Chinmay (connectors, evidence extraction, mock site feeds)
- Loki (backend orchestration, rules, planner, approval, action, verification)
- Dadu (database schema, frontend dashboard, test harnesses)
"""

from datetime import date, datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator


# ==============================================================================
# Standardised Status Enums
# ==============================================================================

class SubscriptionStatus(str, Enum):
    """Lifecycle status of a tracked subscription."""
    ACTIVE = "active"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class EligibilityStatus(str, Enum):
    """Eligibility classification for potential cancellation."""
    CANDIDATE = "candidate"
    NOT_ELIGIBLE = "not_eligible"
    NEEDS_REVIEW = "needs_review"


class ApprovalStatus(str, Enum):
    """Human-in-the-loop approval workflow states."""
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    DECLINED = "declined"
    INVALIDATED = "invalidated"


class ActionStatus(str, Enum):
    """Execution state of an automated or staged cancellation action.

    CRITICAL SAFETY RULE:
    An action status of 'succeeded' indicates that execution was attempted
    without runtime failure. It MUST NEVER be treated as proof that the
    subscription is cancelled. Independent verification is mandatory.
    """
    PENDING = "pending"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"


class VerificationStatus(str, Enum):
    """Independent verification status of the final subscription state."""
    VERIFIED = "verified"
    FAILED = "failed"
    PENDING = "pending"


class FinalResultStatus(str, Enum):
    """Final reconciled result of an agent run for a subscription."""
    VERIFIED_CANCELLED = "verified_cancelled"
    KEPT = "kept"
    BLOCKED = "blocked"
    FAILED = "failed"
    PENDING = "pending"


class DiscoverySource(str, Enum):
    """Origin of a subscription record."""
    CONNECTOR = "connector"
    EMAIL = "email"
    MANUAL = "manual"


class CandidateReviewStatus(str, Enum):
    """Review lifecycle status for an email-discovered subscription candidate."""
    PENDING = "pending"
    CONFIRMED = "confirmed"
    DISMISSED = "dismissed"


# ==============================================================================
# Shared Domain Models
# ==============================================================================

class Subscription(BaseModel):
    """Core subscription entity.

    Represents a streaming subscription identified for evaluation.
    Missing usage history must be represented as None/null rather than
    inventing a synthetic date.
    """
    model_config = ConfigDict(extra="forbid")

    subscription_id: str = Field(
        ...,
        description="Unique identifier for the subscription entity (e.g. 'sub_tc1_netflix')."
    )
    service_name: str = Field(
        ...,
        description="Name of the streaming service (e.g. 'Netflix', 'Spotify')."
    )
    status: SubscriptionStatus = Field(
        ...,
        description="Current recorded status of the subscription."
    )
    renewal_date: Optional[date] = Field(
        default=None,
        description="Next scheduled renewal date in ISO format (YYYY-MM-DD)."
    )
    last_used_at: Optional[datetime] = Field(
        default=None,
        description="Timestamp of last detected user streaming activity. None if unknown."
    )
    source_url: Optional[str] = Field(
        default=None,
        description="Originating mock site or connector URL."
    )
    source_record_id: Optional[str] = Field(
        default=None,
        description="Identifier within the connector source system."
    )
    monthly_cost: Optional[float] = Field(
        default=None,
        description="Monthly billing amount in currency units."
    )
    currency: Optional[str] = Field(
        default="USD",
        description="ISO 4217 currency code (e.g. 'USD')."
    )
    billing_cycle: Optional[str] = Field(
        default="monthly",
        description="Billing cycle interval (e.g. 'monthly', 'annual')."
    )
    cancellation_terms_url: Optional[str] = Field(
        default=None,
        description="Direct link to cancellation policies or terms of service."
    )
    data_deletion_warning: bool = Field(
        default=False,
        description="Flag indicating cancellation triggers immediate data loss."
    )
    pause_option_available: bool = Field(
        default=False,
        description="Flag indicating the provider offers a pause alternative."
    )
    user_id: Optional[str] = Field(
        default="user_default",
        description="Tenant or user identifier owning this subscription."
    )
    category: Optional[str] = Field(
        default=None,
        description="Subscription category (e.g., 'streaming', 'cloud', 'fitness')."
    )
    discovery_source: Optional[str] = Field(
        default="connector",
        description="Source provenance: 'connector', 'email', or 'manual'."
    )
    confidence: Optional[float] = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        description="Confidence score of extracted or supplied details."
    )
    notes: Optional[str] = Field(
        default=None,
        description="User or system notes associated with this subscription."
    )
    is_archived: bool = Field(
        default=False,
        description="Whether this subscription is archived from active inventory."
    )
    manual_action_url: Optional[str] = Field(
        default=None,
        description="Official account or cancellation portal URL for manual workflows."
    )
    manual_action_instructions: Optional[str] = Field(
        default=None,
        description="Human-readable step-by-step instructions for manual cancellation."
    )
    created_at: Optional[datetime] = Field(
        default=None,
        description="Timestamp when subscription was first recorded."
    )
    updated_at: Optional[datetime] = Field(
        default=None,
        description="Timestamp of last update."
    )


class SubscriptionCreateRequest(BaseModel):
    """Payload for user manual creation of a subscription in inventory."""
    model_config = ConfigDict(extra="forbid")

    service_name: str = Field(..., min_length=1, max_length=100)
    monthly_cost: Optional[float] = Field(default=None, ge=0.0)
    currency: Optional[str] = Field(default="USD", max_length=5)
    billing_cycle: Optional[str] = Field(default="monthly", max_length=20)
    renewal_date: Optional[date] = None
    last_used_at: Optional[datetime] = None
    category: Optional[str] = Field(default=None, max_length=50)
    status: Optional[SubscriptionStatus] = SubscriptionStatus.ACTIVE
    notes: Optional[str] = Field(default=None, max_length=1000)
    cancellation_terms_url: Optional[str] = None
    manual_action_url: Optional[str] = None
    manual_action_instructions: Optional[str] = None

    @field_validator("service_name")
    @classmethod
    def validate_service_name(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Service name must not be blank.")
        return v.strip()


class SubscriptionUpdateRequest(BaseModel):
    """Payload for user updating permitted subscription details."""
    model_config = ConfigDict(extra="forbid")

    service_name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    monthly_cost: Optional[float] = Field(default=None, ge=0.0)
    currency: Optional[str] = Field(default=None, max_length=5)
    billing_cycle: Optional[str] = Field(default=None, max_length=20)
    renewal_date: Optional[date] = None
    last_used_at: Optional[datetime] = None
    status: Optional[SubscriptionStatus] = None
    category: Optional[str] = Field(default=None, max_length=50)
    notes: Optional[str] = Field(default=None, max_length=1000)
    is_archived: Optional[bool] = None
    manual_action_url: Optional[str] = None
    manual_action_instructions: Optional[str] = None


class SubscriptionCandidate(BaseModel):
    """Discovered candidate subscription extracted from billing emails."""
    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    user_id: str = "user_default"
    merchant_name: str
    product_name: Optional[str] = None
    estimated_cost: Optional[float] = None
    currency: Optional[str] = "USD"
    billing_interval: Optional[str] = "monthly"
    detected_renewal_date: Optional[date] = None
    detected_last_used: Optional[datetime] = None
    review_status: CandidateReviewStatus = CandidateReviewStatus.PENDING
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    source_message_id: Optional[str] = None
    source_subject: Optional[str] = None
    source_sender: Optional[str] = None
    source_date: Optional[datetime] = None
    email_snippet: Optional[str] = None
    cancellation_signal: bool = False
    notes: Optional[str] = None
    created_at: Optional[datetime] = None


class EmailAccountStatus(BaseModel):
    """Status model for connected email provider account."""
    model_config = ConfigDict(extra="forbid")

    user_id: str = "user_default"
    email_address: Optional[str] = None
    is_connected: bool = False
    connected: bool = False
    provider: str = "gmail"
    scopes: List[str] = Field(default_factory=list)
    last_synced_at: Optional[datetime] = None
    sync_status: str = "idle"
    candidate_count: int = 0
    error_message: Optional[str] = None
    is_live_configured: bool = False
    mode: str = "demo"

    def model_post_init(self, __context: Any) -> None:
        if self.is_connected and not self.connected:
            object.__setattr__(self, "connected", True)
        elif self.connected and not self.is_connected:
            object.__setattr__(self, "is_connected", True)



class Evidence(BaseModel):
    """Evidence artifact supporting eligibility decisions.

    Collected by Chinmay's connector layer to substantiate inactivity,
    cancellation policy terms, or provider-specific consequences.
    """
    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(
        ...,
        description="Unique identifier for this piece of evidence."
    )
    subscription_id: str = Field(
        ...,
        description="Subscription ID this evidence pertains to."
    )
    evidence_type: str = Field(
        ...,
        description="Category of evidence (e.g. 'usage_history', 'cancellation_terms', 'pause_policy')."
    )
    excerpt: str = Field(
        ...,
        description="Extracted textual excerpt, log snippet, or policy clause."
    )
    source_url: Optional[str] = Field(
        default=None,
        description="URL where the evidence was extracted."
    )
    captured_at: datetime = Field(
        ...,
        description="Timezone-aware timestamp when this evidence was collected."
    )
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Confidence score between 0.0 (unreliable) and 1.0 (authoritative)."
    )
    raw_reference: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional raw payload or provenance metadata from connector."
    )

    @field_validator("captured_at")
    @classmethod
    def validate_captured_at_timezone(cls, v: datetime) -> datetime:
        """Enforce timezone awareness on captured event timestamps."""
        if v.tzinfo is None or v.tzinfo.utcoffset(v) is None:
            raise ValueError("captured_at timestamp must be timezone-aware (include UTC offset).")
        return v


class Decision(BaseModel):
    """Evaluation decision produced by rule engine or agent reasoning.

    Determines if a subscription meets cancellation criteria (e.g., unused >= 90 days).
    """
    model_config = ConfigDict(extra="forbid")

    decision_id: str = Field(
        ...,
        description="Unique identifier for the decision record."
    )
    subscription_id: str = Field(
        ...,
        description="Subscription ID evaluated."
    )
    eligibility: EligibilityStatus = Field(
        ...,
        description="Evaluation result: candidate, not_eligible, or needs_review."
    )
    reason: str = Field(
        ...,
        description="Human-readable explanation of the decision rationale."
    )
    uncertainty: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="Uncertainty measure from 0.0 (certain) to 1.0 (completely uncertain)."
    )
    rule_version: str = Field(
        ...,
        description="Version of the decision rule set used for evaluation."
    )
    evaluated_at: datetime = Field(
        ...,
        description="Timestamp when decision was computed."
    )
    data_deletion_risk: bool = Field(
        default=False,
        description="True if cancellation triggers irreversible data deletion."
    )
    pause_alternative_available: bool = Field(
        default=False,
        description="True if the subscription supports pausing instead of cancelling."
    )
    consequences: List[str] = Field(
        default_factory=list,
        description="List of consequence explanations presented to user before approval."
    )


class CancellationPlan(BaseModel):
    """Actionable plan generated for an eligible subscription.

    Requires explicit approval before any execution can take place.
    Bound to an exact service name and plan version.
    """
    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(
        ...,
        description="Unique identifier for the cancellation plan."
    )
    subscription_id: str = Field(
        ...,
        description="Subscription targeted by this plan."
    )
    service_name: str = Field(
        ...,
        description="Target streaming service name."
    )
    plan_version: str = Field(
        ...,
        description="Version string of this plan specification (e.g. 'v1.0')."
    )
    steps: List[str] = Field(
        default_factory=list,
        description="Sequential human-readable steps planned for execution."
    )
    consequences_warning: Optional[str] = Field(
        default=None,
        description="Explicit warning message regarding data deletion or loss of benefits."
    )
    pause_alternative_available: bool = Field(
        default=False,
        description="Indicates if a pause option exists as an alternative to cancellation."
    )
    status: ApprovalStatus = Field(
        default=ApprovalStatus.AWAITING_APPROVAL,
        description="Approval state of this plan."
    )
    created_at: datetime = Field(
        ...,
        description="Timestamp when plan was generated."
    )


class Approval(BaseModel):
    """Explicit human approval record authorizing cancellation execution.

    Safety constraint: Must validate against the exact plan_id, service_name,
    and plan_version. Generic or multi-service approvals are strictly disallowed.
    """
    model_config = ConfigDict(extra="forbid")

    approval_id: str = Field(
        ...,
        description="Unique identifier for this approval event."
    )
    plan_id: str = Field(
        ...,
        description="Target plan ID approved or declined."
    )
    subscription_id: str = Field(
        ...,
        description="Subscription ID."
    )
    service_name: str = Field(
        ...,
        description="Service name explicitly verified in the approval."
    )
    plan_version: str = Field(
        ...,
        description="Exact plan version authorized."
    )
    status: ApprovalStatus = Field(
        ...,
        description="Resulting approval status (approved, declined, invalidated)."
    )
    approved_by: Optional[str] = Field(
        default=None,
        description="User identity or identifier who submitted the approval."
    )
    decided_at: Optional[datetime] = Field(
        default=None,
        description="Timestamp when approval or decline decision was registered."
    )
    notes: Optional[str] = Field(
        default=None,
        description="Optional user notes or decline reason."
    )


class ActionResult(BaseModel):
    """Outcome of an attempted cancellation action.

    CRITICAL: An action status of 'succeeded' only means execution completed
    without errors; it DOES NOT imply verified cancellation.
    """
    model_config = ConfigDict(extra="forbid")

    action_id: str = Field(
        ...,
        description="Unique action execution identifier."
    )
    plan_id: str = Field(
        ...,
        description="Plan ID that authorized this execution."
    )
    subscription_id: str = Field(
        ...,
        description="Subscription operated on."
    )
    status: ActionStatus = Field(
        ...,
        description="Status of the action execution."
    )
    started_at: Optional[datetime] = Field(
        default=None,
        description="Execution start timestamp."
    )
    completed_at: Optional[datetime] = Field(
        default=None,
        description="Execution completion timestamp."
    )
    message: Optional[str] = Field(
        default=None,
        description="Summary of execution outcome or error message."
    )
    action_type: str = Field(
        default="cancellation",
        description="Type of action performed (e.g. 'cancellation', 'pause')."
    )
    logs: List[str] = Field(
        default_factory=list,
        description="Chronological log entries captured during execution."
    )


class VerificationResult(BaseModel):
    """Independent verification check on post-action subscription status."""
    model_config = ConfigDict(extra="forbid")

    verification_id: str = Field(
        ...,
        description="Unique identifier for this verification check."
    )
    action_id: str = Field(
        ...,
        description="Action ID verified."
    )
    subscription_id: str = Field(
        ...,
        description="Subscription verified."
    )
    status: VerificationStatus = Field(
        ...,
        description="Verification outcome (verified, failed, pending)."
    )
    method: str = Field(
        ...,
        description="Independent verification method (e.g. 'renewal_status_check', 'mock_site_api')."
    )
    verified_at: Optional[datetime] = Field(
        default=None,
        description="Timestamp when verification was performed."
    )
    final_state_observed: Optional[str] = Field(
        default=None,
        description="State observed during independent check (e.g. 'cancelled', 'paused', 'active')."
    )
    details: Optional[str] = Field(
        default=None,
        description="Detailed verification observations or error message."
    )


class AgentRun(BaseModel):
    """Overall agent orchestration session record."""
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(
        ...,
        description="Unique identifier for the agent execution run."
    )
    started_at: datetime = Field(
        ...,
        description="Run start timestamp."
    )
    completed_at: Optional[datetime] = Field(
        default=None,
        description="Run completion timestamp."
    )
    status: str = Field(
        default="pending",
        description="Run status: 'pending', 'in_progress', 'completed', 'failed'."
    )
    subscription_ids: List[str] = Field(
        default_factory=list,
        description="List of subscription IDs tracked in this run."
    )
    final_results: Dict[str, FinalResultStatus] = Field(
        default_factory=dict,
        description="Map of subscription_id -> FinalResultStatus."
    )
    summary: Optional[str] = Field(
        default=None,
        description="High-level narrative summary of run outcomes."
    )


class HealthCheckResponse(BaseModel):
    """Structured response model for GET /health endpoint."""
    model_config = ConfigDict(extra="forbid")

    status: str = Field(default="healthy", description="Application operational health status.")
    app_title: str = Field(..., description="Application name.")
    version: str = Field(..., description="Application semantic version.")
    phase: str = Field(default="Phase 0: Foundation & Team Independence", description="Project development phase.")
    timestamp: datetime = Field(..., description="Timestamp of health check response.")
