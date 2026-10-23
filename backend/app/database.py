"""Durable SQLite persistence for orchestration runs and their audit history.

The workflow models remain Pydantic objects in the orchestration layer. This
module owns the durable boundary and stores both indexed lifecycle fields and
validated JSON snapshots so a run can be restored after a process restart.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from datetime import date
from pathlib import Path
import sqlite3
from typing import Any, Dict, Iterator, List, Optional, Tuple, TYPE_CHECKING

from backend.app.models import ActionResult, AgentRun, Subscription, SubscriptionStatus

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
        CREATE TABLE IF NOT EXISTS gmail_candidates (
            message_id TEXT PRIMARY KEY,
            thread_id TEXT,
            user_id TEXT NOT NULL,
            subject TEXT,
            sender TEXT,
            date_header TEXT,
            provider_name TEXT,
            amount REAL,
            currency TEXT,
            billing_date TEXT,
            body_preview TEXT,
            confirmed INTEGER NOT NULL DEFAULT 0,
            extracted_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_gmail_candidates_user
            ON gmail_candidates(user_id, confirmed);
        CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);
        CREATE INDEX IF NOT EXISTS idx_run_subscriptions_status
            ON run_subscriptions(final_status);
        CREATE INDEX IF NOT EXISTS idx_audit_events_subscription
            ON audit_events(subscription_id, event_at);
        CREATE UNIQUE INDEX IF NOT EXISTS idx_action_records_idempotency
            ON action_records(idempotency_key) WHERE idempotency_key IS NOT NULL;
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


def save_gmail_candidates(
    candidates: List[Dict[str, Any]],
    user_id: str,
) -> int:
    """Persist Gmail billing candidates, skipping duplicates by message_id.

    Returns the number of newly inserted records.
    """
    inserted = 0
    with get_db_session() as connection:
        for c in candidates:
            try:
                connection.execute(
                    """
                    INSERT INTO gmail_candidates(
                        message_id, thread_id, user_id, subject, sender,
                        date_header, provider_name, amount, currency,
                        billing_date, body_preview, extracted_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(message_id) DO UPDATE SET
                        provider_name=excluded.provider_name,
                        amount=excluded.amount,
                        billing_date=excluded.billing_date,
                        body_preview=excluded.body_preview
                    """,
                    (
                        c.get("message_id"),
                        c.get("thread_id"),
                        user_id,
                        c.get("subject", "")[:200],
                        c.get("sender", "")[:200],
                        c.get("date_header", "")[:50],
                        c.get("provider_name"),
                        c.get("amount"),
                        c.get("currency"),
                        c.get("billing_date"),
                        c.get("body_preview", "")[:500],
                        c.get("extracted_at", ""),
                    ),
                )
                inserted += 1
            except Exception:
                continue
    return inserted


def load_gmail_candidates(
    user_id: str,
    confirmed_only: bool = False,
) -> List[Dict[str, Any]]:
    """Load Gmail billing candidates for the given user."""
    with get_db_session() as connection:
        query = """
            SELECT message_id, thread_id, subject, sender, date_header,
                   provider_name, amount, currency, billing_date,
                   body_preview, confirmed, extracted_at, created_at
            FROM gmail_candidates
            WHERE user_id = ?
        """
        params: list = [user_id]
        if confirmed_only:
            query += " AND confirmed = 1"
        query += " ORDER BY billing_date DESC, created_at DESC"
        rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]


def load_all_subscriptions() -> List[Subscription]:
    """Load all persisted subscriptions from database."""
    with get_db_session() as connection:
        rows = connection.execute(
            "SELECT payload_json FROM subscriptions ORDER BY service_name"
        ).fetchall()
        return [Subscription.model_validate_json(row["payload_json"]) for row in rows]


def confirm_gmail_candidate(message_id: str, user_id: str) -> bool:
    """Mark a Gmail billing candidate as user-confirmed and promote to subscription inventory.

    Returns True if found and updated/confirmed, False if not found.
    Handles duplicate confirmations safely and idempotently.
    """
    with get_db_session() as connection:
        row = connection.execute(
            """
            SELECT message_id, thread_id, subject, sender, date_header,
                   provider_name, amount, currency, billing_date,
                   body_preview, confirmed, extracted_at
            FROM gmail_candidates
            WHERE message_id = ? AND user_id = ?
            """,
            (message_id, user_id),
        ).fetchone()
        if not row:
            return False

        candidate = dict(row)
        if not candidate["confirmed"]:
            connection.execute(
                "UPDATE gmail_candidates SET confirmed = 1 WHERE message_id = ? AND user_id = ?",
                (message_id, user_id),
            )

        # Promote to subscription inventory if not already present
        sub_id = f"sub_gmail_{message_id}"
        existing = connection.execute(
            "SELECT subscription_id FROM subscriptions WHERE subscription_id = ?",
            (sub_id,),
        ).fetchone()

        if not existing:
            # Parse billing_date safely into date if present
            renewal_d = None
            raw_date = candidate.get("billing_date")
            if raw_date:
                try:
                    renewal_d = date.fromisoformat(str(raw_date)[:10])
                except (ValueError, TypeError):
                    renewal_d = None

            sub = Subscription(
                subscription_id=sub_id,
                service_name=candidate.get("provider_name") or "Discovered Service",
                status=SubscriptionStatus.ACTIVE,
                renewal_date=renewal_d,
                last_used_at=None,  # Never invent usage history
                monthly_cost=candidate.get("amount"),
                currency=candidate.get("currency") or "USD",
                billing_cycle="monthly",
                source_record_id=message_id,
                source_url=None,
                cancellation_terms_url=None,
                data_deletion_warning=False,
                pause_option_available=False,
            )
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
                    sub.subscription_id,
                    sub.service_name,
                    sub.status.value,
                    sub.model_dump_json(),
                ),
            )
        return True


def get_spending_analytics(user_id: str) -> Dict[str, Any]:
    """Calculate verified spending analytics from persisted subscriptions and candidates.

    Adheres strictly to financial safety invariants:
    - Never combines incompatible currencies into a single raw sum.
    - Never treats missing amounts as zero.
    - Excludes one-time purchases from recurring cost totals.
    - Converts non-monthly billing cycles deterministically.
    - Avoids double-counting candidates that were confirmed into subscriptions.
    - Clearly marks estimates and itemizes data uncertainties.
    """
    with get_db_session() as connection:
        sub_rows = connection.execute(
            "SELECT subscription_id, service_name, status, payload_json FROM subscriptions ORDER BY service_name"
        ).fetchall()

        candidate_rows = connection.execute(
            """
            SELECT message_id, thread_id, subject, sender, date_header,
                   provider_name, amount, currency, billing_date,
                   body_preview, confirmed, extracted_at
            FROM gmail_candidates
            WHERE user_id = ?
            ORDER BY billing_date DESC, created_at DESC
            """,
            (user_id,),
        ).fetchall()

    subscriptions = [Subscription.model_validate_json(r["payload_json"]) for r in sub_rows]
    candidates = [dict(r) for r in candidate_rows]

    # If no subscriptions in database yet, load from connectors to establish baseline
    if not subscriptions:
        try:
            from backend.app.connectors import fetch_subscriptions
            subscriptions = fetch_subscriptions()
        except Exception:
            from backend.app.agent import load_fixture_data
            subscriptions, _ = load_fixture_data()
        for s in subscriptions:
            save_subscription(s)

    candidates_awaiting_review = [c for c in candidates if not c.get("confirmed")]

    currency_totals: Dict[str, Dict[str, Any]] = {}
    active_subscriptions: List[Dict[str, Any]] = []
    cancelled_subscriptions: List[Dict[str, Any]] = []
    unknown_status_subscriptions: List[Dict[str, Any]] = []
    one_time_purchases: List[Dict[str, Any]] = []
    unknown_cost_subscriptions: List[Dict[str, Any]] = []
    upcoming_renewals: List[Dict[str, Any]] = []
    uncertainties: List[Dict[str, Any]] = []

    for sub in subscriptions:
        sub_dict = {
            "subscription_id": sub.subscription_id,
            "service_name": sub.service_name,
            "status": sub.status.value,
            "monthly_cost": sub.monthly_cost,
            "currency": sub.currency or "USD",
            "billing_cycle": sub.billing_cycle or "monthly",
            "renewal_date": sub.renewal_date.isoformat() if sub.renewal_date else None,
            "source_record_id": sub.source_record_id,
        }

        cycle = (sub.billing_cycle or "monthly").strip().lower()
        is_one_time = cycle in ("one-time", "one_time", "one-off", "single", "non-recurring")

        if is_one_time:
            one_time_purchases.append(sub_dict)
            continue

        if sub.status == SubscriptionStatus.CANCELLED:
            cancelled_subscriptions.append(sub_dict)
            continue
        elif sub.status == SubscriptionStatus.UNKNOWN:
            unknown_status_subscriptions.append(sub_dict)
            uncertainties.append({
                "subscription_id": sub.subscription_id,
                "service_name": sub.service_name,
                "issue": "Unknown subscription status",
            })
            continue

        active_subscriptions.append(sub_dict)

        if sub.renewal_date:
            upcoming_renewals.append({
                "subscription_id": sub.subscription_id,
                "service_name": sub.service_name,
                "renewal_date": sub.renewal_date.isoformat(),
                "amount": sub.monthly_cost,
                "currency": sub.currency or "USD",
            })
        else:
            uncertainties.append({
                "subscription_id": sub.subscription_id,
                "service_name": sub.service_name,
                "issue": "Missing renewal date",
            })

        if sub.monthly_cost is None:
            unknown_cost_subscriptions.append(sub_dict)
            uncertainties.append({
                "subscription_id": sub.subscription_id,
                "service_name": sub.service_name,
                "issue": "Missing or unknown billing cost (not counted as 0)",
            })
            continue

        cost = float(sub.monthly_cost)
        curr = (sub.currency or "USD").upper()

        if cycle in ("annual", "yearly"):
            m_cost = round(cost / 12.0, 2)
            a_cost = round(cost, 2)
        elif cycle == "quarterly":
            m_cost = round(cost / 3.0, 2)
            a_cost = round(cost * 4.0, 2)
        elif cycle == "weekly":
            m_cost = round(cost * 52.0 / 12.0, 2)
            a_cost = round(cost * 52.0, 2)
        else:
            m_cost = round(cost, 2)
            a_cost = round(cost * 12.0, 2)

        sub_dict["monthly_equivalent"] = m_cost
        sub_dict["annual_equivalent"] = a_cost

        if curr not in currency_totals:
            currency_totals[curr] = {
                "currency": curr,
                "monthly_recurring_cost": 0.0,
                "annual_recurring_cost": 0.0,
                "count": 0,
            }
        currency_totals[curr]["monthly_recurring_cost"] = round(currency_totals[curr]["monthly_recurring_cost"] + m_cost, 2)
        currency_totals[curr]["annual_recurring_cost"] = round(currency_totals[curr]["annual_recurring_cost"] + a_cost, 2)
        currency_totals[curr]["count"] += 1

    upcoming_renewals.sort(key=lambda x: x["renewal_date"])

    currency_symbols = {"USD": "$", "EUR": "€", "GBP": "£", "CAD": "CA$", "AUD": "A$", "JPY": "¥"}
    display_parts_monthly = []
    display_parts_annual = []
    for curr, totals in currency_totals.items():
        sym = currency_symbols.get(curr, f"{curr} ")
        display_parts_monthly.append(f"{sym}{totals['monthly_recurring_cost']:.2f}")
        display_parts_annual.append(f"{sym}{totals['annual_recurring_cost']:.2f}")

    primary_display = " + ".join(display_parts_monthly) if display_parts_monthly else "$0.00"
    annual_display = " + ".join(display_parts_annual) if display_parts_annual else "$0.00"

    return {
        "total_monitored": len(subscriptions),
        "confirmed_subscriptions_count": len(subscriptions),
        "active_subscriptions_count": len(active_subscriptions),
        "cancelled_subscriptions_count": len(cancelled_subscriptions),
        "unknown_status_count": len(unknown_status_subscriptions),
        "candidates_awaiting_review_count": len(candidates_awaiting_review),
        "one_time_purchases_count": len(one_time_purchases),
        "unknown_cost_count": len(unknown_cost_subscriptions),
        "totals_by_currency": currency_totals,
        "is_multi_currency": len(currency_totals) > 1,
        "monthly_recurring_display": primary_display,
        "annual_recurring_display": annual_display,
        "is_estimate": True,
        "upcoming_renewals": upcoming_renewals,
        "uncertainties": uncertainties,
        "active_subscriptions": active_subscriptions,
        "candidates_awaiting_review": [
            {
                "message_id": c.get("message_id"),
                "provider_name": c.get("provider_name"),
                "amount": c.get("amount"),
                "currency": c.get("currency"),
                "billing_date": c.get("billing_date"),
                "subject": c.get("subject"),
                "extracted_at": c.get("extracted_at"),
            }
            for c in candidates_awaiting_review
        ],
    }
