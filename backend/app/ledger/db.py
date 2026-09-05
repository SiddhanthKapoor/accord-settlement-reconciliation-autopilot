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

-- A record_id identifies a merchant order, not a decision about one. The
-- same order is deliberately re-run across batches (re-processing after
-- a policy change is a normal operation), so the primary key is the
-- (batch, record) pair. It used to be record_id alone, with INSERT OR
-- REPLACE, which meant a second run over the same dataset silently moved
-- rows out of the first batch: that batch still reported its original
-- processed_records but could no longer list them.
CREATE TABLE IF NOT EXISTS records (
    record_id TEXT NOT NULL,
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
    processed_at TEXT NOT NULL,
    classification TEXT,
    exception_type TEXT,
    severity TEXT,
    explanation TEXT,
    recommended_action TEXT,
    considered_json TEXT,
    review_state TEXT NOT NULL DEFAULT 'OPEN',
    PRIMARY KEY (batch_id, record_id)
);
CREATE INDEX IF NOT EXISTS idx_records_batch ON records(batch_id);
CREATE INDEX IF NOT EXISTS idx_records_outcome ON records(batch_id, outcome);
CREATE INDEX IF NOT EXISTS idx_records_record ON records(record_id, processed_at);
CREATE INDEX IF NOT EXISTS idx_records_review ON records(batch_id, review_state, severity);

-- A run is a reconciliation over one or more uploaded sources. `batches`
-- remains for the dataset-driven evaluation path; a run that came from
-- uploads carries its sources here and reuses the same batch_id, so the
-- results, review queue and audit trail are one set of tables rather than
-- a parallel universe for uploaded data.
CREATE TABLE IF NOT EXISTS run_sources (
    source_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    source_type TEXT NOT NULL,
    role TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    accepted_count INTEGER NOT NULL DEFAULT 0,
    rejected_count INTEGER NOT NULL DEFAULT 0,
    mapping_json TEXT NOT NULL,
    detection_json TEXT NOT NULL,
    raw_csv TEXT NOT NULL,
    uploaded_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_run_sources_batch ON run_sources(batch_id);

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
    _migrate_records_primary_key(conn)
    conn.executescript(SCHEMA)


def _migrate_records_primary_key(conn: sqlite3.Connection) -> None:
    """Rebuild `records` if it still carries the old record_id-only key.

    CREATE TABLE IF NOT EXISTS will not alter an existing table, so a
    database created before the composite key would keep the bug
    silently. The table holds derived decisions that are reproduced by
    re-running a batch, so rebuilding it is safe; it is still announced
    rather than done quietly.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'records'"
    ).fetchone()
    if row is None:
        return
    sql = row["sql"] or ""
    if "PRIMARY KEY (batch_id, record_id)" in sql and "review_state" in sql:
        return
    print("[db] rebuilding `records` for the current schema (composite key + review workflow columns); "
          "previously processed batches must be re-run to repopulate it.")
    conn.executescript("DROP TABLE IF EXISTS records;")


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
