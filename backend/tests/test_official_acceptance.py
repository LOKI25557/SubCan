"""Unit & Acceptance test file verifying the official SubCan test scenarios.

Official Scenarios Tested:
- TC1: Approved cancellation (verified cancelled with independent renewal check)
- TC2: Cancellation deletes data immediately (consequences disclosure & decline keeps subscription)
- TC3: Service offers pause alternative (pause recognized; paused is NEVER reported cancelled)
- TC4: Multi-service isolation (approving one subscription strictly isolates all others)
And Edge Cases:
- Missing usage history fails closed to needs_review
- Calendar month boundary leap-year arithmetic
- Contradictory evidence triggers review
- Stale plan version rejection
- Recently used accounts (< 3 months) ineligible
"""

import pytest
from backend.app.acceptance import run_official_test_suite


def test_official_acceptance_scenarios_all_pass():
    """Execute the full official acceptance suite and assert all pass."""
    results = run_official_test_suite()
    assert len(results) >= 9

    failures = [r for r in results if r.status != "PASSED"]
    failure_messages = [f"{r.test_id}: {r.details} (Actual: {r.actual_result})" for r in failures]
    assert not failures, f"Official acceptance scenarios failed: {failure_messages}"

    # Specifically assert all 4 core TCs
    test_ids = {r.test_id: r for r in results}
    assert "TC1" in test_ids and test_ids["TC1"].status == "PASSED"
    assert "TC2" in test_ids and test_ids["TC2"].status == "PASSED"
    assert "TC3" in test_ids and test_ids["TC3"].status == "PASSED"
    assert "TC4" in test_ids and test_ids["TC4"].status == "PASSED"
