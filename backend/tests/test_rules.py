"""Unit tests for Phase 1 Eligibility Engine (backend/app/rules.py).

Verifies:
- 3 CALENDAR MONTHS calculation vs 90-day intervals.
- Month length clamping, leap years, and year boundary transitions.
- Exact threshold boundaries (exact second, before, after).
- Missing, future-dated, and contradictory usage evidence handling.
- Consequence disclosures (data loss warnings, pause alternatives).
- All 4 synthetic scenarios from fixtures/subscriptions.json.
"""

from datetime import date, datetime, timezone
import json
from pathlib import Path
import pytest

from backend.app.models import (
    EligibilityStatus,
    Evidence,
    Subscription,
    SubscriptionStatus,
)
from backend.app.rules import (
    calculate_calendar_months_threshold,
    evaluate_eligibility,
    RULE_VERSION,
)


# ==============================================================================
# Calendar Month Arithmetic & Boundary Tests
# ==============================================================================

def test_calendar_months_standard_subtraction():
    """Verify standard 3-calendar-month calculation preserving timezone and time."""
    as_of = datetime(2026, 10, 19, 15, 30, 0, tzinfo=timezone.utc)
    threshold = calculate_calendar_months_threshold(as_of, months=3)
    assert threshold == datetime(2026, 7, 19, 15, 30, 0, tzinfo=timezone.utc)


def test_calendar_months_year_boundary_transition():
    """Verify year boundary transitions (e.g. February minus 3 months -> November prior year)."""
    as_of = datetime(2026, 2, 15, 10, 0, 0, tzinfo=timezone.utc)
    threshold = calculate_calendar_months_threshold(as_of, months=3)
    assert threshold == datetime(2025, 11, 15, 10, 0, 0, tzinfo=timezone.utc)


def test_calendar_months_leap_year_february_29():
    """Verify leap year handling: May 31, 2024 minus 3 months clamps to February 29 (leap year)."""
    as_of = datetime(2024, 5, 31, 12, 0, 0, tzinfo=timezone.utc)
    threshold = calculate_calendar_months_threshold(as_of, months=3)
    assert threshold == datetime(2024, 2, 29, 12, 0, 0, tzinfo=timezone.utc)


def test_calendar_months_common_year_february_28():
    """Verify common year handling: May 31, 2025 minus 3 months clamps to February 28 (common year)."""
    as_of = datetime(2025, 5, 31, 12, 0, 0, tzinfo=timezone.utc)
    threshold = calculate_calendar_months_threshold(as_of, months=3)
    assert threshold == datetime(2025, 2, 28, 12, 0, 0, tzinfo=timezone.utc)


def test_calendar_months_31_to_30_day_clamping():
    """Verify 31-day to 30-day month clamping: July 31 minus 3 months clamps to April 30."""
    as_of = datetime(2026, 7, 31, 9, 0, 0, tzinfo=timezone.utc)
    threshold = calculate_calendar_months_threshold(as_of, months=3)
    assert threshold == datetime(2026, 4, 30, 9, 0, 0, tzinfo=timezone.utc)


def test_naive_timestamp_rejected_by_threshold_calculator():
    """Verify that a timezone-naive datetime raises ValueError."""
    naive_dt = datetime(2026, 10, 19, 12, 0, 0)
    with pytest.raises(ValueError, match="must be timezone-aware"):
        calculate_calendar_months_threshold(naive_dt, months=3)


def test_calendar_months_vs_hardcoded_90_days_difference():
    """Demonstrate why 3 calendar months is strictly different from a hardcoded 90-day interval.

    From July 19, 2026 to October 19, 2026 is 92 days.
    An activity on July 20, 2026 is 91 days prior to October 19.
    - Under a 90-day rule, 91 days would be incorrectly marked as inactive (CANDIDATE).
    - Under the 3-calendar-month rule, July 20 is INSIDE the 3-month window (July 19 to Oct 19),
      so it is correctly recognized as active (NOT_ELIGIBLE).
    """
    as_of = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)
    # 91 days prior is 2026-07-20 12:00:00Z
    last_used = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)

    sub = Subscription(
        subscription_id="sub_calendar_vs_90",
        service_name="TestStream",
        status=SubscriptionStatus.ACTIVE,
        last_used_at=last_used,
    )

    decision = evaluate_eligibility(sub, as_of=as_of)
    assert decision.eligibility == EligibilityStatus.NOT_ELIGIBLE
    assert "Active within the last three calendar months" in decision.reason


# ==============================================================================
# Threshold Boundary Precision Tests
# ==============================================================================

def test_exact_threshold_boundary_is_candidate():
    """Verify exact boundary timestamp (threshold = last_used_at) is marked CANDIDATE."""
    as_of = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)
    exact_threshold = datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc)

    sub = Subscription(
        subscription_id="sub_exact_boundary",
        service_name="BoundaryStream",
        status=SubscriptionStatus.ACTIVE,
        last_used_at=exact_threshold,
    )

    decision = evaluate_eligibility(sub, as_of=as_of)
    assert decision.eligibility == EligibilityStatus.CANDIDATE
    assert decision.rule_version == RULE_VERSION


def test_one_second_after_threshold_is_not_eligible():
    """Verify 1 second more recent than the threshold (last_used > threshold) is NOT_ELIGIBLE."""
    as_of = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)
    one_second_recent = datetime(2026, 7, 19, 12, 0, 1, tzinfo=timezone.utc)

    sub = Subscription(
        subscription_id="sub_one_sec_recent",
        service_name="BoundaryStream",
        status=SubscriptionStatus.ACTIVE,
        last_used_at=one_second_recent,
    )

    decision = evaluate_eligibility(sub, as_of=as_of)
    assert decision.eligibility == EligibilityStatus.NOT_ELIGIBLE


def test_one_second_before_threshold_is_candidate():
    """Verify 1 second older than the threshold (last_used < threshold) is CANDIDATE."""
    as_of = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)
    one_second_older = datetime(2026, 7, 19, 11, 59, 59, tzinfo=timezone.utc)

    sub = Subscription(
        subscription_id="sub_one_sec_older",
        service_name="BoundaryStream",
        status=SubscriptionStatus.ACTIVE,
        last_used_at=one_second_older,
    )

    decision = evaluate_eligibility(sub, as_of=as_of)
    assert decision.eligibility == EligibilityStatus.CANDIDATE


# ==============================================================================
# Edge Cases: Missing, Future-Dated, Stale & Contradictory Evidence
# ==============================================================================

def test_missing_usage_history_requires_review_never_invents_date():
    """Missing usage history must be treated as NEEDS_REVIEW with uncertainty 1.0."""
    as_of = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)
    sub = Subscription(
        subscription_id="sub_missing_usage",
        service_name="UnknownActivityStream",
        status=SubscriptionStatus.ACTIVE,
        last_used_at=None,
    )

    decision = evaluate_eligibility(sub, as_of=as_of)
    assert decision.eligibility == EligibilityStatus.NEEDS_REVIEW
    assert decision.uncertainty == 1.0
    assert "Missing usage history" in decision.reason


def test_future_dated_subscription_usage_requires_review():
    """A subscription with last_used_at in the future relative to as_of requires review."""
    as_of = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)
    future_date = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)

    sub = Subscription(
        subscription_id="sub_future_used",
        service_name="TimeTravelStream",
        status=SubscriptionStatus.ACTIVE,
        last_used_at=future_date,
    )

    decision = evaluate_eligibility(sub, as_of=as_of)
    assert decision.eligibility == EligibilityStatus.NEEDS_REVIEW
    assert decision.uncertainty == 1.0
    assert "Future-dated usage detected" in decision.reason


def test_future_dated_evidence_requires_review():
    """Evidence reporting activity in the future relative to as_of requires review."""
    as_of = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)
    sub = Subscription(
        subscription_id="sub_test_evidence",
        service_name="Stream",
        status=SubscriptionStatus.ACTIVE,
        last_used_at=datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc),
    )
    future_ev = Evidence(
        evidence_id="ev_future",
        subscription_id=sub.subscription_id,
        evidence_type="usage_history",
        excerpt="Future activity logged on 2026-10-25T12:00:00Z",
        captured_at=as_of,
        confidence=0.9,
    )

    decision = evaluate_eligibility(sub, evidence_list=[future_ev], as_of=as_of)
    assert decision.eligibility == EligibilityStatus.NEEDS_REVIEW
    assert decision.uncertainty == 1.0
    assert "Future-dated evidence detected" in decision.reason


def test_contradictory_evidence_between_sub_and_evidence():
    """Subscription record says inactive (May 2026), but high-confidence evidence claims recent activity."""
    as_of = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)
    sub = Subscription(
        subscription_id="sub_contradict",
        service_name="Stream",
        status=SubscriptionStatus.ACTIVE,
        last_used_at=datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc),  # > 5 months ago
    )
    conflicting_ev = Evidence(
        evidence_id="ev_recent_stream",
        subscription_id=sub.subscription_id,
        evidence_type="usage_history",
        excerpt="User watched movie on 2026-10-10T20:00:00Z from mobile app.",
        captured_at=as_of,
        confidence=0.85,
    )

    decision = evaluate_eligibility(sub, evidence_list=[conflicting_ev], as_of=as_of)
    assert decision.eligibility == EligibilityStatus.NEEDS_REVIEW
    assert decision.uncertainty >= 0.8
    assert "Contradictory usage evidence" in decision.reason


def test_already_cancelled_subscription_is_not_eligible():
    """Subscription already cancelled is NOT_ELIGIBLE with uncertainty 0.0."""
    as_of = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)
    sub = Subscription(
        subscription_id="sub_cancelled",
        service_name="OldStream",
        status=SubscriptionStatus.CANCELLED,
        last_used_at=datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
    )

    decision = evaluate_eligibility(sub, as_of=as_of)
    assert decision.eligibility == EligibilityStatus.NOT_ELIGIBLE
    assert decision.uncertainty == 0.0
    assert "already recorded as cancelled" in decision.reason


def test_unknown_status_subscription_requires_review():
    """Subscription with status UNKNOWN requires review to verify billing."""
    as_of = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)
    sub = Subscription(
        subscription_id="sub_unknown_status",
        service_name="MysteryStream",
        status=SubscriptionStatus.UNKNOWN,
        last_used_at=datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc),
    )

    decision = evaluate_eligibility(sub, as_of=as_of)
    assert decision.eligibility == EligibilityStatus.NEEDS_REVIEW
    assert "status is unknown" in decision.reason


# ==============================================================================
# Consequence Detection Tests
# ==============================================================================

def test_data_loss_warning_detected():
    """Verify data deletion warning is identified and included in consequences."""
    as_of = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)
    sub = Subscription(
        subscription_id="sub_data_loss",
        service_name="CloudAudio",
        status=SubscriptionStatus.ACTIVE,
        last_used_at=datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc),
        data_deletion_warning=True,
    )

    decision = evaluate_eligibility(sub, as_of=as_of)
    assert decision.data_deletion_risk is True
    assert any("data deletion" in c.lower() for c in decision.consequences)


def test_pause_alternative_detected():
    """Verify pause option availability is identified and included in consequences."""
    as_of = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)
    sub = Subscription(
        subscription_id="sub_pause",
        service_name="GymStream",
        status=SubscriptionStatus.ACTIVE,
        last_used_at=datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc),
        pause_option_available=True,
    )

    decision = evaluate_eligibility(sub, as_of=as_of)
    assert decision.pause_alternative_available is True
    assert any("pause" in c.lower() for c in decision.consequences)


# ==============================================================================
# Synthetic Fixtures Scenario Tests (TC1, TC2, TC3, TC4)
# ==============================================================================

def _load_fixtures():
    fixtures_path = Path(__file__).resolve().parents[2] / "fixtures" / "subscriptions.json"
    with fixtures_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def test_tc1_fixture_rule_evaluation():
    """TC1: Standard inactive subscription > 3 calendar months -> CANDIDATE."""
    fixtures = _load_fixtures()
    as_of = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)

    sub_data = next(s for s in fixtures["subscriptions"] if s["subscription_id"] == "sub_tc1_cinevault")
    sub = Subscription.model_validate(sub_data)

    ev_list = [
        Evidence.model_validate(e)
        for e in fixtures["scenarios"]["TC1"]["evidence"]
    ]

    decision = evaluate_eligibility(sub, evidence_list=ev_list, as_of=as_of)
    assert decision.eligibility == EligibilityStatus.CANDIDATE
    assert decision.data_deletion_risk is False
    assert decision.pause_alternative_available is False
    assert decision.uncertainty <= 0.1


def test_tc2_fixture_rule_evaluation():
    """TC2: Inactive with immediate data deletion risk -> CANDIDATE with data_deletion_risk=True."""
    fixtures = _load_fixtures()
    as_of = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)

    sub_data = next(s for s in fixtures["subscriptions"] if s["subscription_id"] == "sub_tc2_audiocloud")
    sub = Subscription.model_validate(sub_data)

    ev_list = [
        Evidence.model_validate(e)
        for e in fixtures["scenarios"]["TC2"]["evidence"]
    ]

    decision = evaluate_eligibility(sub, evidence_list=ev_list, as_of=as_of)
    assert decision.eligibility == EligibilityStatus.CANDIDATE
    assert decision.data_deletion_risk is True
    assert any("data deletion" in c.lower() for c in decision.consequences)


def test_tc3_fixture_rule_evaluation():
    """TC3: Inactive with pause option -> CANDIDATE with pause_alternative_available=True."""
    fixtures = _load_fixtures()
    as_of = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)

    sub_data = next(s for s in fixtures["subscriptions"] if s["subscription_id"] == "sub_tc3_fitstream")
    sub = Subscription.model_validate(sub_data)

    ev_list = [
        Evidence.model_validate(e)
        for e in fixtures["scenarios"]["TC3"]["evidence"]
    ]

    decision = evaluate_eligibility(sub, evidence_list=ev_list, as_of=as_of)
    assert decision.eligibility == EligibilityStatus.CANDIDATE
    assert decision.pause_alternative_available is True
    assert any("pause" in c.lower() for c in decision.consequences)


def test_tc4_fixture_isolated_evaluation():
    """TC4: Multiple subscriptions evaluated independently without cross-talk."""
    fixtures = _load_fixtures()
    as_of = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)

    sub_mm = Subscription.model_validate(
        next(s for s in fixtures["subscriptions"] if s["subscription_id"] == "sub_tc4_moviemax")
    )
    sub_gs = Subscription.model_validate(
        next(s for s in fixtures["subscriptions"] if s["subscription_id"] == "sub_tc4_gamestream")
    )

    ev_mm = [
        Evidence.model_validate(e)
        for e in fixtures["scenarios"]["TC4"]["evidence"]
        if e["subscription_id"] == "sub_tc4_moviemax"
    ]
    ev_gs = [
        Evidence.model_validate(e)
        for e in fixtures["scenarios"]["TC4"]["evidence"]
        if e["subscription_id"] == "sub_tc4_gamestream"
    ]

    dec_mm = evaluate_eligibility(sub_mm, evidence_list=ev_mm, as_of=as_of)
    dec_gs = evaluate_eligibility(sub_gs, evidence_list=ev_gs, as_of=as_of)

    assert dec_mm.subscription_id == "sub_tc4_moviemax"
    assert dec_mm.eligibility == EligibilityStatus.CANDIDATE
    assert dec_gs.subscription_id == "sub_tc4_gamestream"
    assert dec_gs.eligibility == EligibilityStatus.CANDIDATE
