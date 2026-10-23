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
