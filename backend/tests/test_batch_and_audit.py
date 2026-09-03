"""
Batch-level resilience (one bad record can't take down the run) and
audit-log integrity (the hash chain genuinely detects tampering, not
just that it exists) — the audit ledger module itself is unchanged from
its prior use (see app/ledger/audit.py's docstring), reused as-is,
because a receiver-attested hash chain has no domain-specific logic in
it at all.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.domain.models import MerchantRecord, PolicyConfig, ReconciliationOutcome, ReconciliationRecord
from app.engine.batch import process_batch
from app.engine.semantic import SemanticVerdictResult
from app.ledger import audit, db

NOW = datetime(2026, 3, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def clean_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db._local.__dict__.clear()
    db.init_db()
    yield
    db._local.__dict__.clear()


def _merchant(order_id: str, **overrides) -> MerchantRecord:
    defaults = dict(
        order_id=order_id, reference_id=f"{order_id}-REF", amount_minor=100000, currency="INR",
        order_date=NOW, status="captured", refund_amount_minor=0, description=f"Order {order_id} - Widget",
    )
    defaults.update(overrides)
    return MerchantRecord(**defaults)


class ExplodingVerifier:
    """Raises on every call — used to prove a batch keeps going even if
    the semantic path is completely broken for every ambiguous record."""

    def compare(self, merchant, candidate):
        raise RuntimeError("boom")


def test_one_bad_record_does_not_stop_the_batch(monkeypatch):
    records = [ReconciliationRecord(record_id=f"ORD{i}", merchant=_merchant(f"ORD{i}")) for i in range(5)]

    import app.engine.batch as batch_module

    original_reconcile = batch_module.policy_engine.reconcile
    call_count = {"n": 0}

    def flaky_reconcile(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 3:
            raise ValueError("simulated unexpected failure on record 3")
        return original_reconcile(*args, **kwargs)

    monkeypatch.setattr(batch_module.policy_engine, "reconcile", flaky_reconcile)

    results = process_batch(records, razorpay_records=[])
    assert len(results) == 5, "all 5 records must produce a result even though one raised"
    assert results[2].outcome == ReconciliationOutcome.HUMAN_REVIEW
    assert "unexpected processing error" in results[2].reason.lower()
    # The other four should have processed normally (all missing settlements -> EXCEPTION).
    assert results[0].outcome == ReconciliationOutcome.EXCEPTION


def test_batch_survives_every_ambiguous_record_failing_ai():
    """A pool that forces every record through the semantic path, with a
    verifier that always explodes — the whole batch must still complete."""
    records = [ReconciliationRecord(record_id=f"ORD{i}", merchant=_merchant(f"ORD{i}", reference_id="NO-MATCH")) for i in range(3)]
    from app.domain.models import RazorpaySettlementRecord

    pool = [
        RazorpaySettlementRecord(
            payment_id=f"pay_{i}", order_reference="DIFFERENT-REF", settlement_id=f"setl_{i}",
            gross_amount_minor=100000, fee_minor=2000, tax_minor=360, net_amount_minor=97640,
            order_date=NOW, settlement_date=NOW, status="settled", description="Settlement widget order",
        )
        for i in range(3)
    ]
    results = process_batch(records, pool, semantic_verifier=ExplodingVerifier())
    assert len(results) == 3
    assert all(r.outcome == ReconciliationOutcome.HUMAN_REVIEW for r in results)


def test_batch_progress_callback_fires_for_every_record():
    records = [ReconciliationRecord(record_id=f"ORD{i}", merchant=_merchant(f"ORD{i}")) for i in range(4)]
    seen = []
    process_batch(records, [], on_record=lambda i, total, rec, res: seen.append((i, total)))
    assert seen == [(0, 4), (1, 4), (2, 4), (3, 4)]


# --------------------------------------------------------------- audit integrity

def test_audit_chain_is_intact_after_normal_writes():
    for i in range(10):
        audit.append_event(transaction_id=f"ORD{i}", event_type="RECORD_DECIDED", prior_state=None, new_state="EXCEPTION", payload={"i": i})
    status = audit.verify_chain()
    assert status["intact"] is True
    assert status["total_events"] == 10
    assert status["breaks"] == []


def test_audit_chain_detects_tampered_payload():
    audit.append_event(transaction_id="ORD1", event_type="RECORD_DECIDED", prior_state=None, new_state="RECONCILED", payload={"outcome": "RECONCILED"})
    audit.append_event(transaction_id="ORD2", event_type="RECORD_DECIDED", prior_state=None, new_state="EXCEPTION", payload={"outcome": "EXCEPTION"})

    conn = db.get_conn()
    # Simulate someone editing history directly in the database — flip a
    # RECONCILED decision to EXCEPTION after the fact.
    conn.execute("UPDATE audit_log SET payload_json = ? WHERE seq = 1", ('{"outcome":"EXCEPTION"}',))

    status = audit.verify_chain()
    assert status["intact"] is False
    assert any(b["reason"] == "hash mismatch (tampered payload)" for b in status["breaks"])


def test_audit_trail_is_queryable_per_record():
    audit.append_event(transaction_id="ORD_X", event_type="RECORD_DECIDED", prior_state=None, new_state="RECONCILED", payload={})
    audit.append_event(transaction_id="ORD_Y", event_type="RECORD_DECIDED", prior_state=None, new_state="EXCEPTION", payload={})
    trail = audit.get_trail("ORD_X")
    assert len(trail) == 1
    assert trail[0]["transaction_id"] == "ORD_X"
