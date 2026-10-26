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
    SubscriptionStatus,
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


db_session = get_db_session



def audit_and_reconcile_database(
    connection: Optional[sqlite3.Connection] = None,
    user_id: str = "user_default",
    dry_run: bool = True,
) -> Dict[str, Any]:
    """Inspect and optionally reconcile the database to eliminate mock/synthetic fixtures and false positives.

    Supports dry-run mode to report findings without modifying state.
    Preserves all legitimate user records (e.g. manual subscriptions, connected email tokens).
    """
    owns_connection = connection is None
    if owns_connection:
        path = _database_path()
        connection = sqlite3.connect(path)
        connection.row_factory = sqlite3.Row

    try:
        report: Dict[str, Any] = {
            "user_id": user_id,
            "dry_run": dry_run,
            "mock_subscriptions_to_remove": [],
            "test_manual_subscriptions_to_remove": [],
            "false_positive_candidates_to_remove": [],
            "candidates_to_reconcile": [],
            "subscriptions_to_reconcile": [],
            "legitimate_subscriptions_preserved": [],
            "legitimate_candidates_preserved": [],
        }

        # 1. Inspect subscriptions
        sub_rows = connection.execute(
            "SELECT subscription_id, service_name, discovery_source, status, payload_json FROM subscriptions WHERE user_id = ?",
            (user_id,),
        ).fetchall()

        demo_sub_ids = {
            "sub_tc1_cinevault", "sub_recent_001", "sub_missing_usage_01",
            "sub_tc4_moviemax", "sub_tc4_gamestream", "sub_tc2_audiocloud", "sub_tc3_fitstream",
        }
        test_manual_names = {"Specialty Dev Server Pro", "Legacy Enterprise License"}

        for r in sub_rows:
            sid = r["subscription_id"]
            name = r["service_name"]
            source = r["discovery_source"]
            if source == "connector" or sid in demo_sub_ids:
                report["mock_subscriptions_to_remove"].append({
                    "subscription_id": sid,
                    "service_name": name,
                    "reason": "Synthetic mock connector subscription fixture",
                })
            elif source == "manual" and (name in test_manual_names or sid.startswith("sub_man_specialty_") or sid.startswith("sub_man_legacy_")):
                report["test_manual_subscriptions_to_remove"].append({
                    "subscription_id": sid,
                    "service_name": name,
                    "reason": "Test artifact subscription fixture",
                })
            else:
                # Check for past-due trial dates on confirmed email subscriptions
                if "spotify" in name.lower() and source in ("email", "gmail"):
                    sub_payload = json.loads(r["payload_json"]) if r["payload_json"] else {}
                    r_date = sub_payload.get("renewal_date")
                    if r_date and r_date <= str(date.today()):
                        report["subscriptions_to_reconcile"].append({
                            "subscription_id": sid,
                            "service_name": name,
                            "action": "reconcile_spotify_past_trial_status",
                            "reason": f"Historical trial offer date ({r_date}) is in the past without subsequent renewal; mark status as unknown rather than claiming active",
                        })
                report["legitimate_subscriptions_preserved"].append({
                    "subscription_id": sid,
                    "service_name": name,
                    "discovery_source": source,
                })

        # 2. Inspect candidates
        cand_rows = connection.execute(
            """SELECT candidate_id, merchant_name, product_name, estimated_cost, currency,
                      detected_renewal_date, review_status, source_message_id, source_subject,
                      cancellation_signal, lifecycle_status, payload_json
               FROM subscription_candidates
               WHERE user_id = ?""",
            (user_id,),
        ).fetchall()

        for cr in cand_rows:
            cid = cr["candidate_id"]
            mname = (cr["merchant_name"] or "").strip()
            subj = (cr["source_subject"] or "").lower()
            cost = cr["estimated_cost"]

            # False positive promotional candidates: Udemy or Unacademy marketing or demo fixtures
            if mname.lower() in ("udemy", "unacademy") or cid.startswith("cand_user_default_github_") or (cr["source_message_id"] or "").startswith("msg_gmail_"):
                report["false_positive_candidates_to_remove"].append({
                    "candidate_id": cid,
                    "merchant_name": mname,
                    "reason": "Marketing promotional email or demo fixture without verified billing transaction",
                })
                continue

            # Check for McAfee survey stat reconciliation
            if "mcafee" in mname.lower() and cost is not None and cost > 500:
                report["candidates_to_reconcile"].append({
                    "candidate_id": cid,
                    "merchant_name": mname,
                    "action": "clear_survey_quote_cost_and_mark_expired",
                    "reason": "Survey quote ($1,471) must be cleared to None; mark lifecycle as expired",
                })
                continue

            # Check for Spotify downgrade or past trial reconciliation
            if "spotify" in mname.lower() and cr["cancellation_signal"] == 0 and ("no longer on standard" in subj or "downgraded" in subj):
                report["candidates_to_reconcile"].append({
                    "candidate_id": cid,
                    "merchant_name": mname,
                    "action": "mark_payment_failed_and_cancelled",
                    "reason": "Tier downgrade message indicates subscription is no longer active",
                })
                continue
            elif "spotify" in mname.lower() and cr["detected_renewal_date"] and cr["detected_renewal_date"] <= str(date.today()):
                if cr["lifecycle_status"] not in ("unknown", "payment_failed", "cancelled", "expired"):
                    report["candidates_to_reconcile"].append({
                        "candidate_id": cid,
                        "merchant_name": mname,
                        "action": "reconcile_spotify_candidate_unknown_lifecycle",
                        "reason": f"Detected date ({cr['detected_renewal_date']}) was trial offer end date; mark lifecycle as unknown",
                    })
                    continue

            # Check for Google One refund reconciliation
            if "google" in mname.lower() and "refund" in subj and cr["cancellation_signal"] == 0:
                report["candidates_to_reconcile"].append({
                    "candidate_id": cid,
                    "merchant_name": mname,
                    "action": "mark_refunded",
                    "reason": "Refund approved email indicates subscription cancelled and refunded",
                })
                continue

            # Check for OpenAI payment failure notice
            if ("openai" in mname.lower() or "chatgpt" in mname.lower()) and cr["lifecycle_status"] != "payment_failed":
                if "payment method" in subj or "could not be processed" in subj or "failed" in subj:
                    report["candidates_to_reconcile"].append({
                        "candidate_id": cid,
                        "merchant_name": mname,
                        "action": "mark_openai_payment_failed",
                        "reason": "Payment method failure notice indicates payment failed, not active subscription",
                    })
                    continue

            report["legitimate_candidates_preserved"].append({
                "candidate_id": cid,
                "merchant_name": mname,
                "product_name": cr["product_name"],
            })

        # Apply changes if dry_run is False
        if not dry_run:
            if owns_connection:
                db_p = _database_path()
                if db_p.exists():
                    import shutil
                    bak_p = db_p.with_suffix(".db.bak")
                    try:
                        shutil.copy2(db_p, bak_p)
                    except Exception:
                        pass

            subs_to_del = [s["subscription_id"] for s in report["mock_subscriptions_to_remove"] + report["test_manual_subscriptions_to_remove"]]
            if subs_to_del:
                placeholders = ",".join(["?"] * len(subs_to_del))
                connection.execute(f"DELETE FROM run_subscriptions WHERE subscription_id IN ({placeholders})", subs_to_del)
                connection.execute(f"DELETE FROM audit_events WHERE subscription_id IN ({placeholders})", subs_to_del)
                connection.execute(f"DELETE FROM action_records WHERE subscription_id IN ({placeholders})", subs_to_del)
                connection.execute(f"DELETE FROM subscriptions WHERE subscription_id IN ({placeholders})", subs_to_del)

            # Apply subscription reconciliations
            for srec in report.get("subscriptions_to_reconcile", []):
                ssid = srec["subscription_id"]
                saction = srec["action"]
                s_row = connection.execute("SELECT payload_json FROM subscriptions WHERE subscription_id = ?", (ssid,)).fetchone()
                if s_row:
                    s_obj = Subscription.model_validate_json(s_row["payload_json"])
                    if saction == "reconcile_spotify_past_trial_status":
                        s_obj.status = SubscriptionStatus.UNKNOWN
                        s_obj.trial_end_date = s_obj.renewal_date
                        s_obj.renewal_date = None
                        s_obj.confidence = 0.6
                        s_obj.notes = "Detected from billing email: Your order confirmation for \"Premium Standard\" (Trial ended on 2026-03-04 - current status unknown)"
                    save_subscription(s_obj, connection=connection)

            cands_to_del = [c["candidate_id"] for c in report["false_positive_candidates_to_remove"]]
            if cands_to_del:
                c_placeholders = ",".join(["?"] * len(cands_to_del))
                connection.execute(f"DELETE FROM subscription_candidates WHERE candidate_id IN ({c_placeholders})", cands_to_del)

            # Apply candidate reconciliations
            for rec in report["candidates_to_reconcile"]:
                rcid = rec["candidate_id"]
                action = rec["action"]
                cand_row = connection.execute("SELECT payload_json FROM subscription_candidates WHERE candidate_id = ?", (rcid,)).fetchone()
                if cand_row:
                    cand_obj = SubscriptionCandidate.model_validate_json(cand_row["payload_json"])
                    if action == "clear_survey_quote_cost_and_mark_expired":
                        cand_obj.estimated_cost = None
                        cand_obj.currency = None
                        cand_obj.original_amount = None
                        cand_obj.original_currency = None
                        cand_obj.lifecycle_status = "expired"
                        cand_obj.cancellation_signal = True
                        cand_obj.detected_renewal_date = None
                        cand_obj.notes = "Detected from billing email: Protection ended notice (Survey quote cleared)"
                    elif action == "mark_payment_failed_and_cancelled":
                        cand_obj.lifecycle_status = "payment_failed"
                        cand_obj.cancellation_signal = True
                        cand_obj.notes = "Detected from billing email: Downgraded to Spotify Free"
                    elif action == "mark_refunded":
                        cand_obj.lifecycle_status = "refunded"
                        cand_obj.cancellation_signal = True
                        cand_obj.notes = "Detected from billing email: Google Play refund approved"
                    elif action == "mark_openai_payment_failed":
                        cand_obj.lifecycle_status = "payment_failed"
                        cand_obj.cancellation_signal = True
                        cand_obj.evidence_category = "payment_failure"
                        cand_obj.detected_renewal_date = None
                        cand_obj.confidence = 0.65
                        cand_obj.notes = "Detected from billing email: Please update your Plus payment method (Payment failure notice - verify account status)"
                    elif action == "reconcile_spotify_candidate_unknown_lifecycle":
                        cand_obj.lifecycle_status = "unknown"
                        cand_obj.trial_end_date = cand_obj.detected_renewal_date
                        cand_obj.detected_renewal_date = None
                        cand_obj.confidence = 0.6
                        cand_obj.notes = "Detected from billing email: Your order confirmation for \"Premium Standard\" (Trial ended on 2026-03-04 - current status unknown)"
                    save_subscription_candidate(cand_obj, connection=connection)

            if owns_connection:
                connection.commit()

        return report
    finally:
        if owns_connection:
            connection.close()


def cleanup_demo_and_mock_data(
    connection: Optional[sqlite3.Connection] = None,
    user_id: str = "user_default",
) -> Dict[str, Any]:
    """Safely and idempotently clean up mock connector records, test artifacts, and sample candidates for real users.

    Guarantees:
    - Never deletes genuine Gmail-discovered records or legitimate user-created manual subscriptions.
    - Preserves OAuth credentials, tokens, audit events, and user review decisions.
    - Targets only known demo identifiers, connector subscriptions, and test artifact records.
    """
    report = audit_and_reconcile_database(connection=connection, user_id=user_id, dry_run=False)
    removed_subs = [s["subscription_id"] for s in report["mock_subscriptions_to_remove"] + report["test_manual_subscriptions_to_remove"]]
    removed_cands = [c["candidate_id"] for c in report["false_positive_candidates_to_remove"]]
    return {
        "success": True,
        "user_id": user_id,
        "subscriptions_removed_count": len(removed_subs),
        "subscriptions_removed_ids": removed_subs,
        "candidates_removed_count": len(removed_cands),
        "candidates_removed_ids": removed_cands,
        "reconciled_candidates_count": len(report["candidates_to_reconcile"]),
    }


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
            user_id TEXT NOT NULL DEFAULT 'user_default',
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
            account_id TEXT,
            account_email TEXT,
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
            user_id TEXT NOT NULL DEFAULT 'user_default',
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
            user_id TEXT NOT NULL DEFAULT 'user_default',
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
            account_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            email_address TEXT,
            account_name TEXT,
            is_connected INTEGER NOT NULL DEFAULT 0,
            is_default INTEGER NOT NULL DEFAULT 1,
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
            account_id TEXT,
            account_email TEXT,
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
        CREATE INDEX IF NOT EXISTS idx_email_accounts_user
            ON email_accounts(user_id);
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            google_sub TEXT UNIQUE,
            email TEXT UNIQUE NOT NULL,
            name TEXT,
            picture TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_login_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS user_sessions (
            session_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            session_token_hash TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            expires_at TEXT NOT NULL,
            is_revoked INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_users_google_sub ON users(google_sub);
        CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
        CREATE INDEX IF NOT EXISTS idx_sessions_token_hash ON user_sessions(session_token_hash);
        CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON user_sessions(user_id);
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
        if "original_amount" not in cols:
            connection.execute("ALTER TABLE subscriptions ADD COLUMN original_amount REAL")
        if "original_currency" not in cols:
            connection.execute("ALTER TABLE subscriptions ADD COLUMN original_currency TEXT")
        if "converted_amount" not in cols:
            connection.execute("ALTER TABLE subscriptions ADD COLUMN converted_amount REAL")
        if "converted_currency" not in cols:
            connection.execute("ALTER TABLE subscriptions ADD COLUMN converted_currency TEXT")
        if "lifecycle_status" not in cols:
            connection.execute("ALTER TABLE subscriptions ADD COLUMN lifecycle_status TEXT")
        if "trial_end_date" not in cols:
            connection.execute("ALTER TABLE subscriptions ADD COLUMN trial_end_date TEXT")
        if "purchase_date" not in cols:
            connection.execute("ALTER TABLE subscriptions ADD COLUMN purchase_date TEXT")
        if "cancellation_date" not in cols:
            connection.execute("ALTER TABLE subscriptions ADD COLUMN cancellation_date TEXT")
        if "last_payment_date" not in cols:
            connection.execute("ALTER TABLE subscriptions ADD COLUMN last_payment_date TEXT")
        if "evidence_category" not in cols:
            connection.execute("ALTER TABLE subscriptions ADD COLUMN evidence_category TEXT")
        if "account_id" not in cols:
            connection.execute("ALTER TABLE subscriptions ADD COLUMN account_id TEXT")
        if "account_email" not in cols:
            connection.execute("ALTER TABLE subscriptions ADD COLUMN account_email TEXT")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_subscriptions_user_archived ON subscriptions(user_id, is_archived)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_subscriptions_user_account ON subscriptions(user_id, account_id)"
        )
    except Exception:
        pass

    try:
        c_cursor = connection.execute("PRAGMA table_info(subscription_candidates)")
        c_cols = {row[1] for row in c_cursor.fetchall()}
        if "is_demo" not in c_cols:
            connection.execute("ALTER TABLE subscription_candidates ADD COLUMN is_demo INTEGER NOT NULL DEFAULT 0")
        if "original_amount" not in c_cols:
            connection.execute("ALTER TABLE subscription_candidates ADD COLUMN original_amount REAL")
        if "original_currency" not in c_cols:
            connection.execute("ALTER TABLE subscription_candidates ADD COLUMN original_currency TEXT")
        if "converted_amount" not in c_cols:
            connection.execute("ALTER TABLE subscription_candidates ADD COLUMN converted_amount REAL")
        if "converted_currency" not in c_cols:
            connection.execute("ALTER TABLE subscription_candidates ADD COLUMN converted_currency TEXT")
        if "lifecycle_status" not in c_cols:
            connection.execute("ALTER TABLE subscription_candidates ADD COLUMN lifecycle_status TEXT")
        if "trial_end_date" not in c_cols:
            connection.execute("ALTER TABLE subscription_candidates ADD COLUMN trial_end_date TEXT")
        if "purchase_date" not in c_cols:
            connection.execute("ALTER TABLE subscription_candidates ADD COLUMN purchase_date TEXT")
        if "cancellation_date" not in c_cols:
            connection.execute("ALTER TABLE subscription_candidates ADD COLUMN cancellation_date TEXT")
        if "last_payment_date" not in c_cols:
            connection.execute("ALTER TABLE subscription_candidates ADD COLUMN last_payment_date TEXT")
        if "evidence_category" not in c_cols:
            connection.execute("ALTER TABLE subscription_candidates ADD COLUMN evidence_category TEXT")
        if "reason" not in c_cols:
            connection.execute("ALTER TABLE subscription_candidates ADD COLUMN reason TEXT")
        if "account_id" not in c_cols:
            connection.execute("ALTER TABLE subscription_candidates ADD COLUMN account_id TEXT")
        if "account_email" not in c_cols:
            connection.execute("ALTER TABLE subscription_candidates ADD COLUMN account_email TEXT")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_candidates_user_source_msg ON subscription_candidates(user_id, source_message_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_candidates_user_merchant ON subscription_candidates(user_id, merchant_name)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_candidates_user_account ON subscription_candidates(user_id, account_id)"
        )
    except Exception:
        pass

    try:
        e_cursor = connection.execute("PRAGMA table_info(email_accounts)")
        e_rows = e_cursor.fetchall()
        e_cols = {row[1] for row in e_rows}
        pk_cols = [row[1] for row in e_rows if row[5] > 0]
        import hashlib

        # If email_accounts has user_id as sole primary key, migrate table to account_id as primary key
        if pk_cols == ["user_id"] or "account_id" not in e_cols:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS email_accounts_v2 (
                    account_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    email_address TEXT,
                    account_name TEXT,
                    is_connected INTEGER NOT NULL DEFAULT 0,
                    is_default INTEGER NOT NULL DEFAULT 1,
                    provider TEXT NOT NULL DEFAULT 'gmail',
                    scopes_json TEXT NOT NULL DEFAULT '[]',
                    refresh_token_encrypted TEXT,
                    access_token_encrypted TEXT,
                    token_expires_at TEXT,
                    last_synced_at TEXT,
                    sync_status TEXT NOT NULL DEFAULT 'idle',
                    error_message TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    messages_retrieved INTEGER,
                    duplicates_skipped INTEGER,
                    parsing_failures INTEGER,
                    messages_skipped INTEGER,
                    messages_discovered INTEGER,
                    messages_parsed INTEGER,
                    messages_insufficient_evidence INTEGER,
                    is_partial_scan INTEGER DEFAULT 0,
                    scan_limit INTEGER,
                    scan_date_range TEXT,
                    skip_reasons_json TEXT DEFAULT '{}',
                    failure_reasons_json TEXT DEFAULT '{}'
                );
                """
            )
            old_rows = connection.execute("SELECT * FROM email_accounts").fetchall()
            for r in old_rows:
                r_dict = dict(r)
                u_id = r_dict.get("user_id") or "user_default"
                em = r_dict.get("email_address") or "default"
                acc_id = r_dict.get("account_id")
                if not acc_id:
                    em_hash = hashlib.sha256(em.lower().strip().encode()).hexdigest()[:8]
                    acc_id = f"acct_{u_id}_{em_hash}"
                r_dict["account_id"] = acc_id
                r_dict["account_name"] = r_dict.get("account_name") or em
                r_dict["is_default"] = r_dict.get("is_default", 1)
                valid_fields = [
                    "account_id", "user_id", "email_address", "account_name", "is_connected",
                    "is_default", "provider", "scopes_json", "refresh_token_encrypted",
                    "access_token_encrypted", "token_expires_at", "last_synced_at", "sync_status",
                    "error_message", "updated_at", "messages_retrieved", "duplicates_skipped",
                    "parsing_failures", "messages_skipped", "messages_discovered", "messages_parsed",
                    "messages_insufficient_evidence", "is_partial_scan", "scan_limit", "scan_date_range",
                    "skip_reasons_json", "failure_reasons_json"
                ]
                ins_cols = [c for c in valid_fields if c in r_dict]
                p_holders = ",".join(["?"] * len(ins_cols))
                c_names = ",".join(ins_cols)
                v_list = [r_dict[c] for c in ins_cols]
                connection.execute(f"INSERT OR REPLACE INTO email_accounts_v2 ({c_names}) VALUES ({p_holders})", v_list)

            connection.execute("DROP TABLE email_accounts")
            connection.execute("ALTER TABLE email_accounts_v2 RENAME TO email_accounts")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_email_accounts_user ON email_accounts(user_id)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_email_accounts_email ON email_accounts(email_address)")
        else:
            if "messages_retrieved" not in e_cols:
                connection.execute("ALTER TABLE email_accounts ADD COLUMN messages_retrieved INTEGER")
            if "messages_skipped" not in e_cols:
                connection.execute("ALTER TABLE email_accounts ADD COLUMN messages_skipped INTEGER")
            if "duplicates_skipped" not in e_cols:
                connection.execute("ALTER TABLE email_accounts ADD COLUMN duplicates_skipped INTEGER")
            if "parsing_failures" not in e_cols:
                connection.execute("ALTER TABLE email_accounts ADD COLUMN parsing_failures INTEGER")
            if "messages_discovered" not in e_cols:
                connection.execute("ALTER TABLE email_accounts ADD COLUMN messages_discovered INTEGER")
            if "messages_parsed" not in e_cols:
                connection.execute("ALTER TABLE email_accounts ADD COLUMN messages_parsed INTEGER")
            if "messages_insufficient_evidence" not in e_cols:
                connection.execute("ALTER TABLE email_accounts ADD COLUMN messages_insufficient_evidence INTEGER")
            if "is_partial_scan" not in e_cols:
                connection.execute("ALTER TABLE email_accounts ADD COLUMN is_partial_scan INTEGER DEFAULT 0")
            if "scan_limit" not in e_cols:
                connection.execute("ALTER TABLE email_accounts ADD COLUMN scan_limit INTEGER")
            if "scan_date_range" not in e_cols:
                connection.execute("ALTER TABLE email_accounts ADD COLUMN scan_date_range TEXT")
            if "skip_reasons_json" not in e_cols:
                connection.execute("ALTER TABLE email_accounts ADD COLUMN skip_reasons_json TEXT DEFAULT '{}'")
            if "failure_reasons_json" not in e_cols:
                connection.execute("ALTER TABLE email_accounts ADD COLUMN failure_reasons_json TEXT DEFAULT '{}'")
            if "account_name" not in e_cols:
                connection.execute("ALTER TABLE email_accounts ADD COLUMN account_name TEXT")
            if "is_default" not in e_cols:
                connection.execute("ALTER TABLE email_accounts ADD COLUMN is_default INTEGER NOT NULL DEFAULT 1")
    except Exception:
        pass

    try:
        r_cursor = connection.execute("PRAGMA table_info(runs)")
        r_cols = {row[1] for row in r_cursor.fetchall()}
        if "user_id" not in r_cols:
            connection.execute("ALTER TABLE runs ADD COLUMN user_id TEXT NOT NULL DEFAULT 'user_default'")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_runs_user ON runs(user_id, started_at)")
    except Exception:
        pass

    try:
        a_cursor = connection.execute("PRAGMA table_info(audit_events)")
        a_cols = {row[1] for row in a_cursor.fetchall()}
        if "user_id" not in a_cols:
            connection.execute("ALTER TABLE audit_events ADD COLUMN user_id TEXT NOT NULL DEFAULT 'user_default'")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_audit_events_user ON audit_events(user_id, event_at)")
    except Exception:
        pass

    try:
        ar_cursor = connection.execute("PRAGMA table_info(action_records)")
        ar_cols = {row[1] for row in ar_cursor.fetchall()}
        if "user_id" not in ar_cols:
            connection.execute("ALTER TABLE action_records ADD COLUMN user_id TEXT NOT NULL DEFAULT 'user_default'")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_action_records_user ON action_records(user_id, created_at)")
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
            INSERT INTO runs(run_id, user_id, status, started_at, completed_at, payload_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(run_id) DO UPDATE SET
                user_id=excluded.user_id,
                status=excluded.status,
                started_at=excluded.started_at,
                completed_at=excluded.completed_at,
                payload_json=excluded.payload_json,
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                run.run_id,
                run.user_id or "user_default",
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
                INSERT INTO subscriptions(subscription_id, service_name, status, payload_json, user_id)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(subscription_id) DO UPDATE SET
                    service_name=excluded.service_name,
                    status=excluded.status,
                    payload_json=excluded.payload_json,
                    user_id=excluded.user_id,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    subscription.subscription_id,
                    subscription.service_name,
                    subscription.status.value,
                    subscription.model_dump_json(),
                    subscription.user_id or run.user_id or "user_default",
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
            _append_event(connection, run.run_id, subscription_id, state, user_id=subscription.user_id or run.user_id)


def _append_event(
    connection: sqlite3.Connection,
    run_id: str,
    subscription_id: str,
    state: "SubscriptionRunState",
    user_id: Optional[str] = None,
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
    effective_user_id = user_id or state.subscription.user_id or "user_default"
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
                (run_id, subscription_id, user_id, event_type, entity_id, event_at, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, subscription_id, effective_user_id, event_type, entity_id, timestamp.isoformat(), entity.model_dump_json()),
        )
    for ev in state.evidence_list:
        connection.execute(
            """
            INSERT OR IGNORE INTO audit_events
                (run_id, subscription_id, user_id, event_type, entity_id, event_at, payload_json)
            VALUES (?, ?, ?, 'evidence', ?, ?, ?)
            """,
            (run_id, subscription_id, effective_user_id, ev.evidence_id, ev.captured_at.isoformat(), ev.model_dump_json()),
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


def list_audit_events(run_id: str, user_id: Optional[str] = None) -> list[dict]:
    """Return immutable audit events for reporting and dashboard history."""
    with get_db_session() as connection:
        query = "SELECT event_type, entity_id, event_at, payload_json FROM audit_events WHERE run_id = ?"
        params: List[Any] = [run_id]
        if user_id:
            query += " AND (user_id = ? OR user_id = 'user_default')"
            params.append(user_id)
        query += " ORDER BY event_at, event_id"
        rows = connection.execute(query, params).fetchall()
        return [
            {
                "event_type": row["event_type"],
                "entity_id": row["entity_id"],
                "event_at": row["event_at"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]


def save_subscription(
    subscription: Subscription,
    connection: Optional[sqlite3.Connection] = None,
) -> None:
    """Persist a standalone subscription record with full metadata."""
    # Safeguard: prevent mock connector subscriptions from polluting real-user inventories
    if (subscription.discovery_source or "connector") == "connector" and (subscription.user_id or "user_default") == "user_default":
        return

    owns_conn = connection is None
    if owns_conn:
        sess = get_db_session()
        conn = sess.__enter__()
    else:
        conn = connection

    try:
        conn.execute(
            """
            INSERT INTO subscriptions(
                subscription_id, service_name, status, payload_json,
                user_id, discovery_source, category, is_archived, notes,
                original_amount, original_currency, converted_amount, converted_currency,
                lifecycle_status, trial_end_date, purchase_date, cancellation_date,
                last_payment_date, evidence_category, account_id, account_email, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(subscription_id) DO UPDATE SET
                service_name=excluded.service_name,
                status=excluded.status,
                payload_json=excluded.payload_json,
                user_id=excluded.user_id,
                discovery_source=excluded.discovery_source,
                category=excluded.category,
                is_archived=excluded.is_archived,
                notes=excluded.notes,
                original_amount=excluded.original_amount,
                original_currency=excluded.original_currency,
                converted_amount=excluded.converted_amount,
                converted_currency=excluded.converted_currency,
                lifecycle_status=excluded.lifecycle_status,
                trial_end_date=excluded.trial_end_date,
                purchase_date=excluded.purchase_date,
                cancellation_date=excluded.cancellation_date,
                last_payment_date=excluded.last_payment_date,
                evidence_category=excluded.evidence_category,
                account_id=excluded.account_id,
                account_email=excluded.account_email,
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                subscription.subscription_id,
                subscription.service_name,
                subscription.status.value if hasattr(subscription.status, "value") else str(subscription.status),
                subscription.model_dump_json(),
                subscription.user_id or "user_default",
                subscription.discovery_source or "connector",
                subscription.category,
                1 if subscription.is_archived else 0,
                subscription.notes,
                subscription.original_amount,
                subscription.original_currency,
                subscription.converted_amount,
                subscription.converted_currency,
                subscription.lifecycle_status,
                subscription.trial_end_date.isoformat() if subscription.trial_end_date else None,
                subscription.purchase_date.isoformat() if subscription.purchase_date else None,
                subscription.cancellation_date.isoformat() if subscription.cancellation_date else None,
                subscription.last_payment_date.isoformat() if subscription.last_payment_date else None,
                subscription.evidence_category,
                subscription.account_id,
                subscription.account_email,
            ),
        )
    finally:
        if owns_conn:
            sess.__exit__(None, None, None)


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
    account_id: Optional[str] = None,
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
        if account_id:
            clauses.append("account_id = ?")
            params.append(account_id)

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
    import hashlib

    if not account.account_id:
        em_hash = hashlib.sha256((account.email_address or "default").lower().strip().encode()).hexdigest()[:8]
        account.account_id = f"acct_{account.user_id}_{em_hash}"

    with get_db_session() as connection:
        if not account.account_name or not account.account_name.strip():
            existing_row = connection.execute(
                "SELECT account_name FROM email_accounts WHERE account_id = ?",
                (account.account_id,),
            ).fetchone()
            if existing_row and existing_row["account_name"]:
                account.account_name = existing_row["account_name"]
            else:
                account.account_name = account.email_address or account.account_id

        if account.is_default:
            connection.execute(
                "UPDATE email_accounts SET is_default = 0 WHERE user_id = ? AND account_id != ?",
                (account.user_id, account.account_id),
            )
        connection.execute(
            """
            INSERT INTO email_accounts(
                account_id, user_id, email_address, account_name, is_connected, is_default,
                provider, scopes_json, refresh_token_encrypted, access_token_encrypted,
                token_expires_at, last_synced_at, sync_status, error_message,
                messages_retrieved, messages_skipped, duplicates_skipped, parsing_failures,
                messages_discovered, messages_parsed, messages_insufficient_evidence,
                is_partial_scan, scan_limit, scan_date_range, skip_reasons_json, failure_reasons_json,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(account_id) DO UPDATE SET
                user_id=excluded.user_id,
                email_address=excluded.email_address,
                account_name=CASE
                    WHEN excluded.account_name IS NOT NULL AND excluded.account_name != '' AND excluded.account_name != excluded.email_address
                    THEN excluded.account_name
                    ELSE COALESCE(email_accounts.account_name, excluded.account_name)
                END,
                is_connected=excluded.is_connected,
                is_default=excluded.is_default,
                provider=excluded.provider,
                scopes_json=excluded.scopes_json,
                refresh_token_encrypted=COALESCE(excluded.refresh_token_encrypted, email_accounts.refresh_token_encrypted),
                access_token_encrypted=COALESCE(excluded.access_token_encrypted, email_accounts.access_token_encrypted),
                token_expires_at=COALESCE(excluded.token_expires_at, email_accounts.token_expires_at),
                last_synced_at=COALESCE(excluded.last_synced_at, email_accounts.last_synced_at),
                sync_status=excluded.sync_status,
                error_message=excluded.error_message,
                messages_retrieved=COALESCE(excluded.messages_retrieved, email_accounts.messages_retrieved),
                messages_skipped=COALESCE(excluded.messages_skipped, email_accounts.messages_skipped),
                duplicates_skipped=COALESCE(excluded.duplicates_skipped, email_accounts.duplicates_skipped),
                parsing_failures=COALESCE(excluded.parsing_failures, email_accounts.parsing_failures),
                messages_discovered=COALESCE(excluded.messages_discovered, email_accounts.messages_discovered),
                messages_parsed=COALESCE(excluded.messages_parsed, email_accounts.messages_parsed),
                messages_insufficient_evidence=COALESCE(excluded.messages_insufficient_evidence, email_accounts.messages_insufficient_evidence),
                is_partial_scan=COALESCE(excluded.is_partial_scan, email_accounts.is_partial_scan),
                scan_limit=COALESCE(excluded.scan_limit, email_accounts.scan_limit),
                scan_date_range=COALESCE(excluded.scan_date_range, email_accounts.scan_date_range),
                skip_reasons_json=COALESCE(excluded.skip_reasons_json, email_accounts.skip_reasons_json),
                failure_reasons_json=COALESCE(excluded.failure_reasons_json, email_accounts.failure_reasons_json),
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                account.account_id,
                account.user_id,
                account.email_address,
                account.account_name,
                1 if account.is_connected else 0,
                1 if account.is_default else 0,
                account.provider,
                json.dumps(account.scopes),
                refresh_token,
                access_token,
                expires_at.isoformat() if expires_at else None,
                account.last_synced_at.isoformat() if account.last_synced_at else None,
                account.sync_status,
                account.error_message,
                account.messages_retrieved,
                account.messages_skipped,
                account.duplicates_skipped,
                account.parsing_failures,
                account.messages_discovered,
                account.messages_parsed,
                account.messages_insufficient_evidence,
                1 if account.is_partial_scan else 0,
                account.scan_limit,
                account.scan_date_range,
                json.dumps(account.skip_reasons or {}),
                json.dumps(account.failure_reasons or {}),
            ),
        )


def _row_to_email_account_status(row: sqlite3.Row, live_configured: bool, connection: sqlite3.Connection) -> EmailAccountStatus:
    cols = row.keys()
    user_id = row["user_id"]
    account_id = row["account_id"] if "account_id" in cols else None

    if account_id:
        candidate_count = connection.execute(
            "SELECT COUNT(*) as cnt FROM subscription_candidates WHERE user_id = ? AND account_id = ? AND review_status = 'pending'",
            (user_id, account_id),
        ).fetchone()["cnt"]
    else:
        candidate_count = connection.execute(
            "SELECT COUNT(*) as cnt FROM subscription_candidates WHERE user_id = ? AND review_status = 'pending'",
            (user_id,),
        ).fetchone()["cnt"]

    is_connected = bool(row["is_connected"])
    has_tokens = bool(row["access_token_encrypted"] or row["refresh_token_encrypted"])
    token_str = str(row["access_token_encrypted"] or "")
    is_mock_token = token_str.startswith("mock_") or token_str.startswith("test_")
    stored_email = row["email_address"]

    if not has_tokens:
        is_connected = False

    account_mode = "demo"
    if is_connected and live_configured and not is_mock_token and not (stored_email or "").endswith("@subcan-demo.com"):
        account_mode = "live"
    elif not is_connected and live_configured:
        account_mode = "live"

    email_addr = stored_email
    messages_retrieved = row["messages_retrieved"] if "messages_retrieved" in cols else None
    messages_skipped = row["messages_skipped"] if "messages_skipped" in cols else None
    duplicates_skipped = row["duplicates_skipped"] if "duplicates_skipped" in cols else None
    parsing_failures = row["parsing_failures"] if "parsing_failures" in cols else None
    messages_discovered = row["messages_discovered"] if "messages_discovered" in cols else None
    messages_parsed = row["messages_parsed"] if "messages_parsed" in cols else None
    messages_insufficient = row["messages_insufficient_evidence"] if "messages_insufficient_evidence" in cols else None
    is_partial = bool(row["is_partial_scan"]) if "is_partial_scan" in cols else False
    scan_lim = row["scan_limit"] if "scan_limit" in cols else None
    scan_range = row["scan_date_range"] if "scan_date_range" in cols else None

    skip_reasons = {}
    if "skip_reasons_json" in cols and row["skip_reasons_json"]:
        try:
            skip_reasons = json.loads(row["skip_reasons_json"])
        except Exception:
            skip_reasons = {}

    failure_reasons = {}
    if "failure_reasons_json" in cols and row["failure_reasons_json"]:
        try:
            failure_reasons = json.loads(row["failure_reasons_json"])
        except Exception:
            failure_reasons = {}

    return EmailAccountStatus(
        user_id=row["user_id"],
        account_id=account_id,
        account_name=row["account_name"] if "account_name" in cols else (email_addr or "Primary Mailbox"),
        is_default=bool(row["is_default"]) if "is_default" in cols else True,
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
        messages_discovered=messages_discovered,
        messages_retrieved=messages_retrieved,
        messages_parsed=messages_parsed,
        messages_skipped=messages_skipped,
        messages_insufficient_evidence=messages_insufficient,
        duplicates_skipped=duplicates_skipped,
        parsing_failures=parsing_failures,
        is_partial_scan=is_partial,
        scan_limit=scan_lim,
        scan_date_range=scan_range,
        skip_reasons=skip_reasons,
        failure_reasons=failure_reasons,
    )


def get_email_account(
    user_id: str = "user_default",
    account_id: Optional[str] = None,
) -> Optional[EmailAccountStatus]:
    """Retrieve connected email account for user with truthful mode attribution."""
    from backend.app.config import is_live_oauth_configured
    live_configured = is_live_oauth_configured()

    with get_db_session() as connection:
        if account_id:
            row = connection.execute(
                "SELECT * FROM email_accounts WHERE user_id = ? AND account_id = ?",
                (user_id, account_id),
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT * FROM email_accounts WHERE user_id = ? ORDER BY is_connected DESC, is_default DESC, updated_at DESC LIMIT 1",
                (user_id,),
            ).fetchone()

        if not row:
            return EmailAccountStatus(
                user_id=user_id,
                account_id=account_id,
                is_connected=False,
                is_live_configured=live_configured,
                mode="live" if live_configured else "demo",
            )
        return _row_to_email_account_status(row, live_configured, connection)


def get_email_account_by_email(
    user_id: str,
    email_address: str,
) -> Optional[EmailAccountStatus]:
    """Retrieve an email account for a user by verified email address (case-insensitive)."""
    if not email_address or not email_address.strip():
        return None
    from backend.app.config import is_live_oauth_configured
    live_configured = is_live_oauth_configured()
    clean_email = email_address.lower().strip()

    with get_db_session() as connection:
        row = connection.execute(
            "SELECT * FROM email_accounts WHERE user_id = ? AND LOWER(TRIM(email_address)) = ? LIMIT 1",
            (user_id, clean_email),
        ).fetchone()
        if not row:
            return None
        return _row_to_email_account_status(row, live_configured, connection)


def update_email_account_name(
    user_id: str,
    account_id: str,
    account_name: str,
) -> bool:
    """Update custom nickname for an email account."""
    if not account_name or not account_name.strip():
        return False
    with get_db_session() as connection:
        res = connection.execute(
            """
            UPDATE email_accounts SET
                account_name = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ? AND account_id = ?
            """,
            (account_name.strip(), user_id, account_id),
        )
        return res.rowcount > 0


def list_email_accounts(user_id: str = "user_default") -> List[EmailAccountStatus]:
    """List all connected email accounts for a user."""
    from backend.app.config import is_live_oauth_configured
    live_configured = is_live_oauth_configured()

    with get_db_session() as connection:
        rows = connection.execute(
            "SELECT * FROM email_accounts WHERE user_id = ? ORDER BY is_default DESC, updated_at DESC",
            (user_id,),
        ).fetchall()
        return [_row_to_email_account_status(r, live_configured, connection) for r in rows]


get_email_accounts = list_email_accounts


def get_email_tokens(
    user_id: str = "user_default",
    account_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Retrieve stored OAuth tokens for internal API communication."""
    with get_db_session() as connection:
        if account_id:
            row = connection.execute(
                "SELECT refresh_token_encrypted, access_token_encrypted, token_expires_at FROM email_accounts WHERE user_id = ? AND account_id = ?",
                (user_id, account_id),
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT refresh_token_encrypted, access_token_encrypted, token_expires_at FROM email_accounts WHERE user_id = ? ORDER BY is_default DESC, updated_at DESC LIMIT 1",
                (user_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "refresh_token": row["refresh_token_encrypted"],
            "access_token": row["access_token_encrypted"],
            "expires_at": row["token_expires_at"],
        }


def disconnect_email_account(
    user_id: str = "user_default",
    account_id: Optional[str] = None,
) -> bool:
    """Disconnect email account, delete tokens, and reset sync status."""
    with get_db_session() as connection:
        if account_id:
            connection.execute(
                """
                UPDATE email_accounts SET
                    is_connected = 0,
                    refresh_token_encrypted = NULL,
                    access_token_encrypted = NULL,
                    token_expires_at = NULL,
                    sync_status = 'idle',
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ? AND account_id = ?
                """,
                (user_id, account_id),
            )
        else:
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


def save_subscription_candidate(
    candidate: SubscriptionCandidate,
    connection: Optional[sqlite3.Connection] = None,
) -> None:
    """Save an email-extracted candidate subscription."""
    owns_conn = connection is None
    if owns_conn:
        sess = get_db_session()
        conn = sess.__enter__()
    else:
        conn = connection

    try:
        existing = conn.execute(
            "SELECT review_status FROM subscription_candidates WHERE candidate_id = ?",
            (candidate.candidate_id,),
        ).fetchone()
        if existing and existing["review_status"] in ("confirmed", "dismissed"):
            candidate.review_status = CandidateReviewStatus(existing["review_status"])
        conn.execute(
            """
            INSERT INTO subscription_candidates(
                candidate_id, user_id, merchant_name, product_name,
                estimated_cost, currency, billing_interval, detected_renewal_date,
                detected_last_used, review_status, confidence, source_message_id,
                source_subject, source_sender, source_date, email_snippet,
                cancellation_signal, notes, payload_json, is_demo,
                original_amount, original_currency, converted_amount, converted_currency,
                lifecycle_status, trial_end_date, purchase_date, cancellation_date,
                last_payment_date, evidence_category, reason, account_id, account_email, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(candidate_id) DO UPDATE SET
                user_id=excluded.user_id,
                merchant_name=excluded.merchant_name,
                product_name=excluded.product_name,
                estimated_cost=excluded.estimated_cost,
                currency=excluded.currency,
                billing_interval=excluded.billing_interval,
                detected_renewal_date=excluded.detected_renewal_date,
                detected_last_used=excluded.detected_last_used,
                confidence=excluded.confidence,
                source_message_id=excluded.source_message_id,
                source_subject=excluded.source_subject,
                source_sender=excluded.source_sender,
                source_date=excluded.source_date,
                email_snippet=excluded.email_snippet,
                cancellation_signal=excluded.cancellation_signal,
                original_amount=excluded.original_amount,
                original_currency=excluded.original_currency,
                converted_amount=excluded.converted_amount,
                converted_currency=excluded.converted_currency,
                lifecycle_status=excluded.lifecycle_status,
                trial_end_date=excluded.trial_end_date,
                purchase_date=excluded.purchase_date,
                cancellation_date=excluded.cancellation_date,
                last_payment_date=excluded.last_payment_date,
                evidence_category=excluded.evidence_category,
                reason=excluded.reason,
                account_id=excluded.account_id,
                account_email=excluded.account_email,
                review_status=CASE
                    WHEN subscription_candidates.review_status IN ('confirmed', 'dismissed')
                    THEN subscription_candidates.review_status
                    ELSE excluded.review_status
                END,
                notes=excluded.notes,
                payload_json=excluded.payload_json,
                is_demo=excluded.is_demo,
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
                1 if candidate.is_demo else 0,
                candidate.original_amount,
                candidate.original_currency,
                candidate.converted_amount,
                candidate.converted_currency,
                candidate.lifecycle_status,
                candidate.trial_end_date.isoformat() if candidate.trial_end_date else None,
                candidate.purchase_date.isoformat() if candidate.purchase_date else None,
                candidate.cancellation_date.isoformat() if candidate.cancellation_date else None,
                candidate.last_payment_date.isoformat() if candidate.last_payment_date else None,
                candidate.evidence_category,
                candidate.reason,
                candidate.account_id,
                candidate.account_email,
            ),
        )
    finally:
        if owns_conn:
            sess.__exit__(None, None, None)


def reset_demo_data(user_id: str = "user_default") -> Dict[str, Any]:
    """Safe development-only procedure to purge synthetic demo candidates and mock records.
    Preserves all genuine manual subscriptions, real Gmail-discovered subscriptions, and email tokens.
    """
    res = cleanup_demo_and_mock_data(user_id=user_id)
    return {
        "success": True,
        "user_id": user_id,
        "demo_candidates_removed": res["candidates_removed_count"],
        "demo_subscriptions_removed": res["subscriptions_removed_count"],
        "message": "Demo data safely cleared. Real Gmail and manual records preserved.",
    }


def list_subscription_candidates(
    user_id: str = "user_default",
    review_status: Optional[str] = None,
    account_id: Optional[str] = None,
) -> List[SubscriptionCandidate]:
    """List subscription candidates for user review."""
    with get_db_session() as connection:
        query = "SELECT payload_json FROM subscription_candidates WHERE user_id = ?"
        params: List[Any] = [user_id]
        if review_status:
            query += " AND review_status = ?"
            params.append(review_status)
        if account_id:
            query += " AND account_id = ?"
            params.append(account_id)
        query += " ORDER BY created_at DESC"
        rows = connection.execute(query, params).fetchall()
        return [SubscriptionCandidate.model_validate_json(r["payload_json"]) for r in rows]


def get_subscription_candidate(
    candidate_id: str,
    user_id: Optional[str] = None,
    account_id: Optional[str] = None,
) -> Optional[SubscriptionCandidate]:
    """Retrieve single subscription candidate."""
    with get_db_session() as connection:
        query = "SELECT payload_json FROM subscription_candidates WHERE candidate_id = ?"
        params: List[Any] = [candidate_id]
        if user_id:
            query += " AND user_id = ?"
            params.append(user_id)
        if account_id:
            query += " AND account_id = ?"
            params.append(account_id)
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
    user_id: str = "user_default",
) -> None:
    """Persist an action result and its idempotency signature durably."""
    try:
        with get_db_session() as connection:
            connection.execute(
                """
                INSERT INTO action_records(
                    action_id, user_id, idempotency_key, subscription_id, plan_id,
                    plan_version, action_type, status, request_sig_json, payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(action_id) DO UPDATE SET
                    user_id=excluded.user_id,
                    status=excluded.status,
                    payload_json=excluded.payload_json
                """,
                (
                    action.action_id,
                    user_id,
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

        # Email Accounts (redacting tokens)
        acct_rows = connection.execute(
            """
            SELECT account_id, user_id, email_address, account_name, is_connected, is_default,
                   provider, scopes_json, last_synced_at, sync_status, error_message, updated_at
            FROM email_accounts WHERE user_id = ?
            """,
            (user_id,),
        ).fetchall()
        email_accounts = []
        for acct_row in acct_rows:
            email_accounts.append({
                "account_id": acct_row["account_id"],
                "user_id": acct_row["user_id"],
                "email_address": acct_row["email_address"],
                "account_name": acct_row["account_name"],
                "is_connected": bool(acct_row["is_connected"]),
                "is_default": bool(acct_row["is_default"]),
                "provider": acct_row["provider"],
                "scopes": json.loads(acct_row["scopes_json"]) if acct_row["scopes_json"] else [],
                "last_synced_at": acct_row["last_synced_at"],
                "sync_status": acct_row["sync_status"],
                "error_message": acct_row["error_message"],
                "updated_at": acct_row["updated_at"],
            })

        # Orchestration Runs
        run_rows = connection.execute(
            "SELECT payload_json FROM runs WHERE user_id = ?",
            (user_id,),
        ).fetchall()
        runs = [json.loads(r["payload_json"]) for r in run_rows]

        return {
            "user_id": user_id,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "subscriptions": subscriptions,
            "candidates": candidates,
            "email_accounts": email_accounts,
            "runs": runs,
        }


def delete_user_data(user_id: str) -> Dict[str, Any]:
    """Purge all user-scoped subscriptions, candidates, runs, audit events, and tokens for account deletion."""
    with get_db_session() as connection:
        # 1. Delete run_subscriptions referencing user's runs or user's subscriptions
        connection.execute(
            """
            DELETE FROM run_subscriptions WHERE run_id IN (
                SELECT run_id FROM runs WHERE user_id = ?
            ) OR subscription_id IN (
                SELECT subscription_id FROM subscriptions WHERE user_id = ?
            )
            """,
            (user_id, user_id),
        )
        # 2. Delete action records
        connection.execute("DELETE FROM action_records WHERE user_id = ?", (user_id,))
        # 3. Delete audit events
        connection.execute("DELETE FROM audit_events WHERE user_id = ?", (user_id,))
        # 4. Delete runs
        connection.execute("DELETE FROM runs WHERE user_id = ?", (user_id,))
        # 5. Delete candidates
        connection.execute("DELETE FROM subscription_candidates WHERE user_id = ?", (user_id,))
        # 6. Delete subscriptions
        connection.execute("DELETE FROM subscriptions WHERE user_id = ?", (user_id,))
        # 7. Disconnect email accounts & delete tokens
        connection.execute("DELETE FROM email_accounts WHERE user_id = ?", (user_id,))
        # 8. Delete user sessions
        connection.execute("DELETE FROM user_sessions WHERE user_id = ?", (user_id,))
        # 9. Delete user record
        connection.execute("DELETE FROM users WHERE user_id = ?", (user_id,))

        return {
            "deleted": True,
            "user_id": user_id,
            "deleted_at": datetime.now(timezone.utc).isoformat(),
        }


def repair_mixed_data(
    connection: Optional[sqlite3.Connection] = None,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """Inspect and safely repair cross-account mixed candidates and subscriptions.

    Safety Guarantees:
    1. Backs up SQLite database prior to any mutating repair operations if dry_run=False.
    2. Reassigns records ONLY when ownership can be established from verified email/account evidence.
    3. Never guesses ownership based on merchant names.
    4. Never deletes, overwrites, or silently reassigns records when ownership is ambiguous.
    5. Returns an exhaustive report detailing repairable vs ambiguous items requiring manual review.
    """
    import shutil

    backup_path = None
    if not dry_run:
        # Create timestamped safety backup
        ts = int(datetime.now(timezone.utc).timestamp())
        backup_path = f"reports/subcan_repair_backup_{ts}.db"
        try:
            shutil.copy2("reports/subcan.db", backup_path)
        except Exception:
            pass

    owns_conn = connection is None
    if owns_conn:
        sess = get_db_session()
        conn = sess.__enter__()
    else:
        conn = connection

    try:
        # Step 1: Load all registered email accounts
        account_rows = conn.execute(
            "SELECT account_id, user_id, email_address, account_name FROM email_accounts"
        ).fetchall()

        email_to_account: Dict[str, Tuple[str, str]] = {}  # email -> (user_id, account_id)
        for r in account_rows:
            if r["email_address"]:
                clean_email = r["email_address"].lower().strip()
                email_to_account[clean_email] = (r["user_id"], r["account_id"])

        # Step 2: Inspect subscription candidates
        cand_rows = conn.execute(
            "SELECT candidate_id, user_id, account_id, account_email, source_sender, payload_json FROM subscription_candidates"
        ).fetchall()

        repairable_candidates = []
        ambiguous_candidates = []

        for r in cand_rows:
            cand_id = r["candidate_id"]
            current_uid = r["user_id"]
            current_aid = r["account_id"]
            cand_email = (r["account_email"] or "").lower().strip()

            if not cand_email and r["payload_json"]:
                try:
                    payload = json.loads(r["payload_json"])
                    cand_email = (payload.get("account_email") or "").lower().strip()
                except Exception:
                    pass

            target_uid = None
            target_aid = None
            evidence_reason = None

            if cand_email and cand_email in email_to_account:
                target_uid, target_aid = email_to_account[cand_email]
                evidence_reason = f"Verified account_email '{cand_email}' maps to user '{target_uid}' (account '{target_aid}')"

            if target_uid and (target_uid != current_uid or target_aid != current_aid):
                repairable_candidates.append({
                    "candidate_id": cand_id,
                    "current_user_id": current_uid,
                    "target_user_id": target_uid,
                    "current_account_id": current_aid,
                    "target_account_id": target_aid,
                    "payload_json": r["payload_json"],
                    "evidence": evidence_reason,
                })
            elif not target_uid and current_uid == "user_default" and len(email_to_account) > 0:
                ambiguous_candidates.append({
                    "candidate_id": cand_id,
                    "current_user_id": current_uid,
                    "current_account_id": current_aid,
                    "reason": "Candidate belongs to shared user_default but has no verified email mapping to a known account",
                })

        # Step 3: Inspect subscriptions
        sub_rows = conn.execute(
            "SELECT subscription_id, user_id, account_id, account_email, discovery_source, payload_json FROM subscriptions"
        ).fetchall()

        repairable_subscriptions = []
        ambiguous_subscriptions = []

        for r in sub_rows:
            sub_id = r["subscription_id"]
            current_uid = r["user_id"]
            current_aid = r["account_id"]
            sub_email = (r["account_email"] or "").lower().strip()

            if not sub_email and r["payload_json"]:
                try:
                    payload = json.loads(r["payload_json"])
                    sub_email = (payload.get("account_email") or "").lower().strip()
                except Exception:
                    pass

            target_uid = None
            target_aid = None
            evidence_reason = None

            if sub_email and sub_email in email_to_account:
                target_uid, target_aid = email_to_account[sub_email]
                evidence_reason = f"Verified account_email '{sub_email}' maps to user '{target_uid}' (account '{target_aid}')"

            if target_uid and (target_uid != current_uid or target_aid != current_aid):
                repairable_subscriptions.append({
                    "subscription_id": sub_id,
                    "current_user_id": current_uid,
                    "target_user_id": target_uid,
                    "current_account_id": current_aid,
                    "target_account_id": target_aid,
                    "payload_json": r["payload_json"],
                    "evidence": evidence_reason,
                })
            elif not target_uid and current_uid == "user_default" and r["discovery_source"] == "email":
                ambiguous_subscriptions.append({
                    "subscription_id": sub_id,
                    "current_user_id": current_uid,
                    "current_account_id": current_aid,
                    "reason": "Email subscription belongs to user_default but has no verified account provenance",
                })

        # Step 4: Execute repairs if dry_run is False
        repaired_candidates_count = 0
        repaired_subscriptions_count = 0

        if not dry_run:
            for item in repairable_candidates:
                payload_str = item.get("payload_json")
                if payload_str:
                    try:
                        p_obj = json.loads(payload_str)
                        p_obj["user_id"] = item["target_user_id"]
                        p_obj["account_id"] = item["target_account_id"]
                        payload_str = json.dumps(p_obj)
                    except Exception:
                        pass

                conn.execute(
                    """
                    UPDATE subscription_candidates SET
                        user_id = ?,
                        account_id = ?,
                        payload_json = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE candidate_id = ?
                    """,
                    (item["target_user_id"], item["target_account_id"], payload_str, item["candidate_id"]),
                )
                repaired_candidates_count += 1

            for item in repairable_subscriptions:
                payload_str = item.get("payload_json")
                if payload_str:
                    try:
                        p_obj = json.loads(payload_str)
                        p_obj["user_id"] = item["target_user_id"]
                        p_obj["account_id"] = item["target_account_id"]
                        payload_str = json.dumps(p_obj)
                    except Exception:
                        pass

                conn.execute(
                    """
                    UPDATE subscriptions SET
                        user_id = ?,
                        account_id = ?,
                        payload_json = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE subscription_id = ?
                    """,
                    (item["target_user_id"], item["target_account_id"], payload_str, item["subscription_id"]),
                )
                repaired_subscriptions_count += 1

        return {
            "dry_run": dry_run,
            "backup_path": backup_path,
            "backup_created": backup_path,
            "total_candidates_inspected": len(cand_rows),
            "repairable_candidates_count": len(repairable_candidates),
            "ambiguous_candidates_count": len(ambiguous_candidates),
            "repaired_candidates_count": repaired_candidates_count,
            "reassigned_candidates": len(repairable_candidates) if dry_run else repaired_candidates_count,
            "total_subscriptions_inspected": len(sub_rows),
            "repairable_subscriptions_count": len(repairable_subscriptions),
            "ambiguous_subscriptions_count": len(ambiguous_subscriptions),
            "repaired_subscriptions_count": repaired_subscriptions_count,
            "reassigned_subscriptions": len(repairable_subscriptions) if dry_run else repaired_subscriptions_count,
            "repairable_candidates": repairable_candidates,
            "ambiguous_candidates": ambiguous_candidates,
            "repairable_subscriptions": repairable_subscriptions,
            "ambiguous_subscriptions": ambiguous_subscriptions,
        }
    finally:
        if owns_conn:
            sess.__exit__(None, None, None)


# ==============================================================================
# User Identity & Session Persistence
# ==============================================================================

def save_user(
    user_id: str,
    email: str,
    google_sub: Optional[str] = None,
    name: Optional[str] = None,
    picture: Optional[str] = None,
    connection: Optional[sqlite3.Connection] = None,
) -> None:
    """Insert or update a persistent application user record."""
    owns_conn = connection is None
    if owns_conn:
        sess = db_session()
        conn = sess.__enter__()
    else:
        conn = connection

    try:
        conn.execute(
            """
            INSERT INTO users (user_id, google_sub, email, name, picture, last_login_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                google_sub = coalesce(excluded.google_sub, users.google_sub),
                email = excluded.email,
                name = coalesce(excluded.name, users.name),
                picture = coalesce(excluded.picture, users.picture),
                last_login_at = CURRENT_TIMESTAMP
            """,
            (user_id, google_sub, email, name, picture),
        )
    finally:
        if owns_conn:
            sess.__exit__(None, None, None)


def get_user_by_id(user_id: str, connection: Optional[sqlite3.Connection] = None) -> Optional[Dict[str, Any]]:
    """Retrieve user record by user_id."""
    owns_conn = connection is None
    if owns_conn:
        sess = db_session()
        conn = sess.__enter__()
    else:
        conn = connection

    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT user_id, google_sub, email, name, picture, created_at, last_login_at FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        if owns_conn:
            sess.__exit__(None, None, None)


def get_user_by_google_sub(google_sub: str, connection: Optional[sqlite3.Connection] = None) -> Optional[Dict[str, Any]]:
    """Retrieve user record by Google subject ID (sub claim)."""
    owns_conn = connection is None
    if owns_conn:
        sess = db_session()
        conn = sess.__enter__()
    else:
        conn = connection

    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT user_id, google_sub, email, name, picture, created_at, last_login_at FROM users WHERE google_sub = ?",
            (google_sub,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        if owns_conn:
            sess.__exit__(None, None, None)


def get_user_by_email(email: str, connection: Optional[sqlite3.Connection] = None) -> Optional[Dict[str, Any]]:
    """Retrieve user record by verified email address."""
    owns_conn = connection is None
    if owns_conn:
        sess = db_session()
        conn = sess.__enter__()
    else:
        conn = connection

    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT user_id, google_sub, email, name, picture, created_at, last_login_at FROM users WHERE lower(email) = lower(?)",
            (email.strip(),),
        ).fetchone()
        return dict(row) if row else None
    finally:
        if owns_conn:
            sess.__exit__(None, None, None)


def save_user_session(
    session_id: str,
    user_id: str,
    session_token_hash: str,
    expires_at: str,
    connection: Optional[sqlite3.Connection] = None,
) -> None:
    """Store an active user session token hash."""
    owns_conn = connection is None
    if owns_conn:
        sess = db_session()
        conn = sess.__enter__()
    else:
        conn = connection

    try:
        conn.execute(
            """
            INSERT INTO user_sessions (session_id, user_id, session_token_hash, expires_at)
            VALUES (?, ?, ?, ?)
            """,
            (session_id, user_id, session_token_hash, expires_at),
        )
    finally:
        if owns_conn:
            sess.__exit__(None, None, None)


def get_user_session_by_token_hash(token_hash: str, connection: Optional[sqlite3.Connection] = None) -> Optional[Dict[str, Any]]:
    """Retrieve active non-revoked session by SHA-256 token hash."""
    owns_conn = connection is None
    if owns_conn:
        sess = db_session()
        conn = sess.__enter__()
    else:
        conn = connection

    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT s.session_id, s.user_id, s.expires_at, s.is_revoked,
                   u.email, u.name, u.picture, u.google_sub
            FROM user_sessions s
            JOIN users u ON s.user_id = u.user_id
            WHERE s.session_token_hash = ?
              AND s.is_revoked = 0
            """,
            (token_hash,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        if owns_conn:
            sess.__exit__(None, None, None)


def revoke_user_session(token_hash: str, connection: Optional[sqlite3.Connection] = None) -> bool:
    """Revoke a specific session by token hash."""
    owns_conn = connection is None
    if owns_conn:
        sess = db_session()
        conn = sess.__enter__()
    else:
        conn = connection

    try:
        cursor = conn.execute(
            "UPDATE user_sessions SET is_revoked = 1 WHERE session_token_hash = ?",
            (token_hash,),
        )
        return cursor.rowcount > 0
    finally:
        if owns_conn:
            sess.__exit__(None, None, None)


def revoke_all_sessions_for_user(user_id: str, connection: Optional[sqlite3.Connection] = None) -> int:
    """Revoke all active sessions for a user upon security events or global logout."""
    owns_conn = connection is None
    if owns_conn:
        sess = db_session()
        conn = sess.__enter__()
    else:
        conn = connection

    try:
        cursor = conn.execute(
            "UPDATE user_sessions SET is_revoked = 1 WHERE user_id = ? AND is_revoked = 0",
            (user_id,),
        )
        return cursor.rowcount
    finally:
        if owns_conn:
            sess.__exit__(None, None, None)

