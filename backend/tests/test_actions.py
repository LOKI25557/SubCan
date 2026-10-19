"""Unit tests for Phase 4 Safe Action Coordination & Idempotency (backend/app/actions.py).

Verifies:
1. Valid approved cancellation action executes successfully.
2. Missing approval is rejected.
3. Declined approval is rejected.
4. Wrong plan ID is rejected.
5. Wrong subscription ID is rejected.
6. Wrong service name is rejected.
7. Wrong plan version is rejected.
8. Stale approval is rejected.
9. Pause/cancellation mismatch is rejected.
10. Repeating the same idempotency key and same request does not create a duplicate action.
11. Reusing a key for a different request is rejected with IdempotencyConflictError.
12. Different idempotency keys produce distinct actions.
13. Failed execution does not falsely report successful cancellation.
14. Action success does not establish verified cancellation (independent verification required).
15. Invalid requests do not create actions or trigger execution.
16. Purity of authorization: authorize_action does not execute.
"""

from datetime import date, datetime, timedelta, timezone
import pytest

from backend.app.actions import (
    ActionRegistry,
    ActionTypeMismatchError,
    IdempotencyConflictError,
    authorize_action,
    clear_action_registry,
    execute_plan,
    get_action_registry,
)
from backend.app.approvals import (
    ApprovalValidationError,
    create_approval,
)
from backend.app.models import (
    ActionResult,
    ActionStatus,
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
# Test Fixtures & Setup
# ==============================================================================

@pytest.fixture(autouse=True)
def clean_registry():
    """Ensure a clean action registry for each test."""
    clear_action_registry()
    yield
    clear_action_registry()


@pytest.fixture
def base_time() -> datetime:
    return datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def sample_plan(base_time: datetime) -> CancellationPlan:
    return CancellationPlan(
        plan_id="plan_sub_tc1_cinevault_v1_0",
        subscription_id="sub_tc1_cinevault",
        service_name="CineVault Mock Streaming",
        plan_version="v1.0.abcd1234",
        steps=[
            "1. Proposed Action: Cancellation of streaming subscription for 'CineVault Mock Streaming'.",
            "2. Eligibility Rationale: Inactive for > 3 calendar months.",
            "3. Terms & Renewal: Access active until 2026-11-15.",
            "4. User Approval Gate: Require explicit approval.",
        ],
        consequences_warning=None,
        pause_alternative_available=False,
        status=ApprovalStatus.AWAITING_APPROVAL,
        created_at=base_time,
    )


@pytest.fixture
def valid_approval(sample_plan: CancellationPlan, base_time: datetime) -> Approval:
    return Approval(
        approval_id="appr_001",
        plan_id=sample_plan.plan_id,
        subscription_id=sample_plan.subscription_id,
        service_name=sample_plan.service_name,
        plan_version=sample_plan.plan_version,
        status=ApprovalStatus.APPROVED,
        approved_by="user_loki",
        decided_at=base_time + timedelta(minutes=5),
        notes="Confirmed cancellation.",
    )


@pytest.fixture
def candidate_decision(sample_plan: CancellationPlan, base_time: datetime) -> Decision:
    return Decision(
        decision_id="dec_001",
        subscription_id=sample_plan.subscription_id,
        eligibility=EligibilityStatus.CANDIDATE,
        reason="Inactive for > 3 months.",
        uncertainty=0.05,
        rule_version="1.0.0",
        evaluated_at=base_time,
    )


# ==============================================================================
# Action Authorization & Execution Tests
# ==============================================================================

def test_valid_approved_cancellation_action(
    sample_plan: CancellationPlan,
    valid_approval: Approval,
    candidate_decision: Decision,
    base_time: datetime,
):
    """1. Verify valid approved plan executes and returns SUCCEEDED ActionResult."""
    as_of = base_time + timedelta(minutes=10)
    result = execute_plan(
        plan=sample_plan,
        approval=valid_approval,
        decision=candidate_decision,
        as_of=as_of,
    )

    assert result.status == ActionStatus.SUCCEEDED
    assert result.subscription_id == sample_plan.subscription_id
    assert result.plan_id == sample_plan.plan_id
    assert result.action_type == "cancellation"
    assert result.completed_at is not None
    assert any("AWAITING INDEPENDENT VERIFICATION" in log for log in result.logs)


def test_missing_approval_is_rejected(
    sample_plan: CancellationPlan,
    base_time: datetime,
):
    """2. Missing approval (None) must be rejected without creating an action."""
    with pytest.raises(ApprovalValidationError, match="Missing approval"):
        execute_plan(
            plan=sample_plan,
            approval=None,
            as_of=base_time + timedelta(minutes=10),
        )

    # Registry must remain empty
    assert len(get_action_registry()._actions_by_id) == 0


def test_declined_approval_is_rejected(
    sample_plan: CancellationPlan,
    valid_approval: Approval,
    base_time: datetime,
):
    """3. Declined approval must be rejected."""
    declined_approval = valid_approval.model_copy(update={"status": ApprovalStatus.DECLINED})
    with pytest.raises(ApprovalValidationError, match="explicitly declined"):
        execute_plan(
            plan=sample_plan,
            approval=declined_approval,
            as_of=base_time + timedelta(minutes=10),
        )


def test_wrong_plan_id_is_rejected(
    sample_plan: CancellationPlan,
    valid_approval: Approval,
    base_time: datetime,
):
    """4. Approval with wrong plan_id is rejected."""
    mismatched_approval = valid_approval.model_copy(update={"plan_id": "plan_other_001"})
    with pytest.raises(ApprovalValidationError, match="Plan ID mismatch"):
        execute_plan(
            plan=sample_plan,
            approval=mismatched_approval,
            as_of=base_time + timedelta(minutes=10),
        )


def test_wrong_subscription_id_is_rejected(
    sample_plan: CancellationPlan,
    valid_approval: Approval,
    base_time: datetime,
):
    """5. Approval with wrong subscription_id is rejected."""
    mismatched_approval = valid_approval.model_copy(update={"subscription_id": "sub_different"})
    with pytest.raises(ApprovalValidationError, match="Subscription ID mismatch"):
        execute_plan(
            plan=sample_plan,
            approval=mismatched_approval,
            as_of=base_time + timedelta(minutes=10),
        )


def test_wrong_service_name_is_rejected(
    sample_plan: CancellationPlan,
    valid_approval: Approval,
    base_time: datetime,
):
    """6. Approval with wrong service_name is rejected."""
    mismatched_approval = valid_approval.model_copy(update={"service_name": "DifferentStream"})
    with pytest.raises(ApprovalValidationError, match="Service name mismatch"):
        execute_plan(
            plan=sample_plan,
            approval=mismatched_approval,
            as_of=base_time + timedelta(minutes=10),
        )


def test_wrong_plan_version_is_rejected(
    sample_plan: CancellationPlan,
    valid_approval: Approval,
    base_time: datetime,
):
    """7. Approval with mismatched plan_version is rejected."""
    mismatched_approval = valid_approval.model_copy(update={"plan_version": "v2.0.changed"})
    with pytest.raises(ApprovalValidationError, match="Plan version mismatch"):
        execute_plan(
            plan=sample_plan,
            approval=mismatched_approval,
            as_of=base_time + timedelta(minutes=10),
        )


def test_stale_approval_from_plan_mutation_is_rejected(
    base_time: datetime,
):
    """8. Stale approval granted for an older version is rejected when plan materially changes."""
    sub = Subscription(
        subscription_id="sub_stale_test",
        service_name="StaleStream",
        status=SubscriptionStatus.ACTIVE,
        renewal_date=date(2026, 11, 15),
        last_used_at=datetime(2025, 5, 1, 12, 0, 0, tzinfo=timezone.utc),
        cancellation_terms_url="https://mock.streaming.test/terms",
    )
    decision = Decision(
        decision_id="dec_stale",
        subscription_id=sub.subscription_id,
        eligibility=EligibilityStatus.CANDIDATE,
        reason="Inactive.",
        uncertainty=0.05,
        rule_version="1.0.0",
        evaluated_at=base_time,
    )

    plan_v1 = create_cancellation_plan(sub, decision, created_at=base_time)
    approval_v1 = create_approval(plan_v1, approved=True, decided_at=base_time + timedelta(minutes=2))

    # Subscription is modified (data loss warning added)
    sub_modified = sub.model_copy(update={"data_deletion_warning": True})
    decision_modified = decision.model_copy(update={"data_deletion_risk": True})
    plan_v2 = create_cancellation_plan(sub_modified, decision_modified, created_at=base_time + timedelta(minutes=5))

    # Executing plan_v2 with approval_v1 must fail
    with pytest.raises(ApprovalValidationError, match="Plan version mismatch"):
        execute_plan(
            plan=plan_v2,
            approval=approval_v1,
            as_of=base_time + timedelta(minutes=10),
        )


def test_pause_cancellation_mismatch_is_rejected(
    sample_plan: CancellationPlan,
    valid_approval: Approval,
    base_time: datetime,
):
    """9. Requesting 'pause' action against a cancellation plan is strictly rejected."""
    with pytest.raises(ActionTypeMismatchError, match="Action type mismatch"):
        execute_plan(
            plan=sample_plan,
            approval=valid_approval,
            action_type="pause",
            as_of=base_time + timedelta(minutes=10),
        )


# ==============================================================================
# Idempotency Tests
# ==============================================================================

def test_repeating_same_idempotency_key_returns_existing_action(
    sample_plan: CancellationPlan,
    valid_approval: Approval,
    base_time: datetime,
):
    """10. Repeated calls with the exact same idempotency key return the cached action without duplicating."""
    idempotency_key = "idemp_sub_tc1_run_01"
    as_of = base_time + timedelta(minutes=10)

    # First call
    first_result = execute_plan(
        plan=sample_plan,
        approval=valid_approval,
        idempotency_key=idempotency_key,
        as_of=as_of,
    )

    # Second call (same key, same request)
    second_result = execute_plan(
        plan=sample_plan,
        approval=valid_approval,
        idempotency_key=idempotency_key,
        as_of=as_of + timedelta(seconds=30),
    )

    assert first_result.action_id == second_result.action_id
    assert first_result.status == second_result.status
    assert first_result.completed_at == second_result.completed_at
    # Ensure only 1 action exists in registry
    assert len(get_action_registry()._actions_by_id) == 1


def test_reusing_idempotency_key_for_different_request_is_rejected(
    sample_plan: CancellationPlan,
    valid_approval: Approval,
    base_time: datetime,
):
    """11. Reusing an idempotency key for a different subscription or plan raises IdempotencyConflictError."""
    idempotency_key = "shared_idemp_key_001"
    as_of = base_time + timedelta(minutes=10)

    # First execution for sample_plan
    execute_plan(
        plan=sample_plan,
        approval=valid_approval,
        idempotency_key=idempotency_key,
        as_of=as_of,
    )

    # Different plan (different subscription)
    different_plan = sample_plan.model_copy(
        update={
            "plan_id": "plan_sub_different",
            "subscription_id": "sub_different",
            "service_name": "Different Stream",
        }
    )
    different_approval = create_approval(
        plan=different_plan,
        approved=True,
        decided_at=as_of + timedelta(minutes=1),
    )

    # Attempt to reuse the same key for different_plan
    with pytest.raises(IdempotencyConflictError, match="already used for a different request"):
        execute_plan(
            plan=different_plan,
            approval=different_approval,
            idempotency_key=idempotency_key,
            as_of=as_of + timedelta(minutes=2),
        )


def test_different_idempotency_keys_produce_distinct_actions(
    sample_plan: CancellationPlan,
    valid_approval: Approval,
    base_time: datetime,
):
    """12. Different idempotency keys produce separate distinct actions."""
    as_of = base_time + timedelta(minutes=10)

    res_1 = execute_plan(
        plan=sample_plan,
        approval=valid_approval,
        idempotency_key="key_attempt_01",
        as_of=as_of,
    )

    res_2 = execute_plan(
        plan=sample_plan,
        approval=valid_approval,
        idempotency_key="key_attempt_02",
        as_of=as_of + timedelta(seconds=1),
    )

    assert res_1.action_id != res_2.action_id
    assert len(get_action_registry()._actions_by_id) == 2


# ==============================================================================
# Failure & Verification Invariant Tests
# ==============================================================================

def test_failed_execution_does_not_report_success(
    sample_plan: CancellationPlan,
    valid_approval: Approval,
    base_time: datetime,
):
    """13. Simulated execution failure records FAILED status without blind retry."""
    as_of = base_time + timedelta(minutes=10)
    result = execute_plan(
        plan=sample_plan,
        approval=valid_approval,
        simulate_failure="Mock provider network timeout",
        as_of=as_of,
    )

    assert result.status == ActionStatus.FAILED
    assert "Mock provider network timeout" in result.message
    assert any("EXECUTION FAILURE" in log for log in result.logs)


def test_action_success_does_not_equal_verified_cancellation(
    sample_plan: CancellationPlan,
    valid_approval: Approval,
    base_time: datetime,
):
    """14. ActionStatus.SUCCEEDED does not equal verified cancellation (independent check required)."""
    as_of = base_time + timedelta(minutes=10)
    result = execute_plan(
        plan=sample_plan,
        approval=valid_approval,
        as_of=as_of,
    )

    assert result.status == ActionStatus.SUCCEEDED
    # ActionResult does not contain verification fields; independent verification is mandatory
    assert not hasattr(result, "verified")
    assert not hasattr(result, "final_state_observed")
    assert any("AWAITING INDEPENDENT VERIFICATION" in log for log in result.logs)


def test_invalid_requests_do_not_create_actions_or_mutate_state(
    sample_plan: CancellationPlan,
    base_time: datetime,
):
    """15. Invalid requests must fail before action creation or execution."""
    reg = ActionRegistry()
    with pytest.raises(ApprovalValidationError):
        execute_plan(
            plan=sample_plan,
            approval=None,
            registry=reg,
            as_of=base_time + timedelta(minutes=10),
        )

    # Registry has 0 actions recorded
    assert len(reg._actions_by_id) == 0


def test_authorize_action_is_pure_and_does_not_execute(
    sample_plan: CancellationPlan,
    valid_approval: Approval,
    base_time: datetime,
):
    """16. Authorizing an action returns PENDING and does not execute."""
    as_of = base_time + timedelta(minutes=10)
    pending_action = authorize_action(
        plan=sample_plan,
        approval=valid_approval,
        as_of=as_of,
    )

    assert pending_action.status == ActionStatus.PENDING
    assert pending_action.completed_at is None
    assert "PENDING execution" in pending_action.logs[-1]
