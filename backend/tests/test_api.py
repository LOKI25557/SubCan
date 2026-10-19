"""Unit and integration tests for Phase 7 FastAPI Endpoints (backend/app/main.py).

Verifies the 14 critical requirements:
1. Health endpoint still works.
2. Creating a run returns a valid response and does not execute cancellation.
3. Retrieving an unknown run returns the appropriate error.
4. Run subscription details are returned correctly.
5. A plan cannot be approved without explicit valid approval data.
6. Valid approval affects only its exact subscription and plan.
7. Declining a plan never executes cancellation.
8. Invalid or stale plan approval is rejected.
9. Unknown plan and action IDs return appropriate errors.
10. Verification endpoint does not execute cancellation.
11. Unverified or inconclusive cancellation is not reported as success.
12. Report endpoint returns consistent run results.
13. Repeated requests do not create duplicate actions.
14. Complete suite passes without regressions.
"""

from datetime import date, datetime, timedelta, timezone
from typing import Dict, List
from fastapi.testclient import TestClient
import pytest

from backend.app.actions import clear_action_registry, get_action_registry
from backend.app.agent import clear_agent_run_registry, get_agent_run_registry
from backend.app.main import app
from backend.app.models import (
    ActionStatus,
    ApprovalStatus,
    FinalResultStatus,
    Subscription,
    SubscriptionStatus,
    VerificationStatus,
)


# ==============================================================================
# Fixtures & Test Setup
# ==============================================================================

@pytest.fixture(autouse=True)
def clean_registries():
    """Ensure clean registries before and after each test."""
    clear_action_registry()
    clear_agent_run_registry()
    yield
    clear_action_registry()
    clear_agent_run_registry()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def base_time() -> datetime:
    return datetime(2026, 10, 19, 10, 0, 0, tzinfo=timezone.utc)


# ==============================================================================
# Phase 7 API Endpoint Tests
# ==============================================================================

def test_1_health_endpoint_still_works(client: TestClient):
    """1. Verify GET /health still returns 200 OK with proper system metadata."""
    resp = client.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "healthy"
    assert data["app_title"] == "SubCan: An AI-Powered Subscription Cancellation Agent"
    assert data["version"] == "0.1.0"
    assert "Phase 0" in data["phase"]
    assert "timestamp" in data

    # OpenAPI docs accessible
    docs_resp = client.get("/docs")
    assert docs_resp.status_code == 200


def test_2_creating_run_returns_valid_response_and_does_not_execute(
    client: TestClient,
    base_time: datetime,
):
    """2. Creating a run returns 201 Created, evaluates subscriptions, and NEVER executes cancellation."""
    payload = {
        "subscription_ids": ["sub_tc1_cinevault"],
        "rule_version": "1.0.0",
        "dry_run": False,
        "as_of": base_time.isoformat(),
    }
    resp = client.post("/runs", json=payload)
    assert resp.status_code == 201

    data = resp.json()
    assert "run_id" in data
    assert data["status"] == "in_progress"
    assert data["subscription_ids"] == ["sub_tc1_cinevault"]
    assert data["final_results"]["sub_tc1_cinevault"] == "pending"

    # CRITICAL INVARIANT: Action registry has NO recorded actions
    assert len(get_action_registry()._actions_by_id) == 0


def test_3_retrieving_unknown_run_returns_404_and_invalid_inputs_return_400(
    client: TestClient,
):
    """3. Retrieving an unknown run returns 404 with RFC error envelope; invalid inputs return 400."""
    resp = client.get("/runs/run_unknown_99999")
    assert resp.status_code == 404
    err = resp.json()
    assert "error" in err
    assert err["error"]["code"] == "RESOURCE_NOT_FOUND"
    assert "not found" in err["error"]["message"].lower()

    # Empty subscription_ids returns 400
    resp_empty = client.post("/runs", json={"subscription_ids": []})
    assert resp_empty.status_code == 400
    assert resp_empty.json()["error"]["code"] == "BAD_REQUEST"

    # Unknown subscription_ids returns 400
    resp_unknown_sub = client.post("/runs", json={"subscription_ids": ["sub_does_not_exist"]})
    assert resp_unknown_sub.status_code == 400
    assert resp_unknown_sub.json()["error"]["code"] == "BAD_REQUEST"


def test_4_run_subscription_details_returned_correctly(
    client: TestClient,
    base_time: datetime,
):
    """4. GET /runs/{run_id}/subscriptions returns detailed subscription context and plans."""
    create_resp = client.post(
        "/runs",
        json={"subscription_ids": ["sub_tc1_cinevault"], "as_of": base_time.isoformat()},
    )
    run_id = create_resp.json()["run_id"]

    sub_resp = client.get(f"/runs/{run_id}/subscriptions")
    assert sub_resp.status_code == 200
    data = sub_resp.json()
    assert data["run_id"] == run_id
    assert len(data["items"]) == 1

    item = data["items"][0]
    assert item["subscription"]["subscription_id"] == "sub_tc1_cinevault"
    assert item["decision"]["eligibility"] == "candidate"
    assert item["plan"]["status"] == "awaiting_approval"
    assert item["final_status"] == "pending"
    assert len(item["evidence_list"]) >= 1

    # Unknown run returns 404
    unknown_resp = client.get("/runs/run_ghost_123/subscriptions")
    assert unknown_resp.status_code == 404


def test_5_plan_cannot_be_approved_without_explicit_valid_approval(
    client: TestClient,
    base_time: datetime,
):
    """5. A plan cannot be approved without explicit valid approval data."""
    create_resp = client.post(
        "/runs",
        json={"subscription_ids": ["sub_tc1_cinevault"], "as_of": base_time.isoformat()},
    )
    run_id = create_resp.json()["run_id"]

    sub_resp = client.get(f"/runs/{run_id}/subscriptions")
    plan = sub_resp.json()["items"][0]["plan"]
    plan_id = plan["plan_id"]

    # 5a. Mismatched path plan_id vs body plan_id -> 400
    resp_mismatch_path = client.post(
        f"/plans/{plan_id}/approve",
        json={
            "plan_id": "different_plan_id",
            "subscription_id": "sub_tc1_cinevault",
            "service_name": "CineVault Mock Streaming",
            "plan_version": plan["plan_version"],
        },
    )
    assert resp_mismatch_path.status_code == 400

    # 5b. Wrong subscription ID in body -> 400
    resp_wrong_sub = client.post(
        f"/plans/{plan_id}/approve",
        json={
            "plan_id": plan_id,
            "subscription_id": "sub_wrong_sub_id",
            "service_name": "CineVault Mock Streaming",
            "plan_version": plan["plan_version"],
        },
    )
    assert resp_wrong_sub.status_code == 400

    # 5c. Wrong service name in body -> 400
    resp_wrong_svc = client.post(
        f"/plans/{plan_id}/approve",
        json={
            "plan_id": plan_id,
            "subscription_id": "sub_tc1_cinevault",
            "service_name": "CompletelyDifferentService",
            "plan_version": plan["plan_version"],
        },
    )
    assert resp_wrong_svc.status_code == 400

    # Verify no action was executed
    assert len(get_action_registry()._actions_by_id) == 0


def test_6_valid_approval_affects_only_exact_subscription(
    client: TestClient,
    base_time: datetime,
):
    """6. Approving one subscription affects only its exact plan; others remain untouched."""
    # TC4 has MovieMax and GameStream
    create_resp = client.post(
        "/runs",
        json={
            "subscription_ids": ["sub_tc4_moviemax", "sub_tc4_gamestream"],
            "as_of": base_time.isoformat(),
        },
    )
    run_id = create_resp.json()["run_id"]

    sub_resp = client.get(f"/runs/{run_id}/subscriptions")
    items = {item["subscription"]["subscription_id"]: item for item in sub_resp.json()["items"]}

    plan_mm = items["sub_tc4_moviemax"]["plan"]
    plan_gs = items["sub_tc4_gamestream"]["plan"]

    # Provide observed cancelled subscription for MovieMax
    observed_mm = {
        "subscription_id": "sub_tc4_moviemax",
        "service_name": "MovieMax Mock",
        "status": "cancelled",
        "renewal_date": None,
    }

    # Approve MovieMax
    approve_resp = client.post(
        f"/plans/{plan_mm['plan_id']}/approve",
        json={
            "plan_id": plan_mm["plan_id"],
            "subscription_id": "sub_tc4_moviemax",
            "service_name": "MovieMax Mock",
            "plan_version": plan_mm["plan_version"],
            "approved_by": "user_loki",
            "decided_at": base_time.isoformat(),
            "observed_subscription": observed_mm,
        },
    )
    assert approve_resp.status_code == 200
    assert approve_resp.json()["status"] == "approved"

    # Re-fetch subscriptions
    after_resp = client.get(f"/runs/{run_id}/subscriptions")
    after_items = {item["subscription"]["subscription_id"]: item for item in after_resp.json()["items"]}

    # MovieMax is verified cancelled
    assert after_items["sub_tc4_moviemax"]["final_status"] == "verified_cancelled"
    assert after_items["sub_tc4_moviemax"]["action_result"] is not None

    # CRITICAL INVARIANT: GameStream is completely untouched!
    assert after_items["sub_tc4_gamestream"]["final_status"] == "pending"
    assert after_items["sub_tc4_gamestream"]["approval"] is None
    assert after_items["sub_tc4_gamestream"]["action_result"] is None
    assert after_items["sub_tc4_gamestream"]["plan"]["status"] == "awaiting_approval"


def test_7_declining_plan_never_executes_cancellation(
    client: TestClient,
    base_time: datetime,
):
    """7. Declining a plan marks subscription KEPT and never executes cancellation."""
    create_resp = client.post(
        "/runs",
        json={"subscription_ids": ["sub_tc1_cinevault"], "as_of": base_time.isoformat()},
    )
    run_id = create_resp.json()["run_id"]

    sub_resp = client.get(f"/runs/{run_id}/subscriptions")
    plan = sub_resp.json()["items"][0]["plan"]

    decline_resp = client.post(
        f"/plans/{plan['plan_id']}/decline",
        json={
            "plan_id": plan["plan_id"],
            "plan_version": plan["plan_version"],
            "declined_by": "user_loki",
            "decided_at": base_time.isoformat(),
            "notes": "User opted to keep CineVault.",
        },
    )
    assert decline_resp.status_code == 200
    assert decline_resp.json()["status"] == "declined"

    # Verify run state is kept
    after_resp = client.get(f"/runs/{run_id}/subscriptions")
    state = after_resp.json()["items"][0]
    assert state["final_status"] == "kept"
    assert state["action_result"] is None
    assert len(get_action_registry()._actions_by_id) == 0


def test_8_invalid_or_stale_plan_approval_rejected(
    client: TestClient,
    base_time: datetime,
):
    """8. Invalid or stale plan approval is rejected (returns 400 Bad Request)."""
    create_resp = client.post(
        "/runs",
        json={"subscription_ids": ["sub_tc1_cinevault"], "as_of": base_time.isoformat()},
    )
    run_id = create_resp.json()["run_id"]

    sub_resp = client.get(f"/runs/{run_id}/subscriptions")
    plan = sub_resp.json()["items"][0]["plan"]

    # Stale plan version approval
    resp = client.post(
        f"/plans/{plan['plan_id']}/approve",
        json={
            "plan_id": plan["plan_id"],
            "subscription_id": "sub_tc1_cinevault",
            "service_name": "CineVault Mock Streaming",
            "plan_version": "v_stale_fingerprint_00000000",
            "approved_by": "user_loki",
        },
    )
    assert resp.status_code == 400
    assert "Plan version mismatch" in resp.json()["error"]["message"]
    assert len(get_action_registry()._actions_by_id) == 0


def test_9_unknown_plan_and_action_ids_return_404(client: TestClient):
    """9. Unknown plan and action IDs return 404 Not Found."""
    resp_approve = client.post(
        "/plans/plan_non_existent_123/approve",
        json={
            "plan_id": "plan_non_existent_123",
            "subscription_id": "sub_tc1_cinevault",
            "service_name": "CineVault",
            "plan_version": "v1.0",
        },
    )
    assert resp_approve.status_code == 404

    resp_decline = client.post(
        "/plans/plan_non_existent_123/decline",
        json={"plan_id": "plan_non_existent_123", "plan_version": "v1.0"},
    )
    assert resp_decline.status_code == 404

    resp_verify = client.post("/actions/act_non_existent_123/verify", json={})
    assert resp_verify.status_code == 404

    resp_report = client.get("/runs/run_non_existent_123/report")
    assert resp_report.status_code == 404


def test_10_verification_endpoint_does_not_execute_cancellation(
    client: TestClient,
    base_time: datetime,
):
    """10. POST /actions/{action_id}/verify independently verifies status and does NOT execute actions."""
    create_resp = client.post(
        "/runs",
        json={"subscription_ids": ["sub_tc1_cinevault"], "as_of": base_time.isoformat()},
    )
    run_id = create_resp.json()["run_id"]
    sub_resp = client.get(f"/runs/{run_id}/subscriptions")
    plan = sub_resp.json()["items"][0]["plan"]

    # Execute action via approval
    client.post(
        f"/plans/{plan['plan_id']}/approve",
        json={
            "plan_id": plan["plan_id"],
            "subscription_id": "sub_tc1_cinevault",
            "service_name": "CineVault Mock Streaming",
            "plan_version": plan["plan_version"],
            "decided_at": base_time.isoformat(),
        },
    )

    # Get the action_id
    detail_resp = client.get(f"/runs/{run_id}/subscriptions")
    action_id = detail_resp.json()["items"][0]["action_result"]["action_id"]
    actions_count_before = len(get_action_registry()._actions_by_id)

    # Call verify endpoint with independent observation
    observed_cancelled = {
        "subscription_id": "sub_tc1_cinevault",
        "service_name": "CineVault Mock Streaming",
        "status": "cancelled",
        "renewal_date": None,
    }
    verif_resp = client.post(
        f"/actions/{action_id}/verify",
        json={
            "observed_subscription": observed_cancelled,
            "as_of": (base_time + timedelta(seconds=2)).isoformat(),
        },
    )
    assert verif_resp.status_code == 200
    assert verif_resp.json()["status"] == "verified"

    # Action registry count has NOT increased
    assert len(get_action_registry()._actions_by_id) == actions_count_before


def test_11_unverified_or_inconclusive_cancellation_is_not_success(
    client: TestClient,
    base_time: datetime,
):
    """11. Actions followed by active renewal or pause evidence fail verification."""
    create_resp = client.post(
        "/runs",
        json={"subscription_ids": ["sub_tc1_cinevault"], "as_of": base_time.isoformat()},
    )
    run_id = create_resp.json()["run_id"]
    sub_resp = client.get(f"/runs/{run_id}/subscriptions")
    plan = sub_resp.json()["items"][0]["plan"]

    # Execute action
    client.post(
        f"/plans/{plan['plan_id']}/approve",
        json={
            "plan_id": plan["plan_id"],
            "subscription_id": "sub_tc1_cinevault",
            "service_name": "CineVault Mock Streaming",
            "plan_version": plan["plan_version"],
            "decided_at": base_time.isoformat(),
        },
    )

    action_id = client.get(f"/runs/{run_id}/subscriptions").json()["items"][0]["action_result"]["action_id"]

    # 11a. Observed subscription still active with renewal date -> FAILED verification
    observed_active = {
        "subscription_id": "sub_tc1_cinevault",
        "service_name": "CineVault Mock Streaming",
        "status": "active",
        "renewal_date": "2026-12-15",
    }
    verif_resp_active = client.post(
        f"/actions/{action_id}/verify",
        json={
            "observed_subscription": observed_active,
            "as_of": (base_time + timedelta(seconds=2)).isoformat(),
        },
    )
    assert verif_resp_active.status_code == 200
    assert verif_resp_active.json()["status"] == "failed"

    # 11b. Evidence showing pause instead of cancellation -> FAILED verification
    pause_evidence = [
        {
            "evidence_id": "ev_post_pause_01",
            "subscription_id": "sub_tc1_cinevault",
            "evidence_type": "renewal_status",
            "excerpt": "Account billing paused for 60 days.",
            "captured_at": (base_time + timedelta(seconds=5)).isoformat(),
            "confidence": 0.95,
        }
    ]
    verif_resp_pause = client.post(
        f"/actions/{action_id}/verify",
        json={
            "evidence_list": pause_evidence,
            "as_of": (base_time + timedelta(seconds=6)).isoformat(),
        },
    )
    assert verif_resp_pause.status_code == 200
    assert verif_resp_pause.json()["status"] == "failed"
    assert "pause" in verif_resp_pause.json()["details"].lower()


def test_12_report_endpoint_returns_consistent_run_results(
    client: TestClient,
    base_time: datetime,
):
    """12. GET /runs/{run_id}/report returns consistent audit and financial reconciliation."""
    create_resp = client.post(
        "/runs",
        json={"subscription_ids": ["sub_tc1_cinevault"], "as_of": base_time.isoformat()},
    )
    run_id = create_resp.json()["run_id"]
    sub_resp = client.get(f"/runs/{run_id}/subscriptions")
    plan = sub_resp.json()["items"][0]["plan"]

    # Approve and verify MovieMax/CineVault
    observed_cancelled = {
        "subscription_id": "sub_tc1_cinevault",
        "service_name": "CineVault Mock Streaming",
        "status": "cancelled",
        "renewal_date": None,
    }
    client.post(
        f"/plans/{plan['plan_id']}/approve",
        json={
            "plan_id": plan["plan_id"],
            "subscription_id": "sub_tc1_cinevault",
            "service_name": "CineVault Mock Streaming",
            "plan_version": plan["plan_version"],
            "observed_subscription": observed_cancelled,
            "decided_at": base_time.isoformat(),
        },
    )

    report_resp = client.get(f"/runs/{run_id}/report")
    assert report_resp.status_code == 200
    data = report_resp.json()

    assert data["run_id"] == run_id
    assert data["total_evaluated"] == 1
    assert data["total_cancelled_verified"] == 1
    assert data["total_kept"] == 0
    assert data["total_monthly_savings_usd"] == 14.99
    assert len(data["items"]) == 1

    item = data["items"][0]
    assert item["subscription_id"] == "sub_tc1_cinevault"
    assert item["service_name"] == "CineVault Mock Streaming"
    assert item["final_status"] == "verified_cancelled"
    assert item["approval_id"] is not None
    assert item["action_id"] is not None
    assert item["verification_id"] is not None


def test_13_repeated_requests_respect_idempotency_behavior(
    client: TestClient,
    base_time: datetime,
):
    """13. Replaying an identical approval request with idempotency_key does not duplicate actions."""
    create_resp = client.post(
        "/runs",
        json={"subscription_ids": ["sub_tc1_cinevault"], "as_of": base_time.isoformat()},
    )
    run_id = create_resp.json()["run_id"]
    sub_resp = client.get(f"/runs/{run_id}/subscriptions")
    plan = sub_resp.json()["items"][0]["plan"]

    idemp_key = "idemp_api_test_001"
    approval_payload = {
        "plan_id": plan["plan_id"],
        "subscription_id": "sub_tc1_cinevault",
        "service_name": "CineVault Mock Streaming",
        "plan_version": plan["plan_version"],
        "idempotency_key": idemp_key,
        "decided_at": base_time.isoformat(),
    }

    # First approval
    resp1 = client.post(f"/plans/{plan['plan_id']}/approve", json=approval_payload)
    assert resp1.status_code == 200

    # Second approval with identical idempotency key and payload
    resp2 = client.post(f"/plans/{plan['plan_id']}/approve", json=approval_payload)
    assert resp2.status_code == 200

    # Exactly one action in registry
    assert len(get_action_registry()._actions_by_id) == 1
