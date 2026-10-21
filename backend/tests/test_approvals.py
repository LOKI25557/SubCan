"""Unit tests for Phase 3 Approval Validation (backend/app/approvals.py).

Verifies:
1. Valid approval for the exact current plan.
2. Missing approval (None) fails closed.
3. Explicitly declined approval fails.
4. Wrong subscription ID fails.
5. Wrong plan ID fails.
6. Wrong plan version fails.
7. Wrong approved action fails.
8. Invalid or missing approval timestamp fails.
9. Plan not awaiting approval fails.
10. Ineligible or review-required decision fails.
11. Approval for an outdated plan version fails.
12. Malformed approval data fails.
13. Repeated approval validation is idempotent without side effects.
"""

from datetime import date, datetime, timedelta, timezone
import pytest

from backend.app.approvals import (
    ApprovalValidationError,
    create_approval,
    enforce_approval,
    get_approval_rejection_reason,
    validate_approval,
)
from backend.app.models import (
    Approval,
    ApprovalStatus,
    CancellationPlan,
    Decision,
    EligibilityStatus,
    Subscription,
    SubscriptionStatus,
)
from backend.app.planner import create_cancellation_plan


# ==============================================================================
# Test Fixtures & Helpers
# ==============================================================================

@pytest.fixture
def sample_plan() -> CancellationPlan:
    """Fixture providing a standard valid plan in AWAITING_APPROVAL status."""
    return CancellationPlan(
        plan_id="plan_sub_tc1_cinevault_v1_0",
        subscription_id="sub_tc1_cinevault",
        service_name="CineVault Mock Streaming",
        plan_version="v1.0.abcdef12",
        steps=[
            "1. Proposed Action: Cancellation of streaming subscription for 'CineVault Mock Streaming'.",
            "2. Eligibility Rationale: Inactive for > 3 calendar months.",
            "3. Terms & Renewal: Access active until 2026-11-15.",
            "4. User Approval Gate: Require explicit approval.",
        ],
        consequences_warning=None,
        pause_alternative_available=False,
        status=ApprovalStatus.AWAITING_APPROVAL,
        created_at=datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
    )


@pytest.fixture
def valid_approval(sample_plan: CancellationPlan) -> Approval:
    """Fixture providing a matching valid approval for sample_plan."""
    return Approval(
        approval_id="appr_001",
        plan_id=sample_plan.plan_id,
        subscription_id=sample_plan.subscription_id,
        service_name=sample_plan.service_name,
        plan_version=sample_plan.plan_version,
        status=ApprovalStatus.APPROVED,
        approved_by="user_loki",
        decided_at=sample_plan.created_at + timedelta(minutes=5),
        notes="Confirmed cancellation.",
    )


@pytest.fixture
def candidate_decision() -> Decision:
    """Fixture providing a valid candidate decision."""
    return Decision(
        decision_id="dec_001",
        subscription_id="sub_tc1_cinevault",
        eligibility=EligibilityStatus.CANDIDATE,
        reason="Inactive for 130 days.",
        uncertainty=0.05,
        rule_version="1.0.0",
        evaluated_at=datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
    )


# ==============================================================================
# Approval Validation Tests
# ==============================================================================

def test_valid_approval_for_exact_current_plan(
    sample_plan: CancellationPlan,
    valid_approval: Approval,
    candidate_decision: Decision,
):
    """1. Verify valid approval matches exact plan, service, and version."""
    assert validate_approval(sample_plan, valid_approval, decision=candidate_decision) is True
    enforce_approval(sample_plan, valid_approval, decision=candidate_decision)


def test_missing_approval_fails_closed(sample_plan: CancellationPlan):
    """2. Missing approval (None) must fail closed and never authorize execution."""
    assert validate_approval(sample_plan, None) is False
    with pytest.raises(ApprovalValidationError, match="Missing approval"):
        enforce_approval(sample_plan, None)


def test_explicitly_declined_approval_rejected(
    sample_plan: CancellationPlan,
    valid_approval: Approval,
):
    """3. Explicitly declined approval is rejected."""
    valid_approval.status = ApprovalStatus.DECLINED
    assert validate_approval(sample_plan, valid_approval) is False
    with pytest.raises(ApprovalValidationError, match="explicitly declined"):
        enforce_approval(sample_plan, valid_approval)


def test_wrong_subscription_id_rejected(
    sample_plan: CancellationPlan,
    valid_approval: Approval,
):
    """4. Approval with mismatched subscription ID is rejected."""
    wrong_sub_approval = valid_approval.model_copy(update={"subscription_id": "sub_different"})
    assert validate_approval(sample_plan, wrong_sub_approval) is False
    reason = get_approval_rejection_reason(sample_plan, wrong_sub_approval)
    assert "Subscription ID mismatch" in reason


def test_wrong_plan_id_rejected(
    sample_plan: CancellationPlan,
    valid_approval: Approval,
):
    """5. Approval with mismatched plan ID is rejected."""
    wrong_plan_approval = valid_approval.model_copy(update={"plan_id": "plan_different"})
    assert validate_approval(sample_plan, wrong_plan_approval) is False
    reason = get_approval_rejection_reason(sample_plan, wrong_plan_approval)
    assert "Plan ID mismatch" in reason


def test_wrong_service_name_rejected(
    sample_plan: CancellationPlan,
    valid_approval: Approval,
):
    """Approval with mismatched service name is rejected."""
    wrong_srv_approval = valid_approval.model_copy(update={"service_name": "Different Streaming"})
    assert validate_approval(sample_plan, wrong_srv_approval) is False
    reason = get_approval_rejection_reason(sample_plan, wrong_srv_approval)
    assert "Service name mismatch" in reason


def test_wrong_plan_version_rejected(
    sample_plan: CancellationPlan,
    valid_approval: Approval,
):
    """6. Approval with mismatched plan version is rejected."""
    wrong_version_approval = valid_approval.model_copy(update={"plan_version": "v2.0.modified"})
    assert validate_approval(sample_plan, wrong_version_approval) is False
    reason = get_approval_rejection_reason(sample_plan, wrong_version_approval)
    assert "Plan version mismatch" in reason


def test_wrong_approved_action_rejected(
    sample_plan: CancellationPlan,
    valid_approval: Approval,
):
    """7. Approval specifying conflicting action (e.g. pause) is rejected."""
    conflicting_action_approval = valid_approval.model_copy(
        update={"notes": "User selected action:pause instead"}
    )
    assert validate_approval(
        sample_plan,
        conflicting_action_approval,
        expected_action="cancellation",
    ) is False
    reason = get_approval_rejection_reason(
        sample_plan,
        conflicting_action_approval,
        expected_action="cancellation",
    )
    assert "Approved action mismatch" in reason


def test_missing_approval_timestamp_rejected(
    sample_plan: CancellationPlan,
    valid_approval: Approval,
):
    """8a. Approval without decided_at timestamp is rejected."""
    no_ts_approval = valid_approval.model_copy(update={"decided_at": None})
    assert validate_approval(sample_plan, no_ts_approval) is False
    reason = get_approval_rejection_reason(sample_plan, no_ts_approval)
    assert "Missing approval timestamp" in reason


def test_future_dated_approval_timestamp_rejected(
    sample_plan: CancellationPlan,
    valid_approval: Approval,
):
    """8b. Approval timestamp in the future relative to as_of is rejected."""
    as_of = datetime(2026, 1, 1, 10, 10, 0, tzinfo=timezone.utc)
    future_approval = valid_approval.model_copy(
        update={"decided_at": as_of + timedelta(days=1)}
    )
    assert validate_approval(sample_plan, future_approval, as_of=as_of) is False
    reason = get_approval_rejection_reason(sample_plan, future_approval, as_of=as_of)
    assert "Future-dated approval timestamp" in reason


def test_pre_plan_approval_timestamp_rejected(
    sample_plan: CancellationPlan,
    valid_approval: Approval,
):
    """8c. Approval timestamp earlier than plan creation is rejected as stale/impossible."""
    stale_ts_approval = valid_approval.model_copy(
        update={"decided_at": sample_plan.created_at - timedelta(hours=1)}
    )
    assert validate_approval(sample_plan, stale_ts_approval) is False
    reason = get_approval_rejection_reason(sample_plan, stale_ts_approval)
    assert "Stale approval timestamp" in reason


def test_plan_not_awaiting_approval_rejected(
    sample_plan: CancellationPlan,
    valid_approval: Approval,
):
    """9. Plans not in AWAITING_APPROVAL status cannot receive new approval."""
    sample_plan.status = ApprovalStatus.APPROVED
    assert validate_approval(sample_plan, valid_approval) is False
    reason = get_approval_rejection_reason(sample_plan, valid_approval)
    assert "Plan is not awaiting approval" in reason


def test_ineligible_or_review_required_decision_rejected(
    sample_plan: CancellationPlan,
    valid_approval: Approval,
):
    """10. Plans whose underlying decision is not CANDIDATE cannot be authorized."""
    ineligible_dec = Decision(
        decision_id="dec_ineligible",
        subscription_id=sample_plan.subscription_id,
        eligibility=EligibilityStatus.NOT_ELIGIBLE,
        reason="Active usage observed.",
        uncertainty=0.0,
        rule_version="1.0.0",
        evaluated_at=datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
    )
    assert validate_approval(sample_plan, valid_approval, decision=ineligible_dec) is False
    with pytest.raises(ApprovalValidationError, match="subscription eligibility is 'not_eligible'"):
        enforce_approval(sample_plan, valid_approval, decision=ineligible_dec)

    review_dec = Decision(
        decision_id="dec_review",
        subscription_id=sample_plan.subscription_id,
        eligibility=EligibilityStatus.NEEDS_REVIEW,
        reason="Missing usage history.",
        uncertainty=1.0,
        rule_version="1.0.0",
        evaluated_at=datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
    )
    assert validate_approval(sample_plan, valid_approval, decision=review_dec) is False


def test_approval_for_outdated_plan_version_rejected():
    """11. Material plan changes produce a new version that invalidates previous approvals."""
    sub = Subscription(
        subscription_id="sub_tc1_cinevault",
        service_name="CineVault Mock Streaming",
        status=SubscriptionStatus.ACTIVE,
        renewal_date=date(2026, 11, 15),
        last_used_at=datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc),
        cancellation_terms_url="https://mock.streaming.test/terms",
    )
    decision = Decision(
        decision_id="dec_tc1",
        subscription_id=sub.subscription_id,
        eligibility=EligibilityStatus.CANDIDATE,
        reason="Inactive > 3 months.",
        uncertainty=0.05,
        rule_version="1.0.0",
        evaluated_at=datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
    )

    # Initial plan v1
    plan_v1 = create_cancellation_plan(
        sub,
        decision,
        created_at=datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
    )
    approval_v1 = create_approval(
        plan_v1,
        approved=True,
        approved_by="user_loki",
        decided_at=datetime(2026, 1, 1, 10, 5, 0, tzinfo=timezone.utc),
    )

    # Initial plan and approval match
    assert validate_approval(plan_v1, approval_v1) is True

    # Material change occurs: provider discovers immediate data loss risk
    sub_modified = sub.model_copy(update={"data_deletion_warning": True})
    decision_modified = decision.model_copy(update={"data_deletion_risk": True})

    # Regenerate plan v2
    plan_v2 = create_cancellation_plan(
        sub_modified,
        decision_modified,
        created_at=datetime(2026, 1, 1, 10, 10, 0, tzinfo=timezone.utc),
    )
    assert plan_v2.plan_version != plan_v1.plan_version

    # The OLD approval cannot authorize the NEW plan
    assert validate_approval(plan_v2, approval_v1) is False
    reason = get_approval_rejection_reason(plan_v2, approval_v1)
    assert "Plan version mismatch" in reason


def test_malformed_approval_data_rejected(sample_plan: CancellationPlan):
    """12. Malformed non-Approval objects fail closed."""
    assert validate_approval(sample_plan, "approved") is False  # type: ignore[arg-type]
    assert validate_approval(sample_plan, {"status": "approved"}) is False  # type: ignore[arg-type]
    with pytest.raises(ApprovalValidationError, match="Malformed approval"):
        enforce_approval(sample_plan, 12345)  # type: ignore[arg-type]


def test_repeated_approval_validation_idempotency(
    sample_plan: CancellationPlan,
    valid_approval: Approval,
    candidate_decision: Decision,
):
    """13. Validation is idempotent and pure; repeated checks have zero side effects."""
    plan_before = sample_plan.model_dump()
    approval_before = valid_approval.model_dump()

    for _ in range(5):
        assert validate_approval(sample_plan, valid_approval, decision=candidate_decision) is True

    assert sample_plan.model_dump() == plan_before
    assert valid_approval.model_dump() == approval_before


def test_create_approval_helper(sample_plan: CancellationPlan):
    """Verify create_approval factory produces matching, validated Approval record."""
    approval = create_approval(
        plan=sample_plan,
        approved=True,
        approved_by="test_reviewer",
        decided_at=sample_plan.created_at + timedelta(minutes=2),
        notes="All clear",
    )
    assert approval.plan_id == sample_plan.plan_id
    assert approval.subscription_id == sample_plan.subscription_id
    assert approval.service_name == sample_plan.service_name
    assert approval.plan_version == sample_plan.plan_version
    assert approval.status == ApprovalStatus.APPROVED
    assert approval.approved_by == "test_reviewer"
    assert approval.decided_at is not None
    assert approval.notes == "All clear"
