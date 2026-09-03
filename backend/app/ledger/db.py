"""
SQLite is the entire persistence layer — same rationale as before: the
real operational requirement here (safely running many records through a
batch, one audit entry per decision, queryable by an interactive UI) is
well served by a single file with WAL mode, with none of a separate
database server's operational surface area.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "reconciliation.db"

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
CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    dataset_source TEXT NOT NULL,
    total_records INTEGER NOT NULL,
    processed_records INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL DEFAULT 'RUNNING'
);

CREATE TABLE IF NOT EXISTS records (
    record_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    seq_in_batch INTEGER NOT NULL,
    merchant_json TEXT NOT NULL,
    candidates_json TEXT NOT NULL,
    candidate_count INTEGER NOT NULL,
    matched_payment_id TEXT,
    ground_truth_case TEXT,
    ground_truth_label TEXT,
    outcome TEXT NOT NULL,
    reason TEXT NOT NULL,
    checks_json TEXT NOT NULL,
    ai_invoked INTEGER NOT NULL DEFAULT 0,
    ai_confidence REAL,
    ai_backend TEXT,
    policy_threshold REAL NOT NULL,
    latency_ms REAL NOT NULL,
    processed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_records_batch ON records(batch_id);
CREATE INDEX IF NOT EXISTS idx_records_outcome ON records(batch_id, outcome);

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
CREATE INDEX IF NOT EXISTS idx_audit_txn ON audit_log(transaction_id);
"""


def init_db() -> None:
    conn = get_conn()
    conn.executescript(SCHEMA)


def reset_db() -> None:
    """Used by tests and the 'reset session' UI action."""
    conn = get_conn()
    conn.executescript(
        """
        DELETE FROM audit_log;
        DELETE FROM records;
        DELETE FROM batches;
        """
    )
