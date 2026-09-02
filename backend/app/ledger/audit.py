"""
Receiver-attested, hash-chained audit log.

Deliberately NOT the headline feature of this project — hash-chained
tamper-evident logs are an established pattern (Certificate-Transparency
style Merkle/hash chains, and there's already an IETF draft — "Agent Audit
Trail" — proposing a standard format for exactly this). It's implemented
here as plumbing that every other component depends on, following the
"receiver-attested" principle from the "Notarized Agents" line of work:
Interlock itself writes and hashes every entry — the agent never gets to
author or edit its own audit trail.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from app.domain.canonical import GENESIS_HASH, chain_hash
from app.ledger.db import get_conn


def _last_hash(conn) -> str:
    row = conn.execute("SELECT hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
    return row["hash"] if row else GENESIS_HASH


def append_event(
    *,
    transaction_id: str,
    event_type: str,
    prior_state: Optional[str],
    new_state: Optional[str],
    evidence_ref: Optional[str] = None,
    payload: Optional[dict] = None,
) -> dict:
    conn = get_conn()
    conn.execute("BEGIN IMMEDIATE")
    try:
        prev_hash = _last_hash(conn)
        timestamp = datetime.now(timezone.utc).isoformat()
        event_payload = {
            "timestamp": timestamp,
            "transaction_id": transaction_id,
            "event_type": event_type,
            "prior_state": prior_state,
            "new_state": new_state,
            "evidence_ref": evidence_ref,
            "payload": payload or {},
        }
        digest = chain_hash(prev_hash, event_payload)
        conn.execute(
            """INSERT INTO audit_log
               (timestamp, transaction_id, event_type, prior_state, new_state,
                evidence_ref, payload_json, prev_hash, hash)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                timestamp,
                transaction_id,
                event_type,
                prior_state,
                new_state,
                evidence_ref,
                json.dumps(payload or {}, default=str),
                prev_hash,
                digest,
            ),
        )
        conn.execute("COMMIT")
        event_payload["prev_hash"] = prev_hash
        event_payload["hash"] = digest
        return event_payload
    except Exception:
        conn.execute("ROLLBACK")
        raise


def get_trail(transaction_id: str) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM audit_log WHERE transaction_id = ? ORDER BY seq ASC",
        (transaction_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_full_log() -> list[dict]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM audit_log ORDER BY seq ASC").fetchall()
    return [dict(r) for r in rows]


def verify_chain() -> dict:
    """Recomputes the entire hash chain from genesis and confirms every
    stored hash matches what recomputing it from (prev_hash, payload)
    produces. This is the 'audit completeness' self-test referenced in
    the evaluation plan — it proves the log wasn't edited after the fact,
    not merely that it exists."""
    rows = get_full_log()
    expected_prev = GENESIS_HASH
    breaks = []
    for row in rows:
        event_payload = {
            "timestamp": row["timestamp"],
            "transaction_id": row["transaction_id"],
            "event_type": row["event_type"],
            "prior_state": row["prior_state"],
            "new_state": row["new_state"],
            "evidence_ref": row["evidence_ref"],
            "payload": json.loads(row["payload_json"]),
        }
        if row["prev_hash"] != expected_prev:
            breaks.append({"seq": row["seq"], "reason": "prev_hash mismatch"})
        recomputed = chain_hash(row["prev_hash"], event_payload)
        if recomputed != row["hash"]:
            breaks.append({"seq": row["seq"], "reason": "hash mismatch (tampered payload)"})
        expected_prev = row["hash"]
    return {
        "total_events": len(rows),
        "intact": len(breaks) == 0,
        "breaks": breaks,
        "head_hash": expected_prev,
    }
