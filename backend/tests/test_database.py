import json
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from backend.app.agent import initialize_orchestration_run, load_fixture_data
from backend.app.database import get_db_session, list_audit_events, load_run_snapshots
from backend.app.main import app


def test_run_persists_and_restores_from_sqlite(tmp_path, monkeypatch):
    database_path = tmp_path / "subcan.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database_path}")
    subscriptions, evidence_map = load_fixture_data(["sub_tc1_cinevault"])
    as_of = datetime(2026, 10, 23, 8, 0, tzinfo=timezone.utc)

    run, context = initialize_orchestration_run(
        subscriptions=subscriptions,
        evidence_map=evidence_map,
        run_id="run_persistence_test",
        as_of=as_of,
    )

    snapshots = load_run_snapshots()
    assert [(saved_run.run_id, saved_context.keys()) for saved_run, saved_context in snapshots] == [
        ("run_persistence_test", context.keys())
    ]
    with get_db_session() as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {"runs", "subscriptions", "run_subscriptions", "audit_events"} <= tables
        stored = connection.execute(
            "SELECT payload_json FROM run_subscriptions WHERE run_id = ?",
            (run.run_id,),
        ).fetchone()
        assert json.loads(stored["payload_json"])["subscription"]["subscription_id"] == "sub_tc1_cinevault"

    assert list_audit_events(run.run_id)


def test_dashboard_route_and_subscription_data_are_available():
    client = TestClient(app)
    page = client.get("/dashboard/")
    subscriptions = client.get("/subscriptions")

    assert page.status_code == 200
    assert "Subscription review" in page.text
    assert subscriptions.status_code == 200
    assert {item["subscription_id"] for item in subscriptions.json()} >= {
        "sub_tc1_cinevault",
        "sub_tc2_audiocloud",
    }


def test_action_records_and_idempotency_survive_restart(tmp_path, monkeypatch):
    """Test that actions and idempotency records survive application process restarts."""
    from backend.app.actions import (
        ActionRegistry,
        IdempotencyConflictError,
        execute_plan,
    )
    from backend.app.models import Approval, ApprovalStatus

    database_path = tmp_path / "subcan_idemp.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database_path}")

    subscriptions, evidence_map = load_fixture_data(["sub_tc1_cinevault"])
    as_of = datetime(2026, 10, 23, 8, 0, tzinfo=timezone.utc)
    _, context = initialize_orchestration_run(
        subscriptions=subscriptions,
        evidence_map=evidence_map,
        as_of=as_of,
    )
    tc1_state = context["sub_tc1_cinevault"]
    plan = tc1_state.plan

    approval = Approval(
        approval_id="appr_persist_01",
        plan_id=plan.plan_id,
        subscription_id=plan.subscription_id,
        service_name=plan.service_name,
        plan_version=plan.plan_version,
        status=ApprovalStatus.APPROVED,
        decided_at=as_of,
    )

    # 1. First process executes with idempotency key
    reg1 = ActionRegistry(restore_persisted=False)
    action1 = execute_plan(
        plan=plan,
        approval=approval,
        idempotency_key="idemp_key_persisted_test",
        decision=tc1_state.decision,
        registry=reg1,
        as_of=as_of,
    )
    assert action1.status.value == "succeeded"

    # 2. Simulate process restart by instantiating fresh registry restoring from SQLite
    reg2 = ActionRegistry(restore_persisted=True)
    assert reg2.get(action1.action_id) is not None
    assert reg2.get_by_idempotency_key("idemp_key_persisted_test") is not None

    # 3. Repeated request returns identical action result without executing duplicate action
    replayed_action = execute_plan(
        plan=plan,
        approval=approval,
        idempotency_key="idemp_key_persisted_test",
        decision=tc1_state.decision,
        registry=reg2,
        as_of=as_of,
    )
    assert replayed_action.action_id == action1.action_id

    # 4. Reusing key for a different plan raises IdempotencyConflictError even after restart
    different_plan = plan.model_copy(update={"plan_id": "plan_different_999"})
    import pytest
    with pytest.raises(IdempotencyConflictError):
        execute_plan(
            plan=different_plan,
            approval=approval,
            idempotency_key="idemp_key_persisted_test",
            registry=reg2,
            as_of=as_of,
        )


def test_test_results_persistence(tmp_path, monkeypatch):
    """Test saving and listing test results in the database."""
    from backend.app.database import list_test_results, save_test_result

    database_path = tmp_path / "subcan_test_results.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database_path}")

    save_test_result(
        test_id="TC1_CINEVAULT",
        scenario_id="TC1",
        status="PASSED",
        expected="Verified Cancelled with renewal disabled",
        actual="Verified Cancelled with renewal disabled",
        evidence_summary="Playback log > 130 days, provider billing terminated",
        details="Standard cancellation completed and independently verified",
    )

    results = list_test_results()
    assert len(results) == 1
    assert results[0]["test_id"] == "TC1_CINEVAULT"
    assert results[0]["status"] == "PASSED"
    assert results[0]["scenario_id"] == "TC1"
