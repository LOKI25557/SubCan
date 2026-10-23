"""Durable SQLite persistence for orchestration runs and their audit history.

The workflow models remain Pydantic objects in the orchestration layer. This
module owns the durable boundary and stores both indexed lifecycle fields and
validated JSON snapshots so a run can be restored after a process restart.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
from typing import Dict, Iterator, Optional, TYPE_CHECKING

from backend.app.models import AgentRun, Subscription

if TYPE_CHECKING:
    from backend.app.agent import SubscriptionRunState


def _database_path() -> Path:
    configured = os.getenv("DATABASE_URL", "sqlite:///./reports/subcan.db")
    if not configured.startswith("sqlite:///"):
        raise ValueError("DATABASE_URL must use the sqlite:/// URL format.")
    raw_path = configured.removeprefix("sqlite:///")
    path = Path(raw_path)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[2] / path
    return path


@contextmanager
def get_db_session() -> Iterator[sqlite3.Connection]:
    """Yield an initialized SQLite connection with foreign keys enabled."""
    path = _database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        initialize_schema(connection)
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize_schema(connection: Optional[sqlite3.Connection] = None) -> None:
    """Create the durable schema without deleting existing data."""
    owns_connection = connection is None
    if owns_connection:
        path = _database_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
    assert connection is not None
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS subscriptions (
            subscription_id TEXT PRIMARY KEY,
            service_name TEXT NOT NULL,
            status TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS run_subscriptions (
            run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
            subscription_id TEXT NOT NULL REFERENCES subscriptions(subscription_id),
            final_status TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            PRIMARY KEY (run_id, subscription_id)
        );
        CREATE TABLE IF NOT EXISTS audit_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
            subscription_id TEXT NOT NULL REFERENCES subscriptions(subscription_id),
            event_type TEXT NOT NULL,
            entity_id TEXT,
            event_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            UNIQUE (run_id, subscription_id, event_type, entity_id, event_at)
        );
        CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
        CREATE INDEX IF NOT EXISTS idx_run_subscriptions_status
            ON run_subscriptions(final_status);
        CREATE INDEX IF NOT EXISTS idx_audit_events_subscription
            ON audit_events(subscription_id, event_at);
        """
    )
    if owns_connection:
        connection.commit()
        connection.close()


def persist_run(run: AgentRun, contexts: Dict[str, "SubscriptionRunState"]) -> None:
    """Upsert a complete run snapshot and append lifecycle audit events."""
    with get_db_session() as connection:
        connection.execute(
            """
            INSERT INTO runs(run_id, status, started_at, completed_at, payload_json, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(run_id) DO UPDATE SET
                status=excluded.status,
                started_at=excluded.started_at,
                completed_at=excluded.completed_at,
                payload_json=excluded.payload_json,
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                run.run_id,
                run.status,
                run.started_at.isoformat(),
                run.completed_at.isoformat() if run.completed_at else None,
                run.model_dump_json(),
            ),
        )
        for subscription_id, state in contexts.items():
            subscription = state.subscription
            connection.execute(
                """
                INSERT INTO subscriptions(subscription_id, service_name, status, payload_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(subscription_id) DO UPDATE SET
                    service_name=excluded.service_name,
                    status=excluded.status,
                    payload_json=excluded.payload_json,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    subscription.subscription_id,
                    subscription.service_name,
                    subscription.status.value,
                    subscription.model_dump_json(),
                ),
            )
            state_json = state.model_dump_json()
            connection.execute(
                """
                INSERT INTO run_subscriptions(run_id, subscription_id, final_status, payload_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(run_id, subscription_id) DO UPDATE SET
                    final_status=excluded.final_status,
                    payload_json=excluded.payload_json
                """,
                (run.run_id, subscription_id, state.final_status.value, state_json),
            )
            _append_event(connection, run.run_id, subscription_id, state)


def _append_event(
    connection: sqlite3.Connection,
    run_id: str,
    subscription_id: str,
    state: "SubscriptionRunState",
) -> None:
    event_at = (
        state.verification_result.verified_at
        if state.verification_result and state.verification_result.verified_at
        else state.approval.decided_at
        if state.approval and state.approval.decided_at
        else state.plan.created_at
        if state.plan
        else state.decision.evaluated_at
        if state.decision
        else state.subscription.last_used_at
    )
    if event_at is None:
        return
    events = [
        ("decision", state.decision.decision_id if state.decision else None, state.decision),
        ("plan", state.plan.plan_id if state.plan else None, state.plan),
        ("approval", state.approval.approval_id if state.approval else None, state.approval),
        ("action", state.action_result.action_id if state.action_result else None, state.action_result),
        ("verification", state.verification_result.verification_id if state.verification_result else None, state.verification_result),
    ]
    for event_type, entity_id, entity in events:
        if entity is None:
            continue
        timestamp = getattr(entity, "decided_at", None) or getattr(entity, "verified_at", None)
        timestamp = timestamp or getattr(entity, "created_at", None) or getattr(entity, "started_at", None) or event_at
        connection.execute(
            """
            INSERT OR IGNORE INTO audit_events
                (run_id, subscription_id, event_type, entity_id, event_at, payload_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (run_id, subscription_id, event_type, entity_id, timestamp.isoformat(), entity.model_dump_json()),
        )


def load_run_snapshots() -> list[tuple[AgentRun, Dict[str, "SubscriptionRunState"]]]:
    """Load persisted runs for process-start recovery."""
    from backend.app.agent import SubscriptionRunState

    with get_db_session() as connection:
        rows = connection.execute(
            "SELECT run_id, payload_json FROM runs ORDER BY started_at"
        ).fetchall()
        snapshots = []
        for row in rows:
            run = AgentRun.model_validate_json(row["payload_json"])
            state_rows = connection.execute(
                """
                SELECT payload_json FROM run_subscriptions
                WHERE run_id = ? ORDER BY subscription_id
                """,
                (row["run_id"],),
            ).fetchall()
            contexts = {}
            for state_row in state_rows:
                restored_state = SubscriptionRunState.model_validate_json(
                    state_row["payload_json"]
                )
                contexts[restored_state.subscription.subscription_id] = restored_state
            snapshots.append((run, contexts))
        return snapshots


def list_audit_events(run_id: str) -> list[dict]:
    """Return immutable audit events for reporting and dashboard history."""
    with get_db_session() as connection:
        rows = connection.execute(
            """
            SELECT event_type, entity_id, event_at, payload_json
            FROM audit_events WHERE run_id = ? ORDER BY event_at, event_id
            """,
            (run_id,),
        ).fetchall()
        return [
            {
                "event_type": row["event_type"],
                "entity_id": row["entity_id"],
                "event_at": row["event_at"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]


def save_subscription(subscription: Subscription) -> None:
    """Persist a standalone subscription record."""
    with get_db_session() as connection:
        connection.execute(
            """
            INSERT INTO subscriptions(subscription_id, service_name, status, payload_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(subscription_id) DO UPDATE SET
                service_name=excluded.service_name,
                status=excluded.status,
                payload_json=excluded.payload_json,
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                subscription.subscription_id,
                subscription.service_name,
                subscription.status.value,
                subscription.model_dump_json(),
            ),
        )
