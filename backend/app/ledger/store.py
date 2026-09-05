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


def list_batches(limit: int = 20) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM batches ORDER BY started_at DESC LIMIT ?", (limit,)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["outcome_counts"] = batch_outcome_counts(d["batch_id"])
        out.append(d)
    return out


def set_batch_total(batch_id: str, total: int, label: str) -> None:
    """A run's size is only known once its sources are mapped, so the row
    is created empty and sized at execution."""
    conn = get_conn()
    conn.execute(
        "UPDATE batches SET total_records=?, label=?, status='RUNNING', processed_records=0 WHERE batch_id=?",
        (total, label, batch_id),
    )


def get_latest_batch() -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM batches ORDER BY started_at DESC LIMIT 1").fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Uploaded sources
# ---------------------------------------------------------------------------

def save_source(
    source_id: str, batch_id: str, filename: str, source_type: str, role: str,
    row_count: int, mapping: dict, detection: dict, raw_csv: str,
) -> None:
    conn = get_conn()
    conn.execute(
        """INSERT OR REPLACE INTO run_sources
           (source_id, batch_id, filename, source_type, role, row_count, accepted_count,
            rejected_count, mapping_json, detection_json, raw_csv, uploaded_at)
           VALUES (?,?,?,?,?,?,0,0,?,?,?,?)""",
        (source_id, batch_id, filename, source_type, role, row_count,
         json.dumps(mapping), json.dumps(detection, default=str), raw_csv,
         datetime.now(timezone.utc).isoformat()),
    )


def update_source_mapping(source_id: str, mapping: dict, source_type: str, role: str) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE run_sources SET mapping_json=?, source_type=?, role=? WHERE source_id=?",
        (json.dumps(mapping), source_type, role, source_id),
    )


def record_source_outcome(source_id: str, accepted: int, rejected: int) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE run_sources SET accepted_count=?, rejected_count=? WHERE source_id=?",
        (accepted, rejected, source_id),
    )


def list_sources(batch_id: str, include_raw: bool = False) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM run_sources WHERE batch_id=? ORDER BY uploaded_at ASC", (batch_id,)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["mapping"] = json.loads(d.pop("mapping_json"))
        d["detection"] = json.loads(d.pop("detection_json"))
        if not include_raw:
            d.pop("raw_csv", None)
        out.append(d)
    return out


def get_source(source_id: str) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM run_sources WHERE source_id=?", (source_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["mapping"] = json.loads(d.pop("mapping_json"))
    d["detection"] = json.loads(d.pop("detection_json"))
    return d


def delete_source(source_id: str) -> None:
    conn = get_conn()
    conn.execute("DELETE FROM run_sources WHERE source_id=?", (source_id,))


def save_record(
    batch_id: str, seq: int, record: ReconciliationRecord, result: ReconciliationResult,
    candidates: list[RazorpaySettlementRecord],
) -> None:
    conn = get_conn()
    conn.execute(
        """INSERT OR REPLACE INTO records
           (record_id, batch_id, seq_in_batch, merchant_json, candidates_json, candidate_count,
            matched_payment_id, ground_truth_case, ground_truth_label, outcome, reason, checks_json,
            ai_invoked, ai_confidence, ai_backend, policy_threshold, latency_ms, processed_at,
            classification, exception_type, severity, explanation, recommended_action, considered_json,
            review_state)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                   COALESCE((SELECT review_state FROM records WHERE batch_id=? AND record_id=?), 'OPEN'))""",
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
            result.classification.value,
            result.exception_type.value if result.exception_type else None,
            result.severity.value if result.severity else None,
            result.explanation,
            result.recommended_action,
            json.dumps([c.model_dump(mode="json") for c in result.considered_candidates]),
            # A re-run must not silently reopen something a reviewer already
            # actioned; the existing review state is preserved when present.
            batch_id, record.record_id,
        ),
    )


# What a reviewer can do, and what it means. Deliberately small: an action
# that does not correspond to a real operational decision is a button that
# teaches an operator the wrong model of the system.
REVIEW_ACTIONS: dict[str, dict] = {
    "APPROVE_MATCH": {
        "label": "Approve match",
        "new_state": "APPROVED",
        "requires_candidate": True,
        "description": "Confirm the suggested settlement is correct and reconcile the record.",
    },
    "REJECT_MATCH": {
        "label": "Reject candidate",
        "new_state": "REJECTED",
        "requires_candidate": True,
        "description": "The suggested settlement is not this order's.",
    },
    "MARK_MISSING": {
        "label": "Confirm missing",
        "new_state": "CONFIRMED_MISSING",
        "requires_candidate": False,
        "description": "No settlement exists; raise this with the provider.",
    },
    "DEFER": {
        "label": "Defer",
        "new_state": "DEFERRED",
        "requires_candidate": False,
        "description": "Wait for the settlement window to close, then re-run.",
    },
    "ESCALATE": {
        "label": "Escalate",
        "new_state": "ESCALATED",
        "requires_candidate": False,
        "description": "Needs someone with more authority or provider contact.",
    },
}


# Exceptions about *which* settlement this is. Only these are resolvable
# by a reviewer confirming or rejecting the candidate.
_MATCHING_DISPUTES = {
    "AMBIGUOUS_MATCH", "LOW_CONFIDENCE_MATCH", "DUPLICATE_REFERENCE",
    "DUPLICATE_CLAIM", "MISSING_SETTLEMENT", "PENDING_SETTLEMENT",
}


def available_actions(record: dict) -> list[dict]:
    """Actions that make sense for this record's current state.

    Two constraints, both about not teaching an operator a false model of
    the system. There must be a candidate before anyone is invited to
    approve one. And where the match itself is settled but the money
    disagrees — an amount, currency, fee or refund discrepancy — the
    dispute is not about *which* settlement this is, so "approve match and
    reconcile" is not an available answer. Reconciling a record whose
    amount is known to be wrong is precisely the outcome this system
    exists to prevent, and offering it as a button would undo that at the
    last step.
    """
    if record.get("review_state") not in (None, "OPEN"):
        return []
    has_candidate = bool(record.get("matched_payment_id")) or bool(
        json.loads(record.get("considered_json") or "[]")
    )
    exception_type = record.get("exception_type")
    match_is_disputed = exception_type in _MATCHING_DISPUTES or exception_type is None

    actions = []
    for key, meta in REVIEW_ACTIONS.items():
        if meta["requires_candidate"] and not has_candidate:
            continue
        if key == "APPROVE_MATCH" and not match_is_disputed:
            continue
        actions.append({"action": key, **{k: v for k, v in meta.items() if k != "requires_candidate"}})
    return actions


def set_review_state(batch_id: str, record_id: str, new_state: str) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE records SET review_state=? WHERE batch_id=? AND record_id=?",
        (new_state, batch_id, record_id),
    )


def list_review_queue(
    batch_id: str, *, state: str = "OPEN", limit: int = 100, offset: int = 0
) -> list[dict]:
    """Records awaiting a human, worst first.

    Ordered by severity then amount: an operator with an hour should spend
    it on the largest thing that is definitely wrong, not on whatever
    happened to be processed first.
    """
    conn = get_conn()
    rows = conn.execute(
        """SELECT * FROM records
           WHERE batch_id=? AND review_state=? AND outcome IN ('HUMAN_REVIEW','EXCEPTION')
           ORDER BY CASE severity WHEN 'HIGH' THEN 0 WHEN 'MEDIUM' THEN 1 ELSE 2 END,
                    CAST(json_extract(merchant_json, '$.amount_minor') AS INTEGER) DESC
           LIMIT ? OFFSET ?""",
        (batch_id, state, limit, offset),
    ).fetchall()
    return [dict(r) for r in rows]


def review_queue_summary(batch_id: str) -> dict:
    conn = get_conn()
    rows = conn.execute(
        """SELECT exception_type, severity, review_state, COUNT(*) AS n,
                  SUM(CAST(json_extract(merchant_json, '$.amount_minor') AS INTEGER)) AS amount_minor
           FROM records
           WHERE batch_id=? AND outcome IN ('HUMAN_REVIEW','EXCEPTION')
           GROUP BY exception_type, severity, review_state""",
        (batch_id,),
    ).fetchall()
    by_type: dict[str, dict] = {}
    open_count = 0
    open_amount = 0
    for r in rows:
        key = r["exception_type"] or "UNCLASSIFIED"
        entry = by_type.setdefault(key, {"count": 0, "amount_minor": 0, "severity": r["severity"]})
        entry["count"] += r["n"]
        entry["amount_minor"] += r["amount_minor"] or 0
        if r["review_state"] == "OPEN":
            open_count += r["n"]
            open_amount += r["amount_minor"] or 0
    return {
        "by_exception_type": by_type,
        "open_count": open_count,
        "open_amount_minor": open_amount,
    }


def get_record(record_id: str, batch_id: Optional[str] = None) -> Optional[dict]:
    """One record's decision. A record_id can appear in several batches
    (re-processing the same orders is normal), so `batch_id` selects a
    specific run; without it the most recent decision wins, which is what
    a UI opening a record by id means."""
    conn = get_conn()
    if batch_id:
        row = conn.execute(
            "SELECT * FROM records WHERE record_id=? AND batch_id=?", (record_id, batch_id)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM records WHERE record_id=? ORDER BY processed_at DESC LIMIT 1", (record_id,)
        ).fetchone()
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
