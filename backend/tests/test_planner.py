"""Unit tests for Phase 2 Cancellation Planner (backend/app/planner.py).

Verifies:
- Plan creation for eligible subscriptions (status AWAITING_APPROVAL).
- Ineligible and needs_review subscriptions rejected.
- Missing, uncertain, and contradictory terms produce safe review-required outcomes.
- Data-loss warnings disclosed in consequences_warning and steps.
- Pause alternatives kept strictly distinct from cancellation.
- Deterministic plan versioning and material change detection.
- Complete independence across multiple subscriptions (TC4).
- Purity: generating plans never executes cancellation.
"""

from datetime import date, datetime, timezone
import json
from pathlib import Path
import pytest

from backend.app.models import (
    ApprovalStatus,
    CancellationPlan,
    Decision,
    EligibilityStatus,
    Evidence,
    Subscription,
    SubscriptionStatus,
)
from backend.app.planner import (
    compute_plan_fingerprint,
    create_cancellation_plan,
    has_material_changes,
    is_review_required,
)
from backend.app.rules import evaluate_eligibility


# ==============================================================================
# Helper Factories
# ==============================================================================

def _make_candidate_sub(
    sub_id: str = "sub_plan_test",
    service_name: str = "TestFlix",
    data_deletion: bool = False,
    pause_available: bool = False,
    terms_url: str = "https://mock.streaming.test/terms",
) -> Subscription:
    return Subscription(
        subscription_id=sub_id,
        service_name=service_name,
        status=SubscriptionStatus.ACTIVE,
        renewal_date=date(2026, 11, 15),
        last_used_at=datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc),
        cancellation_terms_url=terms_url,
        data_deletion_warning=data_deletion,
        pause_option_available=pause_available,
    )


def _make_decision(
    sub_id: str = "sub_plan_test",
    eligibility: EligibilityStatus = EligibilityStatus.CANDIDATE,
    data_deletion_risk: bool = False,
    pause_alternative_available: bool = False,
) -> Decision:
    return Decision(
        decision_id=f"dec_{sub_id}_01",
        subscription_id=sub_id,
        eligibility=eligibility,
        reason="Inactive for > 3 calendar months.",
        uncertainty=0.05,
        rule_version="1.0.0",
        evaluated_at=datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc),
        data_deletion_risk=data_deletion_risk,
        pause_alternative_available=pause_alternative_available,
        consequences=[],
    )


# ==============================================================================
# Eligibility Gate Tests
# ==============================================================================

def test_plan_generation_for_eligible_candidate():
    """Verify plan generation succeeds for an eligible candidate."""
    sub = _make_candidate_sub()
    decision = _make_decision(sub.subscription_id, EligibilityStatus.CANDIDATE)
    evidence = Evidence(
        evidence_id="ev_terms_01",
        subscription_id=sub.subscription_id,
        evidence_type="cancellation_terms",
        excerpt="Cancel anytime. Access active until end of billing cycle.",
        captured_at=datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc),
        confidence=0.95,
    )

    plan = create_cancellation_plan(
        subscription=sub,
        decision=decision,
        evidence_list=[evidence],
        created_at=datetime(2026, 10, 19, 12, 5, 0, tzinfo=timezone.utc),
    )

    assert plan.subscription_id == sub.subscription_id
    assert plan.service_name == sub.service_name
    assert plan.status == ApprovalStatus.AWAITING_APPROVAL
    assert plan.plan_id.startswith("plan_sub_plan_test")
    assert "v1.0" in plan.plan_version
    # Steps verification
    assert any("Cancellation" in step for step in plan.steps)
    assert any("Inactive for > 3 calendar months" in step for step in plan.steps)
    assert any("ev_terms_01" in step for step in plan.steps)


def test_ineligible_subscription_rejected():
    """Subscriptions marked NOT_ELIGIBLE must be rejected with ValueError."""
    sub = _make_candidate_sub()
    decision = _make_decision(sub.subscription_id, EligibilityStatus.NOT_ELIGIBLE)

    with pytest.raises(ValueError, match="Only 'candidate' subscriptions are eligible"):
        create_cancellation_plan(subscription=sub, decision=decision)


def test_needs_review_subscription_rejected():
    """Subscriptions marked NEEDS_REVIEW must be rejected with ValueError."""
    sub = _make_candidate_sub()
    decision = _make_decision(sub.subscription_id, EligibilityStatus.NEEDS_REVIEW)

    with pytest.raises(ValueError, match="Only 'candidate' subscriptions are eligible"):
        create_cancellation_plan(subscription=sub, decision=decision)


# ==============================================================================
# Terms Handling: Missing, Uncertain, Contradictory
# ==============================================================================

def test_missing_terms_returns_safe_review_required():
    """Missing terms return a plan with review-required warning rather than invented terms."""
    sub = _make_candidate_sub()
    sub.cancellation_terms_url = None
    decision = _make_decision(sub.subscription_id, EligibilityStatus.CANDIDATE)

    plan = create_cancellation_plan(subscription=sub, decision=decision, evidence_list=[])

    assert is_review_required(plan) is True
    assert "REVIEW REQUIRED" in plan.consequences_warning
    assert "missing or inaccessible" in plan.consequences_warning
    assert any("MANUAL REVIEW REQUIRED" in step for step in plan.steps)


def test_uncertain_low_confidence_terms_returns_safe_review_required():
    """Terms evidence with low confidence (< 0.6) produces a review-required warning."""
    sub = _make_candidate_sub()
    decision = _make_decision(sub.subscription_id, EligibilityStatus.CANDIDATE)
    ev_low_conf = Evidence(
        evidence_id="ev_terms_low",
        subscription_id=sub.subscription_id,
        evidence_type="cancellation_terms",
        excerpt="Discretionary cancellation policy text.",
        captured_at=datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc),
        confidence=0.45,
    )

    plan = create_cancellation_plan(subscription=sub, decision=decision, evidence_list=[ev_low_conf])

    assert is_review_required(plan) is True
    assert "REVIEW REQUIRED" in plan.consequences_warning
    assert "low confidence" in plan.consequences_warning


def test_contradictory_terms_returns_safe_review_required():
    """Contradictory terms evidence items produce a review-required warning."""
    sub = _make_candidate_sub()
    decision = _make_decision(sub.subscription_id, EligibilityStatus.CANDIDATE)
    ev_free = Evidence(
        evidence_id="ev_terms_free",
        subscription_id=sub.subscription_id,
        evidence_type="cancellation_terms",
        excerpt="Free cancellation anytime with zero charge or fees.",
        captured_at=datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc),
        confidence=0.9,
    )
    ev_fee = Evidence(
        evidence_id="ev_terms_fee",
        subscription_id=sub.subscription_id,
        evidence_type="cancellation_terms",
        excerpt="Early termination fee applies if cancelled within contract term.",
        captured_at=datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc),
        confidence=0.9,
    )

    plan = create_cancellation_plan(
        subscription=sub,
        decision=decision,
        evidence_list=[ev_free, ev_fee],
    )

    assert is_review_required(plan) is True
    assert "REVIEW REQUIRED" in plan.consequences_warning
    assert "Contradictory cancellation terms" in plan.consequences_warning


# ==============================================================================
# Warnings & Consequence Disclosures
# ==============================================================================

def test_data_loss_warning_disclosed_in_plan():
    """Subscriptions with data deletion risk disclose warnings prominently."""
    sub = _make_candidate_sub(data_deletion=True)
    decision = _make_decision(sub.subscription_id, data_deletion_risk=True)

    plan = create_cancellation_plan(subscription=sub, decision=decision)

    assert plan.consequences_warning is not None
    assert "CRITICAL WARNING: Immediate data deletion risk" in plan.consequences_warning
    assert any("irreversible data loss" in step for step in plan.steps)


def test_pause_alternative_kept_distinct_from_cancellation():
    """Pause option is flagged as an alternative without changing the proposed action."""
    sub = _make_candidate_sub(pause_available=True)
    decision = _make_decision(sub.subscription_id, pause_alternative_available=True)

    plan = create_cancellation_plan(subscription=sub, decision=decision)

    assert plan.pause_alternative_available is True
    # Action remains cancellation
    assert any("Cancellation of streaming subscription" in step for step in plan.steps)
    # Pause is documented as alternative
    assert any("A paused subscription is NOT cancelled" in step for step in plan.steps)


# ==============================================================================
# Plan Versioning & Invalidation
# ==============================================================================

def test_plan_versioning_and_material_change_detection():
    """Verify material changes in subscription or terms alter the fingerprint and version."""
    sub_orig = _make_candidate_sub()
    dec_orig = _make_decision(sub_orig.subscription_id)

    plan_orig = create_cancellation_plan(subscription=sub_orig, decision=dec_orig)
    assert has_material_changes(plan_orig, sub_orig, dec_orig) is False

    # Simulate material change: data loss warning turns on
    sub_modified = _make_candidate_sub(data_deletion=True)
    dec_modified = _make_decision(sub_orig.subscription_id, data_deletion_risk=True)

    assert has_material_changes(plan_orig, sub_modified, dec_modified) is True

    plan_new = create_cancellation_plan(subscription=sub_modified, decision=dec_modified)
    assert plan_new.plan_version != plan_orig.plan_version


# ==============================================================================
# Multiple Subscriptions Independence & Purity
# ==============================================================================

def test_multiple_subscriptions_plan_independence():
    """Plans for different subscriptions are isolated with unique IDs and steps."""
    sub_1 = _make_candidate_sub(sub_id="sub_srv_01", service_name="StreamAlpha")
    sub_2 = _make_candidate_sub(sub_id="sub_srv_02", service_name="StreamBeta")

    dec_1 = _make_decision(sub_1.subscription_id)
    dec_2 = _make_decision(sub_2.subscription_id)

    plan_1 = create_cancellation_plan(sub_1, dec_1)
    plan_2 = create_cancellation_plan(sub_2, dec_2)

    assert plan_1.plan_id != plan_2.plan_id
    assert plan_1.subscription_id == "sub_srv_01"
    assert plan_2.subscription_id == "sub_srv_02"
    assert plan_1.service_name == "StreamAlpha"
    assert plan_2.service_name == "StreamBeta"


def test_pure_function_never_executes_cancellation():
    """Generating a plan must never alter subscription status or execute actions."""
    sub = _make_candidate_sub()
    decision = _make_decision(sub.subscription_id)

    plan = create_cancellation_plan(sub, decision)

    # Subscription state is unchanged
    assert sub.status == SubscriptionStatus.ACTIVE
    assert plan.status == ApprovalStatus.AWAITING_APPROVAL


# ==============================================================================
# Integration with Synthetic Fixtures (TC1, TC2, TC3)
# ==============================================================================

def _load_fixtures():
    fixtures_path = Path(__file__).resolve().parents[2] / "fixtures" / "subscriptions.json"
    with fixtures_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def test_tc1_fixture_plan():
    """TC1: Standard inactive -> Plan generated with standard terms."""
    fixtures = _load_fixtures()
    as_of = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)
    sub = Subscription.model_validate(
        next(s for s in fixtures["subscriptions"] if s["subscription_id"] == "sub_tc1_cinevault")
    )
    ev_list = [Evidence.model_validate(e) for e in fixtures["scenarios"]["TC1"]["evidence"]]

    decision = evaluate_eligibility(sub, evidence_list=ev_list, as_of=as_of)
    plan = create_cancellation_plan(sub, decision, evidence_list=ev_list)

    assert plan.subscription_id == "sub_tc1_cinevault"
    assert plan.service_name == "CineVault Mock Streaming"
    assert plan.status == ApprovalStatus.AWAITING_APPROVAL
    assert plan.pause_alternative_available is False
    assert is_review_required(plan) is False


def test_tc2_fixture_plan_with_data_deletion_warning():
    """TC2: Data deletion warning -> Disclosed in plan."""
    fixtures = _load_fixtures()
    as_of = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)
    sub = Subscription.model_validate(
        next(s for s in fixtures["subscriptions"] if s["subscription_id"] == "sub_tc2_audiocloud")
    )
    ev_list = [Evidence.model_validate(e) for e in fixtures["scenarios"]["TC2"]["evidence"]]

    decision = evaluate_eligibility(sub, evidence_list=ev_list, as_of=as_of)
    plan = create_cancellation_plan(sub, decision, evidence_list=ev_list)

    assert plan.subscription_id == "sub_tc2_audiocloud"
    assert plan.consequences_warning is not None
    assert "data deletion risk" in plan.consequences_warning.lower()


def test_tc3_fixture_plan_with_pause_alternative():
    """TC3: Pause option available -> Disclosed as alternative in plan."""
    fixtures = _load_fixtures()
    as_of = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)
    sub = Subscription.model_validate(
        next(s for s in fixtures["subscriptions"] if s["subscription_id"] == "sub_tc3_fitstream")
    )
    ev_list = [Evidence.model_validate(e) for e in fixtures["scenarios"]["TC3"]["evidence"]]

    decision = evaluate_eligibility(sub, evidence_list=ev_list, as_of=as_of)
    plan = create_cancellation_plan(sub, decision, evidence_list=ev_list)

    assert plan.pause_alternative_available is True
    assert any("pause" in step.lower() for step in plan.steps)
