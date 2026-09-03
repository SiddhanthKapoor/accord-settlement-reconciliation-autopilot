from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from app.domain.models import ReconciliationRecord, ReconciliationResult, RazorpaySettlementRecord
from app.ledger.db import get_conn


def create_batch(batch_id: str, label: str, dataset_source: str, total_records: int) -> None:
    conn = get_conn()
    conn.execute(
        """INSERT INTO batches (batch_id, label, dataset_source, total_records, started_at, status)
           VALUES (?,?,?,?,?, 'RUNNING')""",
        (batch_id, label, dataset_source, total_records, datetime.now(timezone.utc).isoformat()),
    )


def mark_batch_complete(batch_id: str) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE batches SET status='COMPLETED', completed_at=? WHERE batch_id=?",
        (datetime.now(timezone.utc).isoformat(), batch_id),
    )


def increment_batch_progress(batch_id: str) -> None:
    conn = get_conn()
    conn.execute("UPDATE batches SET processed_records = processed_records + 1 WHERE batch_id=?", (batch_id,))


def get_batch(batch_id: str) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
    return dict(row) if row else None


def get_latest_batch() -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM batches ORDER BY started_at DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def save_record(
    batch_id: str, seq: int, record: ReconciliationRecord, result: ReconciliationResult,
    candidates: list[RazorpaySettlementRecord],
) -> None:
    conn = get_conn()
    conn.execute(
        """INSERT OR REPLACE INTO records
           (record_id, batch_id, seq_in_batch, merchant_json, candidates_json, candidate_count,
            matched_payment_id, ground_truth_case, ground_truth_label, outcome, reason, checks_json,
            ai_invoked, ai_confidence, ai_backend, policy_threshold, latency_ms, processed_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            record.record_id, batch_id, seq,
            record.merchant.model_dump_json(),
            json.dumps([c.model_dump(mode="json") for c in candidates]),
            result.candidate_count,
            result.matched_payment_id,
            record.ground_truth.case if record.ground_truth else None,
            record.ground_truth.expected_outcome.value if record.ground_truth else None,
            result.outcome.value,
            result.reason,
            json.dumps([c.model_dump(mode="json") for c in result.checks]),
            int(result.ai_invoked),
            result.ai_confidence,
            result.ai_backend,
            result.policy_threshold,
            result.latency_ms,
            result.decided_at.isoformat(),
        ),
    )


def get_record(record_id: str) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM records WHERE record_id=?", (record_id,)).fetchone()
    return dict(row) if row else None


def list_records(batch_id: str, *, outcome: Optional[str] = None, limit: int = 200, offset: int = 0) -> list[dict]:
    conn = get_conn()
    if outcome:
        rows = conn.execute(
            "SELECT * FROM records WHERE batch_id=? AND outcome=? ORDER BY seq_in_batch ASC LIMIT ? OFFSET ?",
            (batch_id, outcome, limit, offset),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM records WHERE batch_id=? ORDER BY seq_in_batch ASC LIMIT ? OFFSET ?",
            (batch_id, limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


def batch_outcome_counts(batch_id: str) -> dict[str, int]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT outcome, COUNT(*) as n FROM records WHERE batch_id=? GROUP BY outcome", (batch_id,)
    ).fetchall()
    return {row["outcome"]: row["n"] for row in rows}
