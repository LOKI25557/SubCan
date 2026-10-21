"""Unit tests for Phase 5 Independent Cancellation Verification (backend/app/verification.py).

Verifies:
1. Valid cancellation with independently observed renewal disabled (VERIFIED).
2. Successful action with renewal still active fails verification (FAILED).
3. Missing verification evidence returns PENDING (never infers success from action).
4. Stale evidence captured before action completion fails verification (FAILED).
5. Contradictory evidence fails verification (FAILED).
6. Subscription ID mismatch raises VerificationMismatchError.
7. Service identity mismatch raises VerificationMismatchError.
8. Pause state is never reported as cancelled (FAILED).
9. Failed action is never reported as verified cancellation (FAILED).
10. Repeated verification with unchanged evidence is pure and deterministic.
11. Verification never executes, retries, or mutates any action.
"""

from datetime import date, datetime, timedelta, timezone
import pytest

from backend.app.models import (
    ActionResult,
    ActionStatus,
    Evidence,
    Subscription,
    SubscriptionStatus,
    VerificationResult,
    VerificationStatus,
)
from backend.app.verification import (
    VerificationMismatchError,
    verify_cancellation,
)


# ==============================================================================
# Test Fixtures
# ==============================================================================

@pytest.fixture
def base_time() -> datetime:
    return datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def successful_action(base_time: datetime) -> ActionResult:
    return ActionResult(
        action_id="act_tc1_001",
        plan_id="plan_sub_tc1_01",
        subscription_id="sub_tc1_cinevault",
        status=ActionStatus.SUCCEEDED,
        started_at=base_time,
        completed_at=base_time + timedelta(seconds=2),
        message="Cancellation dispatched to mock provider.",
        action_type="cancellation",
        logs=["Dispatched mock request.", "HTTP 200 acknowledged."],
    )


@pytest.fixture
def observed_cancelled_sub(successful_action: ActionResult, base_time: datetime) -> Subscription:
    return Subscription(
        subscription_id=successful_action.subscription_id,
        service_name="CineVault Mock Streaming",
        status=SubscriptionStatus.CANCELLED,
        renewal_date=None,
        last_used_at=base_time - timedelta(days=120),
    )


@pytest.fixture
def observed_active_sub(successful_action: ActionResult, base_time: datetime) -> Subscription:
    return Subscription(
        subscription_id=successful_action.subscription_id,
        service_name="CineVault Mock Streaming",
        status=SubscriptionStatus.ACTIVE,
        renewal_date=date(2026, 2, 1),
        last_used_at=base_time - timedelta(days=120),
    )


# ==============================================================================
# Independent Verification Tests
# ==============================================================================

def test_valid_cancellation_with_independently_observed_renewal_disabled(
    successful_action: ActionResult,
    observed_cancelled_sub: Subscription,
    base_time: datetime,
):
    """1. Verify valid cancellation where independent query shows renewal disabled / cancelled."""
    as_of = base_time + timedelta(minutes=5)
    result = verify_cancellation(
        action_result=successful_action,
        observed_subscription=observed_cancelled_sub,
        service_name="CineVault Mock Streaming",
        as_of=as_of,
    )

    assert result.status == VerificationStatus.VERIFIED
    assert result.final_state_observed == "cancelled"
    assert result.action_id == successful_action.action_id
    assert result.subscription_id == successful_action.subscription_id
    assert "confirmed: renewal is disabled" in result.details


def test_successful_action_with_renewal_still_enabled_fails_verification(
    successful_action: ActionResult,
    observed_active_sub: Subscription,
    base_time: datetime,
):
    """2. An action succeeded, but independent check shows subscription still ACTIVE -> FAILED."""
    as_of = base_time + timedelta(minutes=5)
    result = verify_cancellation(
        action_result=successful_action,
        observed_subscription=observed_active_sub,
        service_name="CineVault Mock Streaming",
        as_of=as_of,
    )

    assert result.status == VerificationStatus.FAILED
    assert result.final_state_observed == "active"
    assert "subscription is still ACTIVE" in result.details


def test_missing_verification_evidence_returns_pending(
    successful_action: ActionResult,
    base_time: datetime,
):
    """3. Missing verification data returns PENDING (never infers success from action alone)."""
    as_of = base_time + timedelta(minutes=5)
    result = verify_cancellation(
        action_result=successful_action,
        observed_subscription=None,
        evidence_list=None,
        as_of=as_of,
    )

    assert result.status == VerificationStatus.PENDING
    assert result.final_state_observed == "pending_propagation"
    assert "No independent renewal check data observed yet" in result.details


def test_stale_pre_action_evidence_fails_verification(
    successful_action: ActionResult,
    base_time: datetime,
):
    """4. Evidence captured before the action completed is stale and fails verification."""
    stale_time = successful_action.started_at - timedelta(hours=1)
    stale_ev = Evidence(
        evidence_id="ev_stale_001",
        subscription_id=successful_action.subscription_id,
        evidence_type="renewal_status",
        excerpt="Subscription status: cancelled.",
        captured_at=stale_time,
        confidence=0.9,
    )

    as_of = base_time + timedelta(minutes=5)
    result = verify_cancellation(
        action_result=successful_action,
        observed_subscription=None,
        evidence_list=[stale_ev],
        as_of=as_of,
    )

    assert result.status == VerificationStatus.FAILED
    assert result.final_state_observed == "stale_evidence"
    assert "captured before action execution" in result.details


def test_contradictory_evidence_fails_verification(
    successful_action: ActionResult,
    base_time: datetime,
):
    """5. Contradictory evidence (one claims cancelled, one claims renewing) fails verification."""
    post_action_time = successful_action.completed_at + timedelta(seconds=10)
    ev_cancelled = Evidence(
        evidence_id="ev_cancelled",
        subscription_id=successful_action.subscription_id,
        evidence_type="renewal_status",
        excerpt="Account billing status: terminated and cancelled.",
        captured_at=post_action_time,
        confidence=0.8,
    )
    ev_active = Evidence(
        evidence_id="ev_active",
        subscription_id=successful_action.subscription_id,
        evidence_type="renewal_status",
        excerpt="Next billing date scheduled: active renewal on 2026-02-01.",
        captured_at=post_action_time,
        confidence=0.8,
    )

    as_of = base_time + timedelta(minutes=5)
    result = verify_cancellation(
        action_result=successful_action,
        evidence_list=[ev_cancelled, ev_active],
        as_of=as_of,
    )

    assert result.status == VerificationStatus.FAILED
    assert result.final_state_observed == "contradictory"
    assert "Contradictory evidence detected" in result.details


def test_subscription_id_mismatch_raises_error(
    successful_action: ActionResult,
    base_time: datetime,
):
    """6. Subscription ID mismatch on subscription or evidence raises VerificationMismatchError."""
    wrong_sub = Subscription(
        subscription_id="sub_different_999",
        service_name="CineVault Mock Streaming",
        status=SubscriptionStatus.CANCELLED,
    )

    with pytest.raises(VerificationMismatchError, match="Subscription ID mismatch"):
        verify_cancellation(
            action_result=successful_action,
            observed_subscription=wrong_sub,
            as_of=base_time + timedelta(minutes=5),
        )

    # Evidence from another subscription
    wrong_ev = Evidence(
        evidence_id="ev_wrong_sub",
        subscription_id="sub_other_user",
        evidence_type="renewal_status",
        excerpt="Status cancelled.",
        captured_at=base_time + timedelta(minutes=1),
        confidence=0.9,
    )
    with pytest.raises(VerificationMismatchError, match="Evidence subscription ID mismatch"):
        verify_cancellation(
            action_result=successful_action,
            evidence_list=[wrong_ev],
            as_of=base_time + timedelta(minutes=5),
        )


def test_service_identity_mismatch_raises_error(
    successful_action: ActionResult,
    observed_cancelled_sub: Subscription,
    base_time: datetime,
):
    """7. Service identity mismatch raises VerificationMismatchError."""
    with pytest.raises(VerificationMismatchError, match="Service identity mismatch"):
        verify_cancellation(
            action_result=successful_action,
            observed_subscription=observed_cancelled_sub,
            service_name="TotallyDifferentService",
            as_of=base_time + timedelta(minutes=5),
        )


def test_pause_observed_never_reported_as_cancelled(
    successful_action: ActionResult,
    base_time: datetime,
):
    """8. A paused subscription must NEVER be reported as verified cancellation."""
    paused_sub = Subscription(
        subscription_id=successful_action.subscription_id,
        service_name="CineVault Mock Streaming",
        status=SubscriptionStatus.ACTIVE,
        pause_option_available=True,
    )
    post_time = successful_action.completed_at + timedelta(seconds=5)
    ev_paused = Evidence(
        evidence_id="ev_pause",
        subscription_id=successful_action.subscription_id,
        evidence_type="renewal_status",
        excerpt="Subscription is currently paused. Billing suspended for 90 days.",
        captured_at=post_time,
        confidence=0.95,
    )

    as_of = base_time + timedelta(minutes=5)
    result = verify_cancellation(
        action_result=successful_action,
        observed_subscription=paused_sub,
        evidence_list=[ev_paused],
        as_of=as_of,
    )

    assert result.status == VerificationStatus.FAILED
    assert result.final_state_observed == "paused"
    assert "PAUSED, not cancelled" in result.details


def test_failed_action_never_reported_as_verified(
    base_time: datetime,
):
    """9. An action with status FAILED cannot be verified as cancelled."""
    failed_action = ActionResult(
        action_id="act_failed_01",
        plan_id="plan_01",
        subscription_id="sub_tc1_cinevault",
        status=ActionStatus.FAILED,
        started_at=base_time,
        completed_at=base_time + timedelta(seconds=1),
        message="Execution timed out.",
        action_type="cancellation",
    )
    cancelled_sub = Subscription(
        subscription_id="sub_tc1_cinevault",
        service_name="CineVault Mock Streaming",
        status=SubscriptionStatus.CANCELLED,
    )

    as_of = base_time + timedelta(minutes=5)
    result = verify_cancellation(
        action_result=failed_action,
        observed_subscription=cancelled_sub,
        as_of=as_of,
    )

    assert result.status == VerificationStatus.FAILED
    assert result.final_state_observed == "unattempted_or_failed"
    assert "prior action status is 'failed'" in result.details


def test_repeated_verification_is_pure_and_deterministic(
    successful_action: ActionResult,
    observed_cancelled_sub: Subscription,
    base_time: datetime,
):
    """10. Repeated verification calls with identical data return identical results."""
    as_of = base_time + timedelta(minutes=5)

    res_1 = verify_cancellation(
        action_result=successful_action,
        observed_subscription=observed_cancelled_sub,
        as_of=as_of,
    )
    res_2 = verify_cancellation(
        action_result=successful_action,
        observed_subscription=observed_cancelled_sub,
        as_of=as_of,
    )

    assert res_1.verification_id == res_2.verification_id
    assert res_1.status == res_2.status
    assert res_1.final_state_observed == res_2.final_state_observed
    assert res_1.details == res_2.details


def test_verification_never_executes_or_mutates_action(
    successful_action: ActionResult,
    observed_cancelled_sub: Subscription,
    base_time: datetime,
):
    """11. Verification is purely an observer and never mutates or re-executes actions."""
    action_before = successful_action.model_dump()
    sub_before = observed_cancelled_sub.model_dump()

    verify_cancellation(
        action_result=successful_action,
        observed_subscription=observed_cancelled_sub,
        as_of=base_time + timedelta(minutes=5),
    )

    assert successful_action.model_dump() == action_before
    assert observed_cancelled_sub.model_dump() == sub_before
