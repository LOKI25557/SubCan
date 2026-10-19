"""Unit and integration tests for Phase 6 Agent Orchestration & Workflow Integration (backend/app/agent.py).

Verifies the 14 critical requirements:
1. Starting a run does not execute cancellation.
2. Eligible subscriptions receive plans awaiting approval.
3. Ineligible subscriptions do not receive cancellation plans.
4. Review-required subscriptions do not receive executable plans.
5. No approval means no execution.
6. Approving one subscription leaves every other subscription untouched.
7. An invalid or stale approval is rejected.
8. A valid approval authorizes only its exact plan and subscription.
9. A successful action followed by renewal still being enabled is not reported as a verified cancellation.
10. A pause is never reported as cancellation.
11. Missing verification evidence is not treated as success.
12. Duplicate action requests respect the existing idempotency behavior.
13. Repeated runs do not accidentally mutate source fixtures.
14. Complete multi-subscription reconciliation and declined approval handling.
"""

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Dict, List, Optional
import pytest

from backend.app.actions import (
    IdempotencyConflictError,
    clear_action_registry,
    get_action_registry,
)
from backend.app.agent import (
    AgentRunRegistry,
    SubscriptionRunState,
    clear_agent_run_registry,
    get_agent_run_registry,
    initialize_orchestration_run,
    load_fixture_data,
    process_subscription_approval,
    run_orchestration_cycle,
)
from backend.app.approvals import create_approval
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


# ==============================================================================
# Test Fixtures & Setup
# ==============================================================================

@pytest.fixture(autouse=True)
def clean_registries():
    """Ensure clean action and agent run registries before and after each test."""
    clear_action_registry()
    clear_agent_run_registry()
    yield
    clear_action_registry()
    clear_agent_run_registry()


@pytest.fixture
def base_time() -> datetime:
    """Fixed past UTC timestamp for deterministic test execution."""
    return datetime(2026, 10, 19, 10, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def candidate_sub(base_time: datetime) -> Subscription:
    """Standard streaming subscription inactive for > 3 calendar months."""
    return Subscription(
        subscription_id="sub_tc1_cinevault",
        service_name="CineVault Mock Streaming",
        status=SubscriptionStatus.ACTIVE,
        renewal_date=date(2026, 11, 15),
        last_used_at=datetime(2026, 6, 10, 20, 30, 0, tzinfo=timezone.utc),
        monthly_cost=14.99,
        cancellation_terms_url="https://mock.streaming.test/cinevault/terms/cancellation",
        data_deletion_warning=False,
        pause_option_available=False,
    )


@pytest.fixture
def candidate_evidence(base_time: datetime) -> List[Evidence]:
    return [
        Evidence(
            evidence_id="ev_tc1_01",
            subscription_id="sub_tc1_cinevault",
            evidence_type="usage_history",
            excerpt="Last playback activity observed on 2026-06-10T20:30:00Z. No subsequent playback.",
            captured_at=base_time - timedelta(days=1),
            confidence=0.95,
        ),
        Evidence(
            evidence_id="ev_tc1_02",
            subscription_id="sub_tc1_cinevault",
            evidence_type="cancellation_terms",
            excerpt="Subscribers may cancel at any time. Viewing history preserved for 12 months.",
            captured_at=base_time - timedelta(days=1),
            confidence=1.0,
        ),
    ]


@pytest.fixture
def recently_used_sub(base_time: datetime) -> Subscription:
    """Subscription active and used within the last 10 days (NOT_ELIGIBLE)."""
    return Subscription(
        subscription_id="sub_recent_001",
        service_name="DailyStream Mock",
        status=SubscriptionStatus.ACTIVE,
        renewal_date=date(2026, 11, 1),
        last_used_at=base_time - timedelta(days=10),
        monthly_cost=9.99,
    )


@pytest.fixture
def recently_used_evidence(base_time: datetime) -> List[Evidence]:
    return [
        Evidence(
            evidence_id="ev_recent_01",
            subscription_id="sub_recent_001",
            evidence_type="usage_history",
            excerpt="Active playback stream logged 10 days ago.",
            captured_at=base_time - timedelta(days=1),
            confidence=0.99,
        )
    ]


# ==============================================================================
# Phase 6 Integration Tests
# ==============================================================================

def test_1_starting_run_does_not_execute_cancellation(
    candidate_sub: Subscription,
    candidate_evidence: List[Evidence],
    base_time: datetime,
):
    """1. Starting a run does not execute cancellation. Action registry remains empty."""
    run, contexts = initialize_orchestration_run(
        subscriptions=[candidate_sub],
        evidence_map={candidate_sub.subscription_id: candidate_evidence},
        as_of=base_time,
    )

    # Run initialized
    assert run.run_id is not None
    assert run.status == "in_progress"
    assert candidate_sub.subscription_id in contexts

    state = contexts[candidate_sub.subscription_id]
    # Decision is candidate, plan is generated but awaiting approval
    assert state.decision is not None
    assert state.decision.eligibility == EligibilityStatus.CANDIDATE
    assert state.plan is not None
    assert state.plan.status == ApprovalStatus.AWAITING_APPROVAL

    # Invariant: NO action was executed
    assert state.action_result is None
    assert state.verification_result is None
    assert state.approval is None
    assert state.final_status == FinalResultStatus.PENDING

    # Action registry has no recorded actions
    action_reg = get_action_registry()
    assert len(action_reg._actions_by_id) == 0


def test_2_eligible_subscriptions_receive_plans_awaiting_approval(
    candidate_sub: Subscription,
    candidate_evidence: List[Evidence],
    base_time: datetime,
):
    """2. Eligible subscriptions receive structured cancellation plans awaiting approval."""
    run, contexts = initialize_orchestration_run(
        subscriptions=[candidate_sub],
        evidence_map={candidate_sub.subscription_id: candidate_evidence},
        as_of=base_time,
    )

    state = contexts[candidate_sub.subscription_id]
    assert state.plan is not None
    assert state.plan.subscription_id == candidate_sub.subscription_id
    assert state.plan.service_name == candidate_sub.service_name
    assert state.plan.status == ApprovalStatus.AWAITING_APPROVAL
    assert len(state.plan.steps) > 0
    assert state.final_status == FinalResultStatus.PENDING
    assert run.final_results[candidate_sub.subscription_id] == FinalResultStatus.PENDING


def test_3_ineligible_subscriptions_do_not_receive_cancellation_plans(
    recently_used_sub: Subscription,
    recently_used_evidence: List[Evidence],
    base_time: datetime,
):
    """3. Ineligible subscriptions do not receive cancellation plans and are marked KEPT."""
    run, contexts = initialize_orchestration_run(
        subscriptions=[recently_used_sub],
        evidence_map={recently_used_sub.subscription_id: recently_used_evidence},
        as_of=base_time,
    )

    state = contexts[recently_used_sub.subscription_id]
    assert state.decision is not None
    assert state.decision.eligibility == EligibilityStatus.NOT_ELIGIBLE
    # No cancellation plan generated
    assert state.plan is None
    assert state.action_result is None
    assert state.final_status == FinalResultStatus.KEPT
    assert run.final_results[recently_used_sub.subscription_id] == FinalResultStatus.KEPT
    # Since no candidates exist, run is immediately completed
    assert run.status == "completed"


def test_4_review_required_subscriptions_do_not_receive_executable_plans(
    base_time: datetime,
):
    """4. Review-required subscriptions do not receive executable plans and are marked BLOCKED."""
    # Missing usage history (last_used_at is None and no evidence) triggers NEEDS_REVIEW
    sub_no_usage = Subscription(
        subscription_id="sub_missing_usage_01",
        service_name="MysteriousStream Mock",
        status=SubscriptionStatus.ACTIVE,
        last_used_at=None,
    )

    run, contexts = initialize_orchestration_run(
        subscriptions=[sub_no_usage],
        evidence_map={},
        as_of=base_time,
    )

    state = contexts[sub_no_usage.subscription_id]
    assert state.decision is not None
    assert state.decision.eligibility == EligibilityStatus.NEEDS_REVIEW
    # No cancellation plan generated
    assert state.plan is None
    assert state.action_result is None
    assert state.final_status == FinalResultStatus.BLOCKED
    assert run.final_results[sub_no_usage.subscription_id] == FinalResultStatus.BLOCKED
    assert run.status == "completed"


def test_5_no_approval_means_no_execution(
    candidate_sub: Subscription,
    candidate_evidence: List[Evidence],
    base_time: datetime,
):
    """5. Without explicit user approval, no execution occurs."""
    run, contexts = initialize_orchestration_run(
        subscriptions=[candidate_sub],
        evidence_map={candidate_sub.subscription_id: candidate_evidence},
        as_of=base_time,
    )

    # Time passes, but no approval is submitted
    state = contexts[candidate_sub.subscription_id]
    assert state.plan.status == ApprovalStatus.AWAITING_APPROVAL
    assert state.approval is None
    assert state.action_result is None
    assert state.verification_result is None
    assert len(get_action_registry()._actions_by_id) == 0


def test_6_approving_one_subscription_leaves_others_untouched(base_time: datetime):
    """6. Approving one subscription leaves every other subscription completely untouched."""
    # Load TC4 scenario from fixtures: MovieMax and GameStream
    subscriptions, evidence_map = load_fixture_data(
        target_subscription_ids=["sub_tc4_moviemax", "sub_tc4_gamestream"]
    )
    assert len(subscriptions) == 2

    run, contexts = initialize_orchestration_run(
        subscriptions=subscriptions,
        evidence_map=evidence_map,
        as_of=base_time,
    )

    state_mm = contexts["sub_tc4_moviemax"]
    state_gs = contexts["sub_tc4_gamestream"]

    assert state_mm.plan is not None
    assert state_gs.plan is not None

    # Create approval ONLY for MovieMax
    approval_mm = create_approval(
        plan=state_mm.plan,
        approved=True,
        approved_by="user@example.com",
        decided_at=base_time,
    )

    # Observed post-action entity for MovieMax
    observed_mm = Subscription(
        subscription_id="sub_tc4_moviemax",
        service_name="MovieMax Mock",
        status=SubscriptionStatus.CANCELLED,
        renewal_date=None,
        last_used_at=state_mm.subscription.last_used_at,
    )

    # Process MovieMax approval
    updated_state_mm = process_subscription_approval(
        run_id=run.run_id,
        plan_id=state_mm.plan.plan_id,
        approval=approval_mm,
        observed_subscription=observed_mm,
        as_of=base_time,
    )

    # Verify MovieMax proceeded through execution and verification
    assert updated_state_mm.final_status == FinalResultStatus.VERIFIED_CANCELLED
    assert updated_state_mm.action_result is not None
    assert updated_state_mm.verification_result is not None

    # CRITICAL INVARIANT: GameStream must remain completely untouched!
    updated_state_gs = contexts["sub_tc4_gamestream"]
    assert updated_state_gs.approval is None
    assert updated_state_gs.action_result is None
    assert updated_state_gs.verification_result is None
    assert updated_state_gs.plan.status == ApprovalStatus.AWAITING_APPROVAL
    assert updated_state_gs.final_status == FinalResultStatus.PENDING
    assert run.final_results["sub_tc4_gamestream"] == FinalResultStatus.PENDING


def test_7_invalid_or_stale_approval_is_rejected(
    candidate_sub: Subscription,
    candidate_evidence: List[Evidence],
    base_time: datetime,
):
    """7. An invalid or stale approval is rejected (marked BLOCKED, no execution)."""
    run, contexts = initialize_orchestration_run(
        subscriptions=[candidate_sub],
        evidence_map={candidate_sub.subscription_id: candidate_evidence},
        as_of=base_time,
    )
    state = contexts[candidate_sub.subscription_id]
    plan = state.plan

    # 7a. Stale plan version approval
    stale_approval = Approval(
        approval_id="appr_stale_01",
        plan_id=plan.plan_id,
        subscription_id=plan.subscription_id,
        service_name=plan.service_name,
        plan_version="v_old_invalid_fingerprint_00000000",
        status=ApprovalStatus.APPROVED,
        approved_by="user@example.com",
        decided_at=base_time,
    )

    updated_state = process_subscription_approval(
        run_id=run.run_id,
        plan_id=plan.plan_id,
        approval=stale_approval,
        as_of=base_time,
    )

    assert updated_state.final_status == FinalResultStatus.BLOCKED
    assert updated_state.action_result is None
    assert "Approval validation failed closed" in updated_state.diagnostic_notes

    # 7b. Plan ID mismatch
    wrong_plan_approval = Approval(
        approval_id="appr_wrong_01",
        plan_id="plan_wrong_id_999",
        subscription_id=plan.subscription_id,
        service_name=plan.service_name,
        plan_version=plan.plan_version,
        status=ApprovalStatus.APPROVED,
        approved_by="user@example.com",
        decided_at=base_time,
    )

    updated_state_wrong = process_subscription_approval(
        run_id=run.run_id,
        plan_id="plan_wrong_id_999",
        approval=wrong_plan_approval,
        as_of=base_time,
    )
    assert updated_state_wrong.final_status == FinalResultStatus.BLOCKED
    assert updated_state_wrong.action_result is None


def test_8_valid_approval_authorizes_only_exact_plan_and_subscription(
    candidate_sub: Subscription,
    candidate_evidence: List[Evidence],
    base_time: datetime,
):
    """8. A valid approval authorizes only its exact plan and subscription."""
    run, contexts = initialize_orchestration_run(
        subscriptions=[candidate_sub],
        evidence_map={candidate_sub.subscription_id: candidate_evidence},
        as_of=base_time,
    )
    state = contexts[candidate_sub.subscription_id]
    plan = state.plan

    approval = create_approval(
        plan=plan,
        approved=True,
        approved_by="user@example.com",
        decided_at=base_time,
    )

    observed_sub = Subscription(
        subscription_id=candidate_sub.subscription_id,
        service_name=candidate_sub.service_name,
        status=SubscriptionStatus.CANCELLED,
        renewal_date=None,
        last_used_at=candidate_sub.last_used_at,
    )

    result_state = process_subscription_approval(
        run_id=run.run_id,
        plan_id=plan.plan_id,
        approval=approval,
        observed_subscription=observed_sub,
        as_of=base_time,
    )

    assert result_state.action_result is not None
    assert result_state.action_result.subscription_id == candidate_sub.subscription_id
    assert result_state.action_result.plan_id == plan.plan_id
    assert result_state.action_result.status == ActionStatus.SUCCEEDED
    assert result_state.final_status == FinalResultStatus.VERIFIED_CANCELLED
    assert run.final_results[candidate_sub.subscription_id] == FinalResultStatus.VERIFIED_CANCELLED


def test_9_action_success_with_renewal_still_enabled_fails_verification(
    candidate_sub: Subscription,
    candidate_evidence: List[Evidence],
    base_time: datetime,
):
    """9. Successful action followed by renewal still active is NOT reported as verified cancellation."""
    run, contexts = initialize_orchestration_run(
        subscriptions=[candidate_sub],
        evidence_map={candidate_sub.subscription_id: candidate_evidence},
        as_of=base_time,
    )
    state = contexts[candidate_sub.subscription_id]
    plan = state.plan

    approval = create_approval(
        plan=plan,
        approved=True,
        approved_by="user@example.com",
        decided_at=base_time,
    )

    # Observed subscription STILL HAS active renewal date!
    observed_active = Subscription(
        subscription_id=candidate_sub.subscription_id,
        service_name=candidate_sub.service_name,
        status=SubscriptionStatus.ACTIVE,
        renewal_date=date(2026, 12, 15),
        last_used_at=candidate_sub.last_used_at,
    )

    result_state = process_subscription_approval(
        run_id=run.run_id,
        plan_id=plan.plan_id,
        approval=approval,
        observed_subscription=observed_active,
        as_of=base_time,
    )

    # Action succeeded, but verification failed
    assert result_state.action_result.status == ActionStatus.SUCCEEDED
    assert result_state.verification_result.status == VerificationStatus.FAILED
    assert result_state.final_status == FinalResultStatus.FAILED
    assert run.final_results[candidate_sub.subscription_id] == FinalResultStatus.FAILED


def test_10_pause_is_never_reported_as_cancellation(
    candidate_sub: Subscription,
    candidate_evidence: List[Evidence],
    base_time: datetime,
):
    """10. A pause is never reported as cancellation (VerificationStatus.FAILED, FinalResultStatus.FAILED)."""
    run, contexts = initialize_orchestration_run(
        subscriptions=[candidate_sub],
        evidence_map={candidate_sub.subscription_id: candidate_evidence},
        as_of=base_time,
    )
    state = contexts[candidate_sub.subscription_id]
    plan = state.plan

    approval = create_approval(
        plan=plan,
        approved=True,
        approved_by="user@example.com",
        decided_at=base_time,
    )

    # Post-action evidence showing subscription was paused instead of cancelled
    post_time = base_time + timedelta(seconds=5)
    ev_paused = Evidence(
        evidence_id="ev_pause_01",
        subscription_id=candidate_sub.subscription_id,
        evidence_type="renewal_status",
        excerpt="Subscription is currently paused. Billing suspended for 90 days. A paused subscription is NOT cancelled.",
        captured_at=post_time,
        confidence=0.95,
    )
    observed_paused = Subscription(
        subscription_id=candidate_sub.subscription_id,
        service_name=candidate_sub.service_name,
        status=SubscriptionStatus.ACTIVE,
        pause_option_available=True,
        renewal_date=None,
        last_used_at=candidate_sub.last_used_at,
    )

    result_state = process_subscription_approval(
        run_id=run.run_id,
        plan_id=plan.plan_id,
        approval=approval,
        observed_subscription=observed_paused,
        post_action_evidence=[ev_paused],
        as_of=base_time,
    )

    assert result_state.verification_result.status == VerificationStatus.FAILED
    assert result_state.final_status == FinalResultStatus.FAILED
    assert "pause" in result_state.verification_result.details.lower()


def test_11_missing_verification_evidence_is_not_treated_as_success(
    candidate_sub: Subscription,
    candidate_evidence: List[Evidence],
    base_time: datetime,
):
    """11. Missing verification evidence fails closed as PENDING (never treats action as proof)."""
    run, contexts = initialize_orchestration_run(
        subscriptions=[candidate_sub],
        evidence_map={candidate_sub.subscription_id: candidate_evidence},
        as_of=base_time,
    )
    state = contexts[candidate_sub.subscription_id]
    plan = state.plan

    approval = create_approval(
        plan=plan,
        approved=True,
        approved_by="user@example.com",
        decided_at=base_time,
    )

    # No observed subscription, no post-action evidence
    result_state = process_subscription_approval(
        run_id=run.run_id,
        plan_id=plan.plan_id,
        approval=approval,
        observed_subscription=None,
        post_action_evidence=None,
        as_of=base_time,
    )

    assert result_state.action_result.status == ActionStatus.SUCCEEDED
    assert result_state.verification_result.status == VerificationStatus.PENDING
    assert result_state.final_status == FinalResultStatus.PENDING
    # Overall run remains in_progress
    assert run.status == "in_progress"


def test_12_duplicate_action_requests_respect_idempotency_behavior(
    candidate_sub: Subscription,
    candidate_evidence: List[Evidence],
    base_time: datetime,
):
    """12. Duplicate action requests respect the process-local idempotency mechanism."""
    run, contexts = initialize_orchestration_run(
        subscriptions=[candidate_sub],
        evidence_map={candidate_sub.subscription_id: candidate_evidence},
        as_of=base_time,
    )
    state = contexts[candidate_sub.subscription_id]
    plan = state.plan

    approval = create_approval(
        plan=plan,
        approved=True,
        approved_by="user@example.com",
        decided_at=base_time,
    )

    observed_sub = Subscription(
        subscription_id=candidate_sub.subscription_id,
        service_name=candidate_sub.service_name,
        status=SubscriptionStatus.CANCELLED,
        renewal_date=None,
        last_used_at=candidate_sub.last_used_at,
    )

    idemp_key = "idemp_test_agent_123"

    # First execution
    state_1 = process_subscription_approval(
        run_id=run.run_id,
        plan_id=plan.plan_id,
        approval=approval,
        idempotency_key=idemp_key,
        observed_subscription=observed_sub,
        as_of=base_time,
    )
    act_id_1 = state_1.action_result.action_id

    # Re-executing with the identical idempotency key replays the same action
    state_2 = process_subscription_approval(
        run_id=run.run_id,
        plan_id=plan.plan_id,
        approval=approval,
        idempotency_key=idemp_key,
        observed_subscription=observed_sub,
        as_of=base_time,
    )
    act_id_2 = state_2.action_result.action_id
    assert act_id_1 == act_id_2

    # Verify only ONE action exists in the registry
    assert len(get_action_registry()._actions_by_id) == 1


def test_13_repeated_runs_do_not_mutate_source_fixtures():
    """13. Repeated runs do not accidentally mutate source fixtures on disk."""
    fixtures_path = Path(__file__).resolve().parents[2] / "fixtures" / "subscriptions.json"
    with fixtures_path.open("r", encoding="utf-8") as f:
        original_json_str = f.read()

    # Execute multiple orchestration runs
    run1 = run_orchestration_cycle()
    run2 = run_orchestration_cycle(subscription_ids=["sub_tc1_cinevault"])

    # Re-read fixture file
    with fixtures_path.open("r", encoding="utf-8") as f:
        after_json_str = f.read()

    # Content must be 100% byte-for-byte identical
    assert original_json_str == after_json_str


def test_14_user_declines_cancellation_keeps_subscription_active(
    candidate_sub: Subscription,
    candidate_evidence: List[Evidence],
    base_time: datetime,
):
    """14. When user declines cancellation, subscription is marked KEPT and no action is executed."""
    run, contexts = initialize_orchestration_run(
        subscriptions=[candidate_sub],
        evidence_map={candidate_sub.subscription_id: candidate_evidence},
        as_of=base_time,
    )
    state = contexts[candidate_sub.subscription_id]
    plan = state.plan

    # Create DECLINED approval
    declined_approval = create_approval(
        plan=plan,
        approved=False,
        approved_by="user@example.com",
        decided_at=base_time,
    )

    updated_state = process_subscription_approval(
        run_id=run.run_id,
        plan_id=plan.plan_id,
        approval=declined_approval,
        as_of=base_time,
    )

    assert updated_state.final_status == FinalResultStatus.KEPT
    assert updated_state.action_result is None
    assert updated_state.plan.status == ApprovalStatus.DECLINED
    assert run.final_results[candidate_sub.subscription_id] == FinalResultStatus.KEPT
    assert run.status == "completed"
