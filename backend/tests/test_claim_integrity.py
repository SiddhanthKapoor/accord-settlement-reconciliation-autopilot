"""
One settlement, one merchant record.

Reconciling two orders against the same payment counts the same money
twice, and every per-record check passes while it happens — each record
is individually correct and collectively wrong. These tests pin the batch
level invariant that catches it.
"""

from __future__ import annotations

import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.domain.models import (
    ExceptionType, MatchClassification, MerchantRecord, PolicyConfig, RazorpaySettlementRecord,
    ReconciliationOutcome, ReconciliationRecord, Severity,
)
from app.engine.batch import detect_duplicate_claims, process_batch, resolve_claims
from app.engine.semantic import CandidateComparison, SemanticVerdictResult

NOW = datetime(2026, 3, 1, tzinfo=timezone.utc)
POLICY = PolicyConfig()


class StubVerifier:
    def __init__(self, verdict="SAME", confidence=0.95):
        self.verdict, self.confidence, self.calls = verdict, confidence, 0

    def compare(self, comparison: CandidateComparison) -> SemanticVerdictResult:
        self.calls += 1
        return SemanticVerdictResult(self.verdict, self.confidence, "stub", "stub")


def merchant(order_id: str, **overrides) -> MerchantRecord:
    defaults = dict(
        order_id=order_id, reference_id=f"REF-{order_id}", amount_minor=100000, currency="INR",
        order_date=NOW, status="captured", refund_amount_minor=0,
        description=f"Order {order_id} Premium Plan",
    )
    defaults.update(overrides)
    return MerchantRecord(**defaults)


def settlement(payment_id: str, **overrides) -> RazorpaySettlementRecord:
    defaults = dict(
        payment_id=payment_id, order_reference="SHARED-REF", settlement_id="setl_1",
        gross_amount_minor=100000, fee_minor=2000, tax_minor=360, net_amount_minor=97640,
        refund_amount_minor=0, order_date=NOW, settlement_date=NOW + timedelta(days=2),
        currency="INR", status="settled", description="Settlement Premium Plan",
    )
    defaults.update(overrides)
    return RazorpaySettlementRecord(**defaults)


def record(order_id: str, **overrides) -> ReconciliationRecord:
    return ReconciliationRecord(record_id=order_id, merchant=merchant(order_id, **overrides))


# ---------------------------------------------------------------------------
# The core invariant
# ---------------------------------------------------------------------------

def test_two_orders_cannot_both_reconcile_against_one_settlement():
    """Both records match the same payment on an exact reference. Each is
    individually defensible; reconciling both would book the money twice."""
    shared = settlement("pay_shared", order_reference="SHARED-REF")
    records = [record("A", reference_id="SHARED-REF"), record("B", reference_id="SHARED-REF")]

    results = process_batch(records, [shared], policy=POLICY, semantic_verifier=StubVerifier())

    reconciled = [r for r in results if r.outcome is ReconciliationOutcome.RECONCILED]
    assert len(reconciled) <= 1, "at most one record may reconcile against a single settlement"
    demoted = [r for r in results if r.exception_type is ExceptionType.DUPLICATE_CLAIM]
    assert demoted, "the losing claim must be reported as a duplicate claim, not silently dropped"
    assert all(r.outcome is ReconciliationOutcome.HUMAN_REVIEW for r in demoted)


def test_a_tie_demotes_every_claimant_rather_than_picking_by_position():
    """When two claims rest on identical evidence, order in the batch is
    not a tiebreaker. Choosing by position is arbitrary, and arbitrary is
    indistinguishable from wrong once someone audits it."""
    shared = settlement("pay_shared", order_reference="SHARED-REF")
    records = [record("A", reference_id="SHARED-REF"), record("B", reference_id="SHARED-REF")]

    results = process_batch(records, [shared], policy=POLICY, semantic_verifier=StubVerifier())
    assert all(r.outcome is ReconciliationOutcome.HUMAN_REVIEW for r in results)
    assert all(r.exception_type is ExceptionType.DUPLICATE_CLAIM for r in results)


def test_stronger_evidence_wins_a_conflict_deterministically():
    """An exact reference match outranks a semantic one. That is a real
    difference in evidence, so it settles the conflict."""
    from app.domain.models import ReconciliationResult, CheckResult, CheckStatus

    def result_for(record_id, classification, outcome=ReconciliationOutcome.RECONCILED):
        return ReconciliationResult(
            record_id=record_id, outcome=outcome, reason="", checks=[],
            classification=classification, matched_payment_id="pay_shared",
            candidate_count=1, policy_threshold=0.85, latency_ms=1.0,
        )

    results = [
        result_for("strong", MatchClassification.EXACT_REFERENCE),
        result_for("weak", MatchClassification.SEMANTIC_CONFIRMED),
    ]
    resolved, conflicts = resolve_claims(results)

    assert conflicts == {"pay_shared": ["strong", "weak"]}
    by_id = {r.record_id: r for r in resolved}
    assert by_id["strong"].outcome is ReconciliationOutcome.RECONCILED
    assert by_id["weak"].outcome is ReconciliationOutcome.HUMAN_REVIEW
    assert by_id["weak"].exception_type is ExceptionType.DUPLICATE_CLAIM


def test_conflict_resolution_is_independent_of_batch_order():
    """Reversing the batch must not change any record's fate."""
    from app.domain.models import ReconciliationResult

    def result_for(record_id, classification):
        return ReconciliationResult(
            record_id=record_id, outcome=ReconciliationOutcome.RECONCILED, reason="", checks=[],
            classification=classification, matched_payment_id="pay_shared",
            candidate_count=1, policy_threshold=0.85, latency_ms=1.0,
        )

    forward = [result_for("strong", MatchClassification.EXACT_REFERENCE),
               result_for("weak", MatchClassification.SEMANTIC_CONFIRMED)]
    backward = [result_for("weak", MatchClassification.SEMANTIC_CONFIRMED),
                result_for("strong", MatchClassification.EXACT_REFERENCE)]

    forward_outcomes = {r.record_id: r.outcome for r in resolve_claims(forward)[0]}
    backward_outcomes = {r.record_id: r.outcome for r in resolve_claims(backward)[0]}
    assert forward_outcomes == backward_outcomes


def test_a_demoted_claim_records_who_it_competed_with():
    """The exception has to be actionable: an operator needs to know which
    other order wanted this settlement."""
    shared = settlement("pay_shared", order_reference="SHARED-REF")
    records = [record("A", reference_id="SHARED-REF"), record("B", reference_id="SHARED-REF")]
    results = process_batch(records, [shared], policy=POLICY, semantic_verifier=StubVerifier())

    demoted = next(r for r in results if r.exception_type is ExceptionType.DUPLICATE_CLAIM)
    assert "pay_shared" in demoted.reason
    assert any(other in demoted.reason for other in ("A", "B"))
    assert demoted.severity is Severity.HIGH
    assert any(c.name == "claim_uniqueness" for c in demoted.checks)
    assert demoted.recommended_action


def test_distinct_settlements_are_untouched():
    records = [record("A", reference_id="R1"), record("B", reference_id="R2")]
    pool = [
        settlement("pay_1", order_reference="R1"),
        settlement("pay_2", order_reference="R2"),
    ]
    results = process_batch(records, pool, policy=POLICY, semantic_verifier=StubVerifier())
    assert all(r.outcome is ReconciliationOutcome.RECONCILED for r in results)
    assert detect_duplicate_claims(results) == {}


def test_records_that_matched_nothing_do_not_contend_for_claims():
    """An EXCEPTION carries no claim, so two unmatched records must not be
    reported as competing for a settlement neither of them got."""
    records = [record("A", reference_id="MISSING-1"), record("B", reference_id="MISSING-2")]
    results = process_batch(records, [], policy=POLICY, semantic_verifier=StubVerifier())
    assert detect_duplicate_claims(results) == {}
    assert all(r.exception_type is not ExceptionType.DUPLICATE_CLAIM for r in results)


# ---------------------------------------------------------------------------
# Repetition, isolation, concurrency
# ---------------------------------------------------------------------------

def test_reprocessing_the_same_batch_gives_the_same_claims():
    shared = settlement("pay_shared", order_reference="SHARED-REF")
    records = [record("A", reference_id="SHARED-REF"), record("B", reference_id="SHARED-REF")]

    first = process_batch(records, [shared], policy=POLICY, semantic_verifier=StubVerifier())
    second = process_batch(records, [shared], policy=POLICY, semantic_verifier=StubVerifier())
    assert [r.outcome for r in first] == [r.outcome for r in second]
    assert [r.exception_type for r in first] == [r.exception_type for r in second]


def test_claims_do_not_leak_between_separate_batches():
    """Two batches over the same settlement are independent runs, not a
    double claim — reconciliation is re-run over the same data routinely."""
    shared = settlement("pay_shared", order_reference="SHARED-REF")
    a = process_batch([record("A", reference_id="SHARED-REF")], [shared],
                      policy=POLICY, semantic_verifier=StubVerifier())
    b = process_batch([record("B", reference_id="SHARED-REF")], [shared],
                      policy=POLICY, semantic_verifier=StubVerifier())
    assert a[0].outcome is ReconciliationOutcome.RECONCILED
    assert b[0].outcome is ReconciliationOutcome.RECONCILED


def test_concurrent_batches_resolve_claims_independently():
    shared = settlement("pay_shared", order_reference="SHARED-REF")
    outputs: dict[int, list] = {}

    def work(worker: int):
        records = [record(f"w{worker}-A", reference_id="SHARED-REF"),
                   record(f"w{worker}-B", reference_id="SHARED-REF")]
        outputs[worker] = process_batch(records, [shared], policy=POLICY, semantic_verifier=StubVerifier())

    threads = [threading.Thread(target=work, args=(w,)) for w in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(outputs) == 4
    for results in outputs.values():
        assert len(results) == 2
        assert sum(1 for r in results if r.outcome is ReconciliationOutcome.RECONCILED) <= 1


def test_revision_callback_fires_for_records_whose_outcome_changed():
    """Anything that persisted the first decision has to be told it moved,
    or the stored record disagrees with the returned one."""
    shared = settlement("pay_shared", order_reference="SHARED-REF")
    records = [record("A", reference_id="SHARED-REF"), record("B", reference_id="SHARED-REF")]

    revisions: list[str] = []
    process_batch(records, [shared], policy=POLICY, semantic_verifier=StubVerifier(),
                  on_revision=lambda i, rec, res: revisions.append(rec.record_id))
    assert sorted(revisions) == ["A", "B"]


def test_no_revision_callback_when_there_is_nothing_to_revise():
    records = [record("A", reference_id="R1")]
    pool = [settlement("pay_1", order_reference="R1")]
    revisions: list[str] = []
    process_batch(records, pool, policy=POLICY, semantic_verifier=StubVerifier(),
                  on_revision=lambda i, rec, res: revisions.append(rec.record_id))
    assert revisions == []


def test_settlement_id_may_be_shared_without_conflict():
    """Many payments legitimately settle in one batch and share a
    settlement_id. Only a shared payment_id is a double claim."""
    records = [record("A", reference_id="R1"), record("B", reference_id="R2")]
    pool = [
        settlement("pay_1", order_reference="R1", settlement_id="setl_batch_9"),
        settlement("pay_2", order_reference="R2", settlement_id="setl_batch_9"),
    ]
    results = process_batch(records, pool, policy=POLICY, semantic_verifier=StubVerifier())
    assert all(r.outcome is ReconciliationOutcome.RECONCILED for r in results)
    assert detect_duplicate_claims(results) == {}


# ---------------------------------------------------------------------------
# Aggregated settlements: detected and proposed, never applied
# ---------------------------------------------------------------------------

def test_a_settlement_bundling_several_records_is_detected():
    """A bank credits one amount for several gateway payments. That shape
    is real and worth surfacing — but the grouping is proposed, not
    booked."""
    from app.domain.models import ExceptionType as ET
    records = [
        ReconciliationRecord(record_id="A", merchant=merchant("A", reference_id="R-A", amount_minor=30000)),
        ReconciliationRecord(record_id="B", merchant=merchant("B", reference_id="R-B", amount_minor=70000)),
    ]
    lump = settlement("pay_lump", order_reference="BANKREF-9", gross_amount_minor=100000,
                      fee_minor=0, tax_minor=0, net_amount_minor=100000)

    results = process_batch(records, [lump], policy=POLICY,
                            semantic_verifier=StubVerifier(verdict="DIFFERENT", confidence=0.9))

    assert all(r.outcome is ReconciliationOutcome.HUMAN_REVIEW for r in results)
    assert all(r.exception_type is ET.AGGREGATED_SETTLEMENT for r in results)
    assert all(r.matched_payment_id is None for r in results), \
        "a proposed grouping must not book a match"
    assert any("pay_lump" in r.reason for r in results)


def test_an_ambiguous_decomposition_is_not_reported():
    """Two different pairs summing to the same total means the system
    cannot tell which grouping is real, and saying nothing is correct."""
    from app.engine.batch import detect_aggregated_settlements
    records = [
        ReconciliationRecord(record_id=rid, merchant=merchant(rid, reference_id=f"R-{rid}", amount_minor=amt))
        for rid, amt in [("A", 30000), ("B", 70000), ("C", 40000), ("D", 60000)]
    ]
    lump = settlement("pay_lump", order_reference="BANKREF-9", gross_amount_minor=100000,
                      fee_minor=0, tax_minor=0, net_amount_minor=100000)
    results = process_batch(records, [lump], policy=POLICY,
                            semantic_verifier=StubVerifier(verdict="DIFFERENT", confidence=0.9))
    # A+B and C+D both total 100000.
    assert detect_aggregated_settlements(results, records, [lump], POLICY) == {}


def test_aggregation_detection_ignores_already_matched_settlements():
    from app.engine.batch import detect_aggregated_settlements
    records = [ReconciliationRecord(record_id="A", merchant=merchant("A", reference_id="R1"))]
    pool = [settlement("pay_1", order_reference="R1")]
    results = process_batch(records, pool, policy=POLICY, semantic_verifier=StubVerifier())
    assert detect_aggregated_settlements(results, records, pool, POLICY) == {}


def test_aggregation_search_is_bounded():
    """Subset-sum is exponential; a run that hangs is worse than one that
    misses an aggregation."""
    from app.engine.batch import detect_aggregated_settlements
    records = [
        ReconciliationRecord(record_id=f"R{i}",
                             merchant=merchant(f"R{i}", reference_id=f"REF-{i}", amount_minor=1000 + i))
        for i in range(200)
    ]
    lump = settlement("pay_lump", order_reference="BANKREF", gross_amount_minor=999_999_99,
                      fee_minor=0, tax_minor=0, net_amount_minor=999_999_99)
    results = [type("R", (), {"record_id": r.record_id, "matched_payment_id": None,
                              "outcome": ReconciliationOutcome.EXCEPTION})() for r in records]
    import time as _t
    started = _t.perf_counter()
    detect_aggregated_settlements(results, records, [lump], POLICY)
    assert _t.perf_counter() - started < 2.0, "the candidate cap must keep this bounded"
