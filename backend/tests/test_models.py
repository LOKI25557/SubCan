"""Tests for SubCan Pydantic models, validation rules, and fixture compliance."""

from datetime import date, datetime, timezone
import json
from pathlib import Path
import pytest
from pydantic import ValidationError

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
# Subscription Model Tests
# ==============================================================================

def test_valid_subscription():
    """Verify a complete, valid subscription passes validation."""
    sub = Subscription(
        subscription_id="sub_test_01",
        service_name="TestFlix",
        status=SubscriptionStatus.ACTIVE,
        renewal_date=date(2026, 11, 1),
        last_used_at=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
        source_url="https://mock.streaming.test/account",
        source_record_id="rec_123",
        monthly_cost=15.99,
        currency="USD",
        billing_cycle="monthly",
        cancellation_terms_url="https://mock.streaming.test/terms",
        data_deletion_warning=False,
        pause_option_available=False,
    )
    assert sub.subscription_id == "sub_test_01"
    assert sub.status == SubscriptionStatus.ACTIVE
    assert sub.monthly_cost == 15.99


def test_missing_usage_history_accepted_as_none():
    """Verify missing usage history is accepted as None / null rather than inventing a date."""
    sub = Subscription(
        subscription_id="sub_test_missing_usage",
        service_name="MysteriousStream",
        status=SubscriptionStatus.UNKNOWN,
        renewal_date=None,
        last_used_at=None,
    )
    assert sub.last_used_at is None
    assert sub.status == SubscriptionStatus.UNKNOWN


def test_invalid_subscription_status_rejected():
    """Verify invalid subscription status values are rejected."""
    with pytest.raises(ValidationError) as exc_info:
        Subscription(
            subscription_id="sub_invalid_status",
            service_name="TestStream",
            status="active_and_cancelled",  # type: ignore[arg-type]
        )
    assert "Input should be 'active'" in str(exc_info.value)


def test_extra_fields_forbidden_on_subscription():
    """Verify extraneous unexpected fields are strictly rejected."""
    with pytest.raises(ValidationError) as exc_info:
        Subscription(
            subscription_id="sub_extra",
            service_name="TestStream",
            status=SubscriptionStatus.ACTIVE,
            unknown_random_attribute=123,  # type: ignore[call-arg]
        )
    assert "Extra inputs are not permitted" in str(exc_info.value)


# ==============================================================================
# Evidence Model Tests
# ==============================================================================

def test_valid_evidence():
    """Verify valid evidence with timezone-aware timestamp and confidence in [0, 1]."""
    evidence = Evidence(
        evidence_id="ev_001",
        subscription_id="sub_001",
        evidence_type="usage_history",
        excerpt="No stream detected for 120 days.",
        source_url="https://mock.streaming.test/logs",
        captured_at=datetime(2026, 10, 19, 10, 0, 0, tzinfo=timezone.utc),
        confidence=0.95,
        raw_reference={"connector": "mock_playback_api"},
    )
    assert evidence.confidence == 0.95
    assert evidence.captured_at.tzinfo is not None


def test_confidence_above_one_rejected():
    """Verify confidence > 1.0 is rejected."""
    with pytest.raises(ValidationError) as exc_info:
        Evidence(
            evidence_id="ev_err_high",
            subscription_id="sub_001",
            evidence_type="usage_history",
            excerpt="High confidence test",
            captured_at=datetime.now(timezone.utc),
            confidence=1.5,
        )
    assert "less than or equal to 1" in str(exc_info.value)


def test_confidence_below_zero_rejected():
    """Verify confidence < 0.0 is rejected."""
    with pytest.raises(ValidationError) as exc_info:
        Evidence(
            evidence_id="ev_err_low",
            subscription_id="sub_001",
            evidence_type="usage_history",
            excerpt="Negative confidence test",
            captured_at=datetime.now(timezone.utc),
            confidence=-0.1,
        )
    assert "greater than or equal to 0" in str(exc_info.value)


def test_naive_timestamp_rejected_for_evidence():
    """Verify timezone-naive timestamps are rejected for Evidence.captured_at."""
    naive_dt = datetime(2026, 10, 19, 10, 0, 0)  # No tzinfo
    with pytest.raises(ValidationError) as exc_info:
        Evidence(
            evidence_id="ev_naive",
            subscription_id="sub_001",
            evidence_type="usage_history",
            excerpt="Naive timestamp test",
            captured_at=naive_dt,
            confidence=0.8,
        )
    assert "captured_at timestamp must be timezone-aware" in str(exc_info.value)


# ==============================================================================
# Decision, Plan, Approval, Action, Verification Models
# ==============================================================================

def test_decision_model_validation():
    """Verify Decision model validates eligibility and uncertainty range."""
    decision = Decision(
        decision_id="dec_01",
        subscription_id="sub_01",
        eligibility=EligibilityStatus.CANDIDATE,
        reason="No usage in 110 days.",
        uncertainty=0.05,
        rule_version="1.0.0",
        evaluated_at=datetime.now(timezone.utc),
        data_deletion_risk=True,
        consequences=["Playlists will be deleted immediately."],
    )
    assert decision.eligibility == EligibilityStatus.CANDIDATE
    assert decision.data_deletion_risk is True


def test_cancellation_plan_and_approval_model():
    """Verify plan creation and exact plan/service binding in Approval."""
    plan = CancellationPlan(
        plan_id="plan_01",
        subscription_id="sub_01",
        service_name="CineVault Mock Streaming",
        plan_version="v1.0",
        steps=["Navigate to cancellation page", "Confirm cancellation reason"],
        consequences_warning="Access ends immediately",
        status=ApprovalStatus.AWAITING_APPROVAL,
        created_at=datetime.now(timezone.utc),
    )
    assert plan.status == ApprovalStatus.AWAITING_APPROVAL

    approval = Approval(
        approval_id="appr_01",
        plan_id=plan.plan_id,
        subscription_id=plan.subscription_id,
        service_name=plan.service_name,
        plan_version=plan.plan_version,
        status=ApprovalStatus.APPROVED,
        approved_by="test_user",
        decided_at=datetime.now(timezone.utc),
    )
    assert approval.status == ApprovalStatus.APPROVED
    assert approval.service_name == "CineVault Mock Streaming"


def test_action_result_and_independent_verification_separation():
    """Verify ActionResult does not conflate execution success with verified cancellation."""
    action = ActionResult(
        action_id="act_01",
        plan_id="plan_01",
        subscription_id="sub_01",
        status=ActionStatus.SUCCEEDED,
        completed_at=datetime.now(timezone.utc),
        message="Cancellation request submitted to mock API.",
    )
    assert action.status == ActionStatus.SUCCEEDED

    # Verification is an independent entity with its own verification status
    verif = VerificationResult(
        verification_id="verif_01",
        action_id=action.action_id,
        subscription_id=action.subscription_id,
        status=VerificationStatus.VERIFIED,
        method="renewal_status_check",
        verified_at=datetime.now(timezone.utc),
        final_state_observed="cancelled",
    )
    assert verif.status == VerificationStatus.VERIFIED
    assert verif.final_state_observed == "cancelled"


def test_agent_run_model():
    """Verify AgentRun model aggregates subscription outcomes."""
    run = AgentRun(
        run_id="run_100",
        started_at=datetime.now(timezone.utc),
        status="completed",
        subscription_ids=["sub_01", "sub_02"],
        final_results={
            "sub_01": FinalResultStatus.VERIFIED_CANCELLED,
            "sub_02": FinalResultStatus.KEPT,
        },
        summary="Processed 2 subscriptions: 1 cancelled, 1 kept.",
    )
    assert run.final_results["sub_01"] == FinalResultStatus.VERIFIED_CANCELLED
    assert run.final_results["sub_02"] == FinalResultStatus.KEPT


# ==============================================================================
# Fixtures Validation Tests
# ==============================================================================

def test_fixtures_file_exists_and_parses():
    """Verify fixtures/subscriptions.json exists and contains valid JSON."""
    fixtures_path = Path(__file__).resolve().parents[2] / "fixtures" / "subscriptions.json"
    assert fixtures_path.exists(), f"Fixtures file not found at {fixtures_path}"

    with fixtures_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    assert "subscriptions" in data
    assert "scenarios" in data
    assert len(data["subscriptions"]) >= 5


def test_fixtures_all_subscriptions_validate():
    """Verify every subscription in fixtures passes Pydantic model validation."""
    fixtures_path = Path(__file__).resolve().parents[2] / "fixtures" / "subscriptions.json"
    with fixtures_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    for sub_data in data["subscriptions"]:
        sub = Subscription.model_validate(sub_data)
        assert sub.subscription_id
        assert sub.service_name
        assert sub.status in SubscriptionStatus


def test_fixtures_all_scenarios_and_evidence_validate():
    """Verify all 4 test scenarios (TC1, TC2, TC3, TC4) and their evidence validate."""
    fixtures_path = Path(__file__).resolve().parents[2] / "fixtures" / "subscriptions.json"
    with fixtures_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    scenarios = data["scenarios"]
    for tc_key in ["TC1", "TC2", "TC3", "TC4"]:
        assert tc_key in scenarios, f"Scenario {tc_key} missing from fixtures"
        scenario = scenarios[tc_key]
        assert "evidence" in scenario
        for ev_data in scenario["evidence"]:
            ev = Evidence.model_validate(ev_data)
            assert ev.confidence >= 0.0 and ev.confidence <= 1.0
            assert ev.captured_at.tzinfo is not None


def test_tc1_fixture_invariants():
    """TC1: Active subscription unused for > 3 calendar months."""
    fixtures_path = Path(__file__).resolve().parents[2] / "fixtures" / "subscriptions.json"
    with fixtures_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    sub_tc1 = next(s for s in data["subscriptions"] if s["subscription_id"] == "sub_tc1_cinevault")
    sub = Subscription.model_validate(sub_tc1)
    assert sub.status == SubscriptionStatus.ACTIVE
    assert sub.last_used_at is not None
    # Verify cancellation terms URL exists
    assert sub.cancellation_terms_url is not None


def test_tc2_fixture_invariants():
    """TC2: Data deletion risk flag enabled."""
    fixtures_path = Path(__file__).resolve().parents[2] / "fixtures" / "subscriptions.json"
    with fixtures_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    sub_tc2 = next(s for s in data["subscriptions"] if s["subscription_id"] == "sub_tc2_audiocloud")
    sub = Subscription.model_validate(sub_tc2)
    assert sub.data_deletion_warning is True


def test_tc3_fixture_invariants():
    """TC3: Pause option available flag enabled."""
    fixtures_path = Path(__file__).resolve().parents[2] / "fixtures" / "subscriptions.json"
    with fixtures_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    sub_tc3 = next(s for s in data["subscriptions"] if s["subscription_id"] == "sub_tc3_fitstream")
    sub = Subscription.model_validate(sub_tc3)
    assert sub.pause_option_available is True


def test_tc4_fixture_invariants():
    """TC4: Multiple subscriptions for isolation verification."""
    fixtures_path = Path(__file__).resolve().parents[2] / "fixtures" / "subscriptions.json"
    with fixtures_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    tc4_ids = {"sub_tc4_moviemax", "sub_tc4_gamestream"}
    found_ids = {s["subscription_id"] for s in data["subscriptions"] if s["subscription_id"] in tc4_ids}
    assert found_ids == tc4_ids
