"""
Failure modes found by inspecting the system rather than by listing
categories in the abstract.

Most of these are regression tests for bugs that were actually in the
code: a missing currency check, a primary key that let a re-run steal
another batch's rows, a route that shadowed another, an SSE stream that
replayed its entire history to every client, and the candidate ranker
that made the model look wrong for a mistake made upstream of it. Each
test names the defect it pins down, so a future change that reintroduces
one fails loudly instead of quietly.
"""

from __future__ import annotations

import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.domain.models import (
    MerchantRecord, PolicyConfig, RazorpaySettlementRecord,
    ReconciliationOutcome, ReconciliationRecord,
)
from app.engine import matching, normalize
from app.engine.batch import detect_duplicate_claims, process_batch
from app.engine.policy import reconcile
from app.engine.semantic import CandidateComparison, HeuristicSemanticVerifier, SemanticVerdictResult
from app.ledger import audit, db, store

NOW = datetime(2026, 3, 1, tzinfo=timezone.utc)
POLICY = PolicyConfig()


@pytest.fixture(autouse=True)
def clean_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db._local.__dict__.clear()
    db.init_db()
    yield
    db._local.__dict__.clear()


class StubVerifier:
    def __init__(self, verdict="SAME", confidence=0.95):
        self.verdict, self.confidence, self.calls = verdict, confidence, 0

    def compare(self, comparison: CandidateComparison) -> SemanticVerdictResult:
        self.calls += 1
        return SemanticVerdictResult(self.verdict, self.confidence, "stub", "stub")


def merchant(**overrides) -> MerchantRecord:
    defaults = dict(
        order_id="ORD1", reference_id="ORD-1", amount_minor=100000, currency="INR",
        order_date=NOW, status="captured", refund_amount_minor=0, description="Order ORD1 - Premium Plan",
    )
    defaults.update(overrides)
    return MerchantRecord(**defaults)


def razorpay(**overrides) -> RazorpaySettlementRecord:
    defaults = dict(
        payment_id="pay_1", order_reference="ORD-1", settlement_id="setl_1",
        gross_amount_minor=100000, fee_minor=2000, tax_minor=360, net_amount_minor=97640,
        refund_amount_minor=0, order_date=NOW, settlement_date=NOW + timedelta(days=2),
        currency="INR", status="settled", description="Settlement for ORD1 (Premium Plan)",
    )
    defaults.update(overrides)
    return RazorpaySettlementRecord(**defaults)


def run(m, candidates, verifier=None, policy=None, pool=None):
    index = matching.ReferenceIndex(pool if pool is not None else candidates)
    return reconcile(m, candidates, index, policy or POLICY, verifier or StubVerifier(), record_id=m.order_id)


# ---------------------------------------------------------------------------
# Currency
# ---------------------------------------------------------------------------

def test_same_number_in_a_different_currency_is_not_reconciled():
    """Amounts are integers in minor units with no currency attached, so
    50000 paise compares equal to 50000 cents. Before the currency check
    existed, every other check passed and this reconciled clean."""
    m = merchant(currency="INR", amount_minor=100000)
    r = razorpay(currency="USD", gross_amount_minor=100000)
    result = run(m, [r])
    assert result.outcome is ReconciliationOutcome.EXCEPTION
    assert any(c.name == "currency_match" and c.status.value == "FAIL" for c in result.checks)


def test_currency_comparison_is_case_insensitive():
    result = run(merchant(currency="inr"), [razorpay(currency="INR")])
    assert result.outcome is ReconciliationOutcome.RECONCILED


# ---------------------------------------------------------------------------
# Candidate ranking — the defect that made the model look wrong
# ---------------------------------------------------------------------------

def test_shared_boilerplate_does_not_outrank_the_genuine_counterpart():
    """The original ranker scored candidates on unweighted description
    Jaccard. Template words ('payment for order customer checkout') are
    shared by every settlement, so an unrelated record with the same
    boilerplate outranked the true counterpart, which had an exact amount
    and a shared reference core but different wording."""
    m = merchant(
        order_id="ORD200427", reference_id="ORD.200427.CHK", amount_minor=1337600,
        description="Payment for Order 200427 - Widget Bundle - customer checkout",
    )
    true_match = razorpay(
        payment_id="pay_200427", order_reference="RZP/200427/SETL", gross_amount_minor=1337600,
        fee_minor=26752, tax_minor=4815, net_amount_minor=1306033,
        description="Settlement note: order 200427 widget bundle razorpay reference differs",
    )
    boilerplate_decoy = razorpay(
        payment_id="pay_999999", order_reference="RZP/999999/SETL", gross_amount_minor=1157400,
        fee_minor=23148, tax_minor=4167, net_amount_minor=1130085,
        description="Payment for Order 999999 Consulting Session customer checkout settlement",
    )
    index = matching.ReferenceIndex([boilerplate_decoy, true_match])
    shortlist = matching.build_shortlist(m, index, POLICY)
    assert shortlist[0][0].payment_id == "pay_200427"


def test_ranker_surfaces_a_counterpart_that_shares_no_wording_at_all():
    """Amount agreement is indexed, so a true counterpart is found even
    when reference and description have nothing in common."""
    m = merchant(order_id="ORD5", reference_id="ORD-5", amount_minor=477700, description="Order 5 Starter Kit")
    true_match = razorpay(payment_id="pay_x", order_reference="ZZZ", gross_amount_minor=477700,
                          fee_minor=9554, tax_minor=1720, net_amount_minor=466426,
                          description="collection reference unavailable")
    index = matching.ReferenceIndex([true_match])
    assert matching.build_shortlist(m, index, POLICY)[0][0].payment_id == "pay_x"


def test_shared_digits_plus_equal_amount_alone_does_not_auto_match():
    """An invoice counter on one side can carry the same digits as an
    order number on the other. Identifier and amount agreement without
    the wording corroborating the subject is ambiguity, so it must
    escalate rather than resolve deterministically."""
    m = merchant(order_id="ORD7", reference_id="ORD-4821", amount_minor=250000,
                 description="Order 4821 Premium Plan")
    collision = razorpay(payment_id="pay_other", order_reference="INV-4821", gross_amount_minor=250000,
                         fee_minor=5000, tax_minor=900, net_amount_minor=244100,
                         order_date=NOW + timedelta(days=2),
                         description="Data Export Service batch invoice 4821")
    verifier = StubVerifier(verdict="DIFFERENT", confidence=0.9)
    result = run(m, [], verifier=verifier, pool=[collision])
    assert verifier.calls >= 1, "should have escalated to the semantic verifier, not auto-matched"
    assert result.matched_payment_id is None


# ---------------------------------------------------------------------------
# Reference normalization edge cases
# ---------------------------------------------------------------------------

def test_missing_reference_does_not_crash_and_falls_through_to_matching():
    result = run(merchant(reference_id=None), [], pool=[razorpay()])
    assert result.outcome in tuple(ReconciliationOutcome)


@pytest.mark.parametrize("ref", ["", "   ", "---", "///", "!!!"])
def test_references_that_normalize_to_nothing_never_match_by_reference(ref):
    """A reference of only punctuation normalizes to an empty string. If
    that empty key were looked up, every record with an equally empty
    reference would collide into one giant bucket."""
    assert normalize.normalize_reference(ref) == ""
    index = matching.ReferenceIndex([razorpay(order_reference=ref)])
    assert index.exact_candidates(merchant(reference_id=ref)) == []


def test_unicode_and_width_variants_normalize_to_the_same_reference():
    assert normalize.normalize_reference("ord‑58291") == "ORD58291"       # non-ASCII hyphen
    assert normalize.normalize_reference("  ORD 58291  ") == "ORD58291"
    assert normalize.normalize_reference("ORD#58291/A") == "ORD58291A"


def test_unicode_text_does_not_break_tokenisation():
    assert normalize.token_set("Café — Ünïcode ördér 123") >= {"123"}
    assert 0.0 <= normalize.jaccard("Café ordér", "cafe order") <= 1.0


def test_reference_cores_ignore_short_digit_runs():
    """Three-digit runs collide constantly across a real population, so
    they are not treated as identifiers."""
    assert normalize.reference_cores("ORD-123") == set()
    assert "58291" in normalize.reference_cores("ORD-58291")


# ---------------------------------------------------------------------------
# Amounts, refunds, timing boundaries
# ---------------------------------------------------------------------------

def test_negative_amounts_are_rejected_at_the_boundary():
    with pytest.raises(Exception):
        merchant(amount_minor=-1)
    with pytest.raises(Exception):
        razorpay(gross_amount_minor=-5)


def test_zero_amount_record_is_handled_without_dividing_by_zero():
    m = merchant(amount_minor=0)
    r = razorpay(gross_amount_minor=0, fee_minor=0, tax_minor=0, net_amount_minor=0)
    assert run(m, [r]).outcome is ReconciliationOutcome.RECONCILED


def test_settlement_delay_exactly_at_the_threshold_is_allowed():
    r = razorpay(settlement_date=NOW + timedelta(days=POLICY.max_settlement_delay_days))
    assert run(merchant(), [r]).outcome is ReconciliationOutcome.RECONCILED


def test_settlement_delay_one_day_past_the_threshold_is_an_exception():
    r = razorpay(settlement_date=NOW + timedelta(days=POLICY.max_settlement_delay_days + 1))
    assert run(merchant(), [r]).outcome is ReconciliationOutcome.EXCEPTION


def test_sub_day_settlement_lag_is_not_counted_as_a_day():
    r = razorpay(settlement_date=NOW + timedelta(hours=23, minutes=59))
    assert run(merchant(), [r]).outcome is ReconciliationOutcome.RECONCILED


def test_cumulative_partial_refunds_must_reconcile_against_the_settlement():
    """Two partial refunds on the merchant side sum to what Razorpay
    recorded once — the amounts have to agree in total, not per event."""
    m = merchant(status="partially_refunded", refund_amount_minor=15000)
    r = razorpay(status="partially_refunded", refund_amount_minor=15000, net_amount_minor=82640)
    assert run(m, [r]).outcome is ReconciliationOutcome.RECONCILED


def test_partial_refund_totals_that_disagree_are_an_exception():
    m = merchant(status="partially_refunded", refund_amount_minor=15000)
    r = razorpay(status="partially_refunded", refund_amount_minor=9000, net_amount_minor=88640)
    assert run(m, [r]).outcome is ReconciliationOutcome.EXCEPTION


def test_refund_recorded_on_only_one_side_is_an_exception():
    m = merchant(status="refunded", refund_amount_minor=100000)
    assert run(m, [razorpay()]).outcome is ReconciliationOutcome.EXCEPTION


# ---------------------------------------------------------------------------
# Batch-level integrity
# ---------------------------------------------------------------------------

def test_one_settlement_claimed_by_two_merchant_records_is_detected():
    """Each record is judged alone, so both look reconciled. The
    double-claim only exists across records, and is invisible to any
    per-record check."""
    shared = razorpay(payment_id="pay_shared", order_reference="SHARED-REF")
    records = [
        ReconciliationRecord(record_id="A", merchant=merchant(order_id="A", reference_id="SHARED-REF")),
        ReconciliationRecord(record_id="B", merchant=merchant(order_id="B", reference_id="SHARED-REF")),
    ]
    results = process_batch(records, [shared], policy=POLICY, semantic_verifier=StubVerifier())
    collisions = detect_duplicate_claims(results)
    assert collisions == {"pay_shared": ["A", "B"]}


def test_one_to_one_matching_reports_no_collisions():
    records = [ReconciliationRecord(record_id="A", merchant=merchant(order_id="A", reference_id="R1"))]
    pool = [razorpay(payment_id="pay_1", order_reference="R1")]
    results = process_batch(records, pool, policy=POLICY, semantic_verifier=StubVerifier())
    assert detect_duplicate_claims(results) == {}


def test_empty_batch_produces_no_results_and_does_not_raise():
    assert process_batch([], [razorpay()], policy=POLICY, semantic_verifier=StubVerifier()) == []


def test_batch_with_records_but_no_settlements_at_all():
    records = [ReconciliationRecord(record_id="A", merchant=merchant(order_id="A"))]
    results = process_batch(records, [], policy=POLICY, semantic_verifier=StubVerifier())
    assert results[0].outcome is ReconciliationOutcome.EXCEPTION


def test_concurrent_batches_do_not_corrupt_each_others_results():
    """process_batch holds no shared mutable state; this pins that down
    so a future optimisation with a module-level cache fails here rather
    than in production."""
    pool = [razorpay(payment_id=f"pay_{i}", order_reference=f"R{i}", gross_amount_minor=100000 + i,
                     net_amount_minor=97640 + i) for i in range(20)]
    outputs: dict[int, list] = {}

    def work(worker: int):
        records = [
            ReconciliationRecord(record_id=f"w{worker}-{i}",
                                 merchant=merchant(order_id=f"w{worker}-{i}", reference_id=f"R{i}",
                                                   amount_minor=100000 + i))
            for i in range(20)
        ]
        outputs[worker] = process_batch(records, pool, policy=POLICY, semantic_verifier=StubVerifier())

    threads = [threading.Thread(target=work, args=(w,)) for w in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(outputs) == 4
    for results in outputs.values():
        assert len(results) == 20
        assert all(r.outcome is ReconciliationOutcome.RECONCILED for r in results)


# ---------------------------------------------------------------------------
# Persistence: re-running the same records
# ---------------------------------------------------------------------------

def _save(batch_id: str, record_id: str):
    m = merchant(order_id=record_id)
    result = run(m, [razorpay()])
    store.save_record(batch_id, 0, ReconciliationRecord(record_id=record_id, merchant=m), result, [razorpay()])


def test_re_running_the_same_records_keeps_both_batches_intact():
    """`records` was keyed on record_id alone with INSERT OR REPLACE, so
    a second run over the same dataset moved rows out of the first batch:
    that batch still reported its original processed_records but could no
    longer list them."""
    store.create_batch("batch_one", "first", "dev", 1)
    _save("batch_one", "ORD_SAME")
    store.create_batch("batch_two", "second", "dev", 1)
    _save("batch_two", "ORD_SAME")

    assert len(store.list_records("batch_one")) == 1, "first batch lost its record to the re-run"
    assert len(store.list_records("batch_two")) == 1


def test_record_lookup_can_select_a_specific_batch():
    store.create_batch("batch_one", "first", "dev", 1)
    _save("batch_one", "ORD_SAME")
    store.create_batch("batch_two", "second", "dev", 1)
    _save("batch_two", "ORD_SAME")

    assert store.get_record("ORD_SAME", "batch_one")["batch_id"] == "batch_one"
    assert store.get_record("ORD_SAME", "batch_two")["batch_id"] == "batch_two"
    assert store.get_record("ORD_SAME") is not None  # unqualified lookup still resolves


# ---------------------------------------------------------------------------
# Audit stream plumbing
# ---------------------------------------------------------------------------

def test_events_since_returns_only_newer_events():
    for i in range(5):
        audit.append_event(transaction_id=f"t{i}", event_type="RECORD_DECIDED",
                           prior_state=None, new_state="RECONCILED", payload={"i": i})
    head = audit.head_seq()
    assert head == 5
    assert audit.get_events_since(3) == audit.get_events_since(3, limit=500)
    assert [e["seq"] for e in audit.get_events_since(3)] == [4, 5]
    assert audit.get_events_since(head) == []


def test_events_since_is_bounded_by_limit():
    for i in range(30):
        audit.append_event(transaction_id=f"t{i}", event_type="RECORD_DECIDED",
                           prior_state=None, new_state="RECONCILED", payload={"i": i})
    assert len(audit.get_events_since(0, limit=10)) == 10


def test_head_seq_of_an_empty_log_is_zero():
    assert audit.head_seq() == 0


# ---------------------------------------------------------------------------
# Semantic verifier robustness
# ---------------------------------------------------------------------------

def _comparison() -> CandidateComparison:
    from app.engine.semantic import RecordSide
    return CandidateComparison(
        merchant=RecordSide("ORD-1", "Order 1 Premium Plan", 100000, NOW),
        candidate=RecordSide("RZP-1", "settlement premium plan", 100000, NOW),
        amount_exact_match=True, amount_delta_minor=0, days_apart=0,
        shared_reference_core=False, text_similarity=0.5,
    )


def test_a_malformed_model_response_cannot_reconcile_a_record():
    """A verdict outside the allowed set must not be treated as SAME."""
    class Malformed:
        def compare(self, comparison):
            return SemanticVerdictResult("BANANA", 0.99, "nonsense", "malformed")

    result = run(merchant(reference_id="NOPE"), [], verifier=Malformed(), pool=[razorpay(order_reference="OTHER")])
    assert result.outcome is not ReconciliationOutcome.RECONCILED
    assert result.matched_payment_id is None


def test_an_overconfident_model_still_cannot_exceed_the_policy_gate():
    """Confidence above 1.0 is nonsense, but the gate is a comparison
    against the threshold, so this documents that it still cannot force a
    reconcile on a record whose arithmetic fails."""
    verifier = StubVerifier(verdict="SAME", confidence=99.0)
    m = merchant(reference_id="NOPE", amount_minor=100000)
    r = razorpay(order_reference="OTHER", gross_amount_minor=555555,
                 fee_minor=11111, tax_minor=2000, net_amount_minor=542444)
    assert run(m, [], verifier=verifier, pool=[r]).outcome is not ReconciliationOutcome.RECONCILED


def test_rate_limit_retry_delay_is_read_from_the_provider_response():
    """On a 429 the client waits for the delay Google actually asked for
    rather than a guess, then retries once. This pins the parsing of that
    response shape, which is the part that silently rots when the SDK
    changes."""
    from app.engine import semantic

    class RetryInfoError(Exception):
        details = {"error": {"details": [
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "7s"},
        ]}}

    assert semantic._retry_delay_seconds(RetryInfoError(), default=3.0) == 7.0


def test_retry_delay_falls_back_to_the_default_on_an_unexpected_shape():
    from app.engine import semantic

    class Weird(Exception):
        details = {"nothing": "useful"}

    assert semantic._retry_delay_seconds(Weird(), default=2.5) == 2.5
    assert semantic._retry_delay_seconds(RuntimeError("no details at all"), default=1.5) == 1.5


def test_heuristic_backend_never_returns_confidence_above_its_cap():
    """The offline fallback must not be able to clear the policy gate on
    its own — it has no real judgment to justify it."""
    verifier = HeuristicSemanticVerifier()
    result = verifier.compare(_comparison())
    assert result.confidence <= 0.75 or result.verdict != "SAME"


# ---------------------------------------------------------------------------
# Search bounds
# ---------------------------------------------------------------------------

def test_window_scan_is_bounded_so_a_large_population_stays_tractable():
    pool = [
        razorpay(payment_id=f"pay_{i}", order_reference=f"REF{i}", gross_amount_minor=500000 + i,
                 net_amount_minor=487640 + i, order_date=NOW + timedelta(days=i % 5))
        for i in range(3000)
    ]
    index = matching.ReferenceIndex(pool)
    policy = PolicyConfig(max_window_scan_candidates=50)
    found = index.nearby_by_date(merchant(), policy.candidate_search_window_days, limit=50)
    assert len(found) == 50


def test_shortlist_never_exceeds_the_configured_size():
    pool = [
        razorpay(payment_id=f"pay_{i}", order_reference=f"REF{i}", gross_amount_minor=100000,
                 net_amount_minor=97640)
        for i in range(50)
    ]
    index = matching.ReferenceIndex(pool)
    shortlist = matching.build_shortlist(merchant(), index, POLICY)
    assert len(shortlist) <= POLICY.candidate_shortlist_size
