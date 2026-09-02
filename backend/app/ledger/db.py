"""
SQLite is the entire persistence layer. This is a deliberate choice, not a
shortcut: the one property this system actually needs from storage is
"atomic compare-and-swap on a budget row under concurrent access," and
SQLite's `BEGIN IMMEDIATE` transactions give that for free with zero
operational surface area (no Redis, no separate DB server). Reaching for
a distributed lock service here would be infrastructure cosplay.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "interlock.db"

_local = threading.local()


def _configure(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.row_factory = sqlite3.Row


def get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
        _configure(conn)
        _local.conn = conn
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS intents (
    intent_id TEXT PRIMARY KEY,
    constraints_json TEXT NOT NULL,
    declared_natural_language TEXT,
    created_at TEXT NOT NULL,
    budget_remaining_minor INTEGER NOT NULL,
    budget_reserved INTEGER NOT NULL DEFAULT 0,   -- 0/1: has the single-use budget been spent/reserved
    budget_reserved_by TEXT                        -- commitment_id that reserved it
);

CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL REFERENCES intents(intent_id),
    product_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    source TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS commitments (
    commitment_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL REFERENCES intents(intent_id),
    evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
    merchant_id TEXT NOT NULL,
    product_id TEXT NOT NULL,
    product_name TEXT NOT NULL,
    category TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    price_minor INTEGER NOT NULL,
    currency TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    version INTEGER NOT NULL,
    state TEXT NOT NULL
);

-- Every commitment_id that has ever reached a final ALLOW+EXECUTE is recorded
-- here. A second payment request against the same commitment_id is a replay
-- (T-31) and is rejected before any other check runs.
CREATE TABLE IF NOT EXISTS consumed_commitments (
    commitment_id TEXT PRIMARY KEY,
    transaction_id TEXT NOT NULL,
    consumed_at TEXT NOT NULL
);

-- Idempotent retries: the same client_request_id retried against the SAME
-- commitment_id returns the original decision instead of re-running checks
-- (this is legitimate retry handling, not a security gap).
CREATE TABLE IF NOT EXISTS payment_requests (
    client_request_id TEXT PRIMARY KEY,
    commitment_id TEXT NOT NULL,
    transaction_id TEXT NOT NULL,
    decision_json TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    transaction_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    prior_state TEXT,
    new_state TEXT,
    evidence_ref TEXT,
    payload_json TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS razorpay_executions (
    transaction_id TEXT PRIMARY KEY,
    order_id TEXT,
    payment_link_id TEXT,
    payment_link_url TEXT,
    status TEXT NOT NULL,
    raw_response_json TEXT,
    created_at TEXT NOT NULL
);
"""


def init_db() -> None:
    conn = get_conn()
    conn.executescript(SCHEMA)


def reset_db() -> None:
    """Used by the evaluation harness / tests to start from a clean ledger."""
    conn = get_conn()
    conn.executescript(
        """
        DELETE FROM audit_log;
        DELETE FROM razorpay_executions;
        DELETE FROM payment_requests;
        DELETE FROM consumed_commitments;
        DELETE FROM commitments;
        DELETE FROM evidence;
        DELETE FROM intents;
        """
    )
