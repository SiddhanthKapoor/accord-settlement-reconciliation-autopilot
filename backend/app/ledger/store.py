"""
Persistence + the two mechanisms that actually close AP2's named-open
threats T-31 (mandate replay) and T-33 (shared-budget races):

- `reserve_budget` is an atomic compare-and-swap on the intent's remaining
  budget, executed inside a SQLite `BEGIN IMMEDIATE` transaction. This is
  the real fix for T-33: AP2's own spec only asks the (non-deterministic)
  agent to voluntarily avoid overlapping spends against one open mandate.
  Two concurrent requests hitting `reserve_budget` for the same intent can
  only ever have one of them succeed, regardless of thread/process
  interleaving — it's enforced by the database, not by the agent's
  good behavior.

- `consume_commitment` / `is_commitment_consumed` closes T-31: once a
  commitment has been executed, it is recorded permanently, and any later
  payment request referencing the same commitment_id is rejected before
  any other check runs, independent of whether the artifact re-presented
  is byte-identical or has been re-signed.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from app.domain.models import CatalogEvidence, Commitment, Constraints, TransactionIntent
from app.ledger.db import get_conn


# ---------------------------------------------------------------------------
# Intents / budgets
# ---------------------------------------------------------------------------

def create_intent(intent: TransactionIntent) -> None:
    conn = get_conn()
    conn.execute(
        """INSERT INTO intents (intent_id, constraints_json, declared_natural_language,
                                 created_at, budget_remaining_minor, budget_reserved, budget_reserved_by)
           VALUES (?,?,?,?,?,0,NULL)""",
        (
            intent.intent_id,
            intent.constraints.model_dump_json(),
            intent.declared_natural_language,
            intent.created_at.isoformat(),
            intent.constraints.max_amount_minor,
        ),
    )


def get_intent(intent_id: str) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM intents WHERE intent_id = ?", (intent_id,)).fetchone()
    return dict(row) if row else None


def get_constraints(intent_id: str) -> Optional[Constraints]:
    row = get_intent(intent_id)
    if not row:
        return None
    return Constraints.model_validate_json(row["constraints_json"])


def reserve_budget(intent_id: str, commitment_id: str, amount_minor: int) -> bool:
    """Atomically reserve `amount_minor` against the intent's remaining
    budget. Returns True iff this call is the one that got it."""
    conn = get_conn()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT budget_remaining_minor, budget_reserved FROM intents WHERE intent_id = ?",
            (intent_id,),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return False
        constraints = get_constraints(intent_id)
        if constraints.single_use and row["budget_reserved"]:
            conn.execute("ROLLBACK")
            return False
        if row["budget_remaining_minor"] < amount_minor:
            conn.execute("ROLLBACK")
            return False
        cur = conn.execute(
            """UPDATE intents
               SET budget_remaining_minor = budget_remaining_minor - ?,
                   budget_reserved = 1,
                   budget_reserved_by = ?
               WHERE intent_id = ? AND budget_remaining_minor >= ?""",
            (amount_minor, commitment_id, intent_id, amount_minor),
        )
        ok = cur.rowcount == 1
        conn.execute("COMMIT" if ok else "ROLLBACK")
        return ok
    except Exception:
        conn.execute("ROLLBACK")
        raise


def release_budget(intent_id: str, amount_minor: int) -> None:
    """Used when a reservation's transaction is ultimately BLOCKed (not
    executed) so the budget becomes spendable again rather than being
    permanently burned by a rejected attempt."""
    conn = get_conn()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        """UPDATE intents
           SET budget_remaining_minor = budget_remaining_minor + ?,
               budget_reserved = 0, budget_reserved_by = NULL
           WHERE intent_id = ?""",
        (amount_minor, intent_id),
    )
    conn.execute("COMMIT")


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------

def save_evidence(evidence: CatalogEvidence) -> None:
    conn = get_conn()
    conn.execute(
        """INSERT INTO evidence (evidence_id, intent_id, product_json, fetched_at, source)
           VALUES (?,?,?,?,?)""",
        (
            evidence.evidence_id,
            evidence.intent_id,
            evidence.product.model_dump_json(),
            evidence.fetched_at.isoformat(),
            evidence.source,
        ),
    )


def get_evidence(evidence_id: str) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM evidence WHERE evidence_id = ?", (evidence_id,)).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Commitments
# ---------------------------------------------------------------------------

def save_commitment(commitment: Commitment, state: str) -> None:
    conn = get_conn()
    conn.execute(
        """INSERT INTO commitments (commitment_id, intent_id, evidence_id, merchant_id,
               product_id, product_name, category, quantity, price_minor, currency,
               content_hash, created_at, version, state)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            commitment.commitment_id, commitment.intent_id, commitment.evidence_id,
            commitment.merchant_id, commitment.product_id, commitment.product_name,
            commitment.category, commitment.quantity, commitment.price_minor,
            commitment.currency, commitment.content_hash, commitment.created_at.isoformat(),
            commitment.version, state,
        ),
    )


def get_commitment(commitment_id: str) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM commitments WHERE commitment_id = ?", (commitment_id,)).fetchone()
    return dict(row) if row else None


def update_commitment_state(commitment_id: str, state: str) -> None:
    conn = get_conn()
    conn.execute("UPDATE commitments SET state = ? WHERE commitment_id = ?", (state, commitment_id))


def get_state_counts() -> dict:
    """Real counts from the ledger, grouped by commitment state — the
    Overview screen's numbers come from here, never from a hardcoded
    or fabricated figure."""
    conn = get_conn()
    rows = conn.execute("SELECT state, COUNT(*) as n FROM commitments GROUP BY state").fetchall()
    return {row["state"]: row["n"] for row in rows}


# ---------------------------------------------------------------------------
# Replay ledger (T-31)
# ---------------------------------------------------------------------------

def is_commitment_consumed(commitment_id: str) -> bool:
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM consumed_commitments WHERE commitment_id = ?", (commitment_id,)
    ).fetchone()
    return row is not None


def consume_commitment(commitment_id: str, transaction_id: str) -> bool:
    """Returns False if it was already consumed (a genuine replay caught
    at the last possible instant, e.g. two threads both passed earlier
    checks). Uses INSERT ... primary key conflict as the atomic guard.

    This is called BEFORE the downstream Razorpay call so two concurrent
    /execute calls can never both reach Razorpay for the same commitment.
    If the downstream call then fails for a non-security reason (Razorpay
    not configured, network error), the caller must call
    `unconsume_commitment` to roll this back — a transient infra failure
    must not permanently burn a legitimate commitment."""
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO consumed_commitments (commitment_id, transaction_id, consumed_at) VALUES (?,?,?)",
            (commitment_id, transaction_id, datetime.now(timezone.utc).isoformat()),
        )
        return True
    except Exception:
        return False


def unconsume_commitment(commitment_id: str) -> None:
    """Rollback for consume_commitment when the downstream execution
    failed for a reason unrelated to replay/fraud (e.g. Razorpay not
    configured, transient network error). Never call this after a
    genuinely completed payment."""
    conn = get_conn()
    conn.execute("DELETE FROM consumed_commitments WHERE commitment_id = ?", (commitment_id,))


# ---------------------------------------------------------------------------
# Idempotent payment request handling
# ---------------------------------------------------------------------------

def get_payment_request(client_request_id: str) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM payment_requests WHERE client_request_id = ?", (client_request_id,)
    ).fetchone()
    return dict(row) if row else None


def save_payment_request(client_request_id: str, commitment_id: str, transaction_id: str, decision_json: str) -> None:
    conn = get_conn()
    conn.execute(
        """INSERT OR REPLACE INTO payment_requests
           (client_request_id, commitment_id, transaction_id, decision_json, created_at)
           VALUES (?,?,?,?,?)""",
        (client_request_id, commitment_id, transaction_id, decision_json, datetime.now(timezone.utc).isoformat()),
    )


# ---------------------------------------------------------------------------
# Razorpay execution record
# ---------------------------------------------------------------------------

def save_execution(transaction_id: str, *, order_id: Optional[str], payment_link_id: Optional[str],
                    payment_link_url: Optional[str], status: str, raw_response: dict) -> None:
    conn = get_conn()
    conn.execute(
        """INSERT OR REPLACE INTO razorpay_executions
           (transaction_id, order_id, payment_link_id, payment_link_url, status, raw_response_json, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (transaction_id, order_id, payment_link_id, payment_link_url, status,
         json.dumps(raw_response, default=str), datetime.now(timezone.utc).isoformat()),
    )
