"""Durable SQLite persistence for orchestration runs and their audit history.

The workflow models remain Pydantic objects in the orchestration layer. This
module owns the durable boundary and stores both indexed lifecycle fields and
validated JSON snapshots so a run can be restored after a process restart.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Dict, Iterator, List, Optional, Tuple, TYPE_CHECKING

from backend.app.models import (
    ActionResult,
    AgentRun,
    CandidateReviewStatus,
    EmailAccountStatus,
    Subscription,
    SubscriptionCandidate,
)

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
            user_id TEXT NOT NULL DEFAULT 'user_default',
            discovery_source TEXT NOT NULL DEFAULT 'connector',
            category TEXT,
            is_archived INTEGER NOT NULL DEFAULT 0,
            notes TEXT,
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
        CREATE TABLE IF NOT EXISTS action_records (
            action_id TEXT PRIMARY KEY,
            idempotency_key TEXT,
            subscription_id TEXT NOT NULL,
            plan_id TEXT NOT NULL,
            plan_version TEXT NOT NULL,
            action_type TEXT NOT NULL,
            status TEXT NOT NULL,
            request_sig_json TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS test_results (
            test_id TEXT PRIMARY KEY,
            scenario_id TEXT NOT NULL,
            status TEXT NOT NULL,
            expected TEXT NOT NULL,
            actual TEXT NOT NULL,
            evidence_summary TEXT,
            details TEXT,
            executed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS email_accounts (
            user_id TEXT PRIMARY KEY,
            email_address TEXT,
            is_connected INTEGER NOT NULL DEFAULT 0,
            provider TEXT NOT NULL DEFAULT 'gmail',
            scopes_json TEXT NOT NULL DEFAULT '[]',
            refresh_token_encrypted TEXT,
            access_token_encrypted TEXT,
            token_expires_at TEXT,
            last_synced_at TEXT,
            sync_status TEXT NOT NULL DEFAULT 'idle',
            error_message TEXT,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS subscription_candidates (
            candidate_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            merchant_name TEXT NOT NULL,
            product_name TEXT,
            estimated_cost REAL,
            currency TEXT DEFAULT 'USD',
            billing_interval TEXT DEFAULT 'monthly',
            detected_renewal_date TEXT,
            detected_last_used TEXT,
            review_status TEXT NOT NULL DEFAULT 'pending',
            confidence REAL DEFAULT 0.8,
            source_message_id TEXT,
            source_subject TEXT,
            source_sender TEXT,
            source_date TEXT,
            email_snippet TEXT,
            cancellation_signal INTEGER DEFAULT 0,
            notes TEXT,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
        CREATE INDEX IF NOT EXISTS idx_run_subscriptions_status
            ON run_subscriptions(final_status);
        CREATE INDEX IF NOT EXISTS idx_audit_events_subscription
            ON audit_events(subscription_id, event_at);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_action_records_idempotency
            ON action_records(idempotency_key) WHERE idempotency_key IS NOT NULL;
        CREATE INDEX IF NOT EXISTS idx_candidates_user_review
            ON subscription_candidates(user_id, review_status);
        """
    )
    # Lightweight schema migration for pre-existing subscriptions table
    try:
        cursor = connection.execute("PRAGMA table_info(subscriptions)")
        cols = {row[1] for row in cursor.fetchall()}
        if "user_id" not in cols:
            connection.execute("ALTER TABLE subscriptions ADD COLUMN user_id TEXT NOT NULL DEFAULT 'user_default'")
        if "discovery_source" not in cols:
            connection.execute("ALTER TABLE subscriptions ADD COLUMN discovery_source TEXT NOT NULL DEFAULT 'connector'")
        if "category" not in cols:
            connection.execute("ALTER TABLE subscriptions ADD COLUMN category TEXT")
        if "is_archived" not in cols:
            connection.execute("ALTER TABLE subscriptions ADD COLUMN is_archived INTEGER NOT NULL DEFAULT 0")
        if "notes" not in cols:
            connection.execute("ALTER TABLE subscriptions ADD COLUMN notes TEXT")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_subscriptions_user_archived ON subscriptions(user_id, is_archived)"
        )
    except Exception:
        pass

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
    for ev in state.evidence_list:
        connection.execute(
            """
            INSERT OR IGNORE INTO audit_events
                (run_id, subscription_id, event_type, entity_id, event_at, payload_json)
            VALUES (?, ?, 'evidence', ?, ?, ?)
            """,
            (run_id, subscription_id, ev.evidence_id, ev.captured_at.isoformat(), ev.model_dump_json()),
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
    """Persist a standalone subscription record with full metadata."""
    with get_db_session() as connection:
        connection.execute(
            """
            INSERT INTO subscriptions(
                subscription_id, service_name, status, payload_json,
                user_id, discovery_source, category, is_archived, notes, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(subscription_id) DO UPDATE SET
                service_name=excluded.service_name,
                status=excluded.status,
                payload_json=excluded.payload_json,
                user_id=excluded.user_id,
                discovery_source=excluded.discovery_source,
                category=excluded.category,
                is_archived=excluded.is_archived,
                notes=excluded.notes,
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                subscription.subscription_id,
                subscription.service_name,
                subscription.status.value,
                subscription.model_dump_json(),
                subscription.user_id or "user_default",
                subscription.discovery_source or "connector",
                subscription.category,
                1 if subscription.is_archived else 0,
                subscription.notes,
            ),
        )


def get_subscription_from_db(
    subscription_id: str, user_id: Optional[str] = None
) -> Optional[Subscription]:
    """Retrieve a single subscription from SQLite store."""
    with get_db_session() as connection:
        query = "SELECT payload_json FROM subscriptions WHERE subscription_id = ?"
        params: List[Any] = [subscription_id]
        if user_id:
            query += " AND user_id = ?"
            params.append(user_id)
        row = connection.execute(query, params).fetchone()
        if not row:
            return None
        return Subscription.model_validate_json(row["payload_json"])


def list_subscriptions_from_db(
    user_id: Optional[str] = None,
    include_archived: bool = False,
    status: Optional[str] = None,
    source: Optional[str] = None,
) -> List[Subscription]:
    """Retrieve subscriptions from SQLite store with optional filtering."""
    with get_db_session() as connection:
        clauses = []
        params: List[Any] = []
        if user_id:
            clauses.append("user_id = ?")
            params.append(user_id)
        if not include_archived:
            clauses.append("is_archived = 0")
        if status:
            clauses.append("status = ?")
            params.append(status)
        if source:
            clauses.append("discovery_source = ?")
            params.append(source)

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT payload_json FROM subscriptions {where_sql} ORDER BY created_at DESC"
        rows = connection.execute(query, params).fetchall()
        return [Subscription.model_validate_json(r["payload_json"]) for r in rows]


def archive_subscription_in_db(
    subscription_id: str, user_id: Optional[str] = None
) -> bool:
    """Mark a subscription as archived."""
    sub = get_subscription_from_db(subscription_id, user_id=user_id)
    if not sub:
        return False
    sub.is_archived = True
    save_subscription(sub)
    return True


def delete_subscription_in_db(
    subscription_id: str, user_id: Optional[str] = None
) -> bool:
    """Hard-delete a subscription if needed (e.g. for test cleanup)."""
    with get_db_session() as connection:
        query = "DELETE FROM subscriptions WHERE subscription_id = ?"
        params: List[Any] = [subscription_id]
        if user_id:
            query += " AND user_id = ?"
            params.append(user_id)
        cursor = connection.execute(query, params)
        return cursor.rowcount > 0


def save_email_account(
    account: EmailAccountStatus,
    refresh_token: Optional[str] = None,
    access_token: Optional[str] = None,
    expires_at: Optional[datetime] = None,
) -> None:
    """Persist or update connected email account status and credentials."""
    with get_db_session() as connection:
        connection.execute(
            """
            INSERT INTO email_accounts(
                user_id, email_address, is_connected, provider, scopes_json,
                refresh_token_encrypted, access_token_encrypted, token_expires_at,
                last_synced_at, sync_status, error_message, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                email_address=excluded.email_address,
                is_connected=excluded.is_connected,
                provider=excluded.provider,
                scopes_json=excluded.scopes_json,
                refresh_token_encrypted=COALESCE(excluded.refresh_token_encrypted, email_accounts.refresh_token_encrypted),
                access_token_encrypted=COALESCE(excluded.access_token_encrypted, email_accounts.access_token_encrypted),
                token_expires_at=COALESCE(excluded.token_expires_at, email_accounts.token_expires_at),
                last_synced_at=excluded.last_synced_at,
                sync_status=excluded.sync_status,
                error_message=excluded.error_message,
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                account.user_id,
                account.email_address,
                1 if account.is_connected else 0,
                account.provider,
                json.dumps(account.scopes),
                refresh_token,
                access_token,
                expires_at.isoformat() if expires_at else None,
                account.last_synced_at.isoformat() if account.last_synced_at else None,
                account.sync_status,
                account.error_message,
            ),
        )


def get_email_account(user_id: str = "user_default") -> Optional[EmailAccountStatus]:
    """Retrieve connected email account for user with truthful mode attribution."""
    from backend.app.config import is_live_oauth_configured
    live_configured = is_live_oauth_configured()

    with get_db_session() as connection:
        row = connection.execute(
            "SELECT * FROM email_accounts WHERE user_id = ?", (user_id,)
        ).fetchone()
        if not row:
            return EmailAccountStatus(
                user_id=user_id,
                is_connected=False,
                is_live_configured=live_configured,
                mode="live" if live_configured else "demo",
            )
        candidate_count = connection.execute(
            "SELECT COUNT(*) as cnt FROM subscription_candidates WHERE user_id = ? AND review_status = 'pending'",
            (user_id,),
        ).fetchone()["cnt"]

        is_connected = bool(row["is_connected"])
        has_tokens = bool(row["access_token_encrypted"] or row["refresh_token_encrypted"])
        token_str = str(row["access_token_encrypted"] or "")
        is_mock_token = token_str.startswith("mock_") or token_str.startswith("test_")
        stored_email = row["email_address"]

        # An account without credentials cannot be legitimately connected
        if not has_tokens:
            is_connected = False

        account_mode = "demo"
        if is_connected and live_configured and not is_mock_token and not (stored_email or "").endswith("@subcan-demo.com"):
            account_mode = "live"
        elif not is_connected and live_configured:
            account_mode = "live"

        email_addr = stored_email if is_connected else None

        return EmailAccountStatus(
            user_id=row["user_id"],
            email_address=email_addr,
            is_connected=is_connected,
            provider=row["provider"],
            scopes=json.loads(row["scopes_json"] or "[]"),
            last_synced_at=datetime.fromisoformat(row["last_synced_at"]) if row["last_synced_at"] else None,
            sync_status=row["sync_status"],
            candidate_count=candidate_count,
            error_message=row["error_message"],
            is_live_configured=live_configured,
            mode=account_mode,
        )


def get_email_tokens(user_id: str = "user_default") -> Optional[Dict[str, Any]]:
    """Retrieve stored OAuth tokens for internal API communication."""
    with get_db_session() as connection:
        row = connection.execute(
            "SELECT refresh_token_encrypted, access_token_encrypted, token_expires_at FROM email_accounts WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "refresh_token": row["refresh_token_encrypted"],
            "access_token": row["access_token_encrypted"],
            "expires_at": row["token_expires_at"],
        }


def disconnect_email_account(user_id: str = "user_default") -> bool:
    """Disconnect email account, delete tokens, and reset sync status."""
    with get_db_session() as connection:
        connection.execute(
            """
            UPDATE email_accounts SET
                is_connected = 0,
                refresh_token_encrypted = NULL,
                access_token_encrypted = NULL,
                token_expires_at = NULL,
                sync_status = 'idle',
                updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ?
            """,
            (user_id,),
        )
        return True


def save_subscription_candidate(candidate: SubscriptionCandidate) -> None:
    """Save an email-extracted candidate subscription."""
    with get_db_session() as connection:
        connection.execute(
            """
            INSERT INTO subscription_candidates(
                candidate_id, user_id, merchant_name, product_name,
                estimated_cost, currency, billing_interval, detected_renewal_date,
                detected_last_used, review_status, confidence, source_message_id,
                source_subject, source_sender, source_date, email_snippet,
                cancellation_signal, notes, payload_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(candidate_id) DO UPDATE SET
                review_status=CASE
                    WHEN subscription_candidates.review_status IN ('confirmed', 'dismissed')
                    THEN subscription_candidates.review_status
                    ELSE excluded.review_status
                END,
                notes=excluded.notes,
                payload_json=excluded.payload_json,
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                candidate.candidate_id,
                candidate.user_id,
                candidate.merchant_name,
                candidate.product_name,
                candidate.estimated_cost,
                candidate.currency,
                candidate.billing_interval,
                candidate.detected_renewal_date.isoformat() if candidate.detected_renewal_date else None,
                candidate.detected_last_used.isoformat() if candidate.detected_last_used else None,
                candidate.review_status.value if isinstance(candidate.review_status, CandidateReviewStatus) else candidate.review_status,
                candidate.confidence,
                candidate.source_message_id,
                candidate.source_subject,
                candidate.source_sender,
                candidate.source_date.isoformat() if candidate.source_date else None,
                candidate.email_snippet,
                1 if candidate.cancellation_signal else 0,
                candidate.notes,
                candidate.model_dump_json(),
            ),
        )


def list_subscription_candidates(
    user_id: str = "user_default",
    review_status: Optional[str] = None,
) -> List[SubscriptionCandidate]:
    """List subscription candidates for user review."""
    with get_db_session() as connection:
        query = "SELECT payload_json FROM subscription_candidates WHERE user_id = ?"
        params: List[Any] = [user_id]
        if review_status:
            query += " AND review_status = ?"
            params.append(review_status)
        query += " ORDER BY created_at DESC"
        rows = connection.execute(query, params).fetchall()
        return [SubscriptionCandidate.model_validate_json(r["payload_json"]) for r in rows]


def get_subscription_candidate(
    candidate_id: str, user_id: Optional[str] = None
) -> Optional[SubscriptionCandidate]:
    """Retrieve single subscription candidate."""
    with get_db_session() as connection:
        query = "SELECT payload_json FROM subscription_candidates WHERE candidate_id = ?"
        params: List[Any] = [candidate_id]
        if user_id:
            query += " AND user_id = ?"
            params.append(user_id)
        row = connection.execute(query, params).fetchone()
        if not row:
            return None
        return SubscriptionCandidate.model_validate_json(row["payload_json"])


def update_candidate_status(
    candidate_id: str,
    status: CandidateReviewStatus,
    user_id: Optional[str] = None,
) -> bool:
    """Update candidate review state."""
    cand = get_subscription_candidate(candidate_id, user_id=user_id)
    if not cand:
        return False
    cand.review_status = status
    save_subscription_candidate(cand)
    return True



def save_action_record(
    key: Optional[str],
    action: ActionResult,
    request_sig: Dict[str, str],
) -> None:
    """Persist an action result and its idempotency signature durably."""
    try:
        with get_db_session() as connection:
            connection.execute(
                """
                INSERT INTO action_records(
                    action_id, idempotency_key, subscription_id, plan_id,
                    plan_version, action_type, status, request_sig_json, payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(action_id) DO UPDATE SET
                    status=excluded.status,
                    payload_json=excluded.payload_json
                """,
                (
                    action.action_id,
                    key,
                    action.subscription_id,
                    action.plan_id,
                    request_sig.get("plan_version", ""),
                    action.action_type,
                    action.status.value,
                    json.dumps(request_sig),
                    action.model_dump_json(),
                ),
            )
    except Exception:
        pass


def load_action_records() -> List[Tuple[Optional[str], ActionResult, Dict[str, str]]]:
    """Load persisted actions to restore ActionRegistry on startup."""
    try:
        with get_db_session() as connection:
            rows = connection.execute(
                """
                SELECT idempotency_key, request_sig_json, payload_json
                FROM action_records
                """
            ).fetchall()
            records = []
            for row in rows:
                action = ActionResult.model_validate_json(row["payload_json"])
                sig = json.loads(row["request_sig_json"])
                records.append((row["idempotency_key"], action, sig))
            return records
    except Exception:
        return []


def save_test_result(
    test_id: str,
    scenario_id: str,
    status: str,
    expected: str,
    actual: str,
    evidence_summary: str = "",
    details: str = "",
) -> None:
    """Persist test execution scorecard result."""
    try:
        with get_db_session() as connection:
            connection.execute(
                """
                INSERT INTO test_results(
                    test_id, scenario_id, status, expected, actual,
                    evidence_summary, details, executed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(test_id) DO UPDATE SET
                    status=excluded.status,
                    expected=excluded.expected,
                    actual=excluded.actual,
                    evidence_summary=excluded.evidence_summary,
                    details=excluded.details,
                    executed_at=CURRENT_TIMESTAMP
                """,
                (test_id, scenario_id, status, expected, actual, evidence_summary, details),
            )
    except Exception:
        pass


def list_test_results() -> List[Dict[str, Any]]:
    """Retrieve persisted test run scorecard results."""
    try:
        with get_db_session() as connection:
            rows = connection.execute(
                """
                SELECT test_id, scenario_id, status, expected, actual,
                       evidence_summary, details, executed_at
                FROM test_results
                ORDER BY executed_at DESC, test_id ASC
                """
            ).fetchall()
            return [dict(row) for row in rows]
    except Exception:
        return []


def export_user_data(user_id: str) -> Dict[str, Any]:
    """Export all user-scoped data for portability and privacy compliance (GDPR/CCPA)."""
    with get_db_session() as connection:
        # Subscriptions
        sub_rows = connection.execute(
            "SELECT payload_json FROM subscriptions WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        subscriptions = [json.loads(r["payload_json"]) for r in sub_rows]

        # Discovered Candidates
        cand_rows = connection.execute(
            "SELECT payload_json FROM subscription_candidates WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        candidates = [json.loads(r["payload_json"]) for r in cand_rows]

        # Email Account (redacting tokens)
        acct_row = connection.execute(
            "SELECT user_id, email_address, is_connected, provider, scopes_json, last_synced_at, sync_status, error_message, updated_at FROM email_accounts WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        email_account = None
        if acct_row:
            email_account = {
                "user_id": acct_row["user_id"],
                "email_address": acct_row["email_address"],
                "is_connected": bool(acct_row["is_connected"]),
                "provider": acct_row["provider"],
                "scopes": json.loads(acct_row["scopes_json"]) if acct_row["scopes_json"] else [],
                "last_synced_at": acct_row["last_synced_at"],
                "sync_status": acct_row["sync_status"],
                "error_message": acct_row["error_message"],
                "updated_at": acct_row["updated_at"],
            }

        return {
            "user_id": user_id,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "subscriptions": subscriptions,
            "candidates": candidates,
            "email_account": email_account,
        }


def delete_user_data(user_id: str) -> Dict[str, Any]:
    """Purge all user-scoped subscriptions, candidates, and tokens for account deletion."""
    with get_db_session() as connection:
        # 1. Delete candidates
        connection.execute("DELETE FROM subscription_candidates WHERE user_id = ?", (user_id,))
        # 2. Delete subscriptions
        connection.execute("DELETE FROM subscriptions WHERE user_id = ?", (user_id,))
        # 3. Disconnect email account & delete tokens
        connection.execute("DELETE FROM email_accounts WHERE user_id = ?", (user_id,))

        return {
            "deleted": True,
            "user_id": user_id,
            "deleted_at": datetime.now(timezone.utc).isoformat(),
        }

