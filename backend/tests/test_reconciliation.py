"""
Engine-level tests, hand-constructed (not the generated dataset — this
suite needs precise control over each scenario, independent of whatever
the dataset generator happens to produce). Covers every category the
product spec calls out by name: exact matches, fee/tax normalization,
partial refunds, delayed settlements, missing settlements, duplicate
references, ambiguous matching, AI failure fallback, timeout handling,
invalid data, policy threshold enforcement.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from pydantic import ValidationError

from app.domain.models import (
    MerchantRecord, PolicyConfig, RazorpaySettlementRecord, ReconciliationOutcome,
)
from app.engine import matching
from app.engine.policy import reconcile
from app.engine.semantic import CandidateComparison, SemanticVerdictResult

NOW = datetime(2026, 3, 1, tzinfo=timezone.utc)
POLICY = PolicyConfig()


class StubVerifier:
    """A semantic verifier whose answer is fixed by the test, so policy
    tests don't depend on real model behavior."""

    def __init__(self, verdict="AMBIGUOUS", confidence=0.5, backend="stub"):
        self.verdict = verdict
        self.confidence = confidence
        self.backend = backend
        self.calls = 0

    def compare(self, comparison: CandidateComparison) -> SemanticVerdictResult:
        self.calls += 1
        return SemanticVerdictResult(verdict=self.verdict, confidence=self.confidence, rationale="stub", backend=self.backend)


class RaisingVerifier:
    def compare(self, comparison):
        raise RuntimeError("simulated provider outage")




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


def _reconcile(m, candidates, verifier=None):
    index = matching.ReferenceIndex(candidates)
    return reconcile(m, candidates, index, POLICY, verifier or StubVerifier(), record_id=m.order_id)


def _reconcile_fuzzy(m, pool, verifier, policy=None):
    """For the fuzzy/semantic-fallback tests: `pool` populates the index
    (so the fuzzy/semantic search can find it) but is deliberately NOT
    passed as `candidates` — reconcile() gets an empty exact-candidates
    list, exactly like a real 'no exact reference match' record, forcing
    the fuzzy fallback path in matching.resolve_fuzzy_or_semantic."""
    index = matching.ReferenceIndex(pool)
    return reconcile(m, [], index, policy or POLICY, verifier, record_id=m.order_id)


# --------------------------------------------------------------- exact match

def test_exact_match_reconciles():
    result = _reconcile(merchant(), [razorpay()])
    assert result.outcome == ReconciliationOutcome.RECONCILED
    assert result.matched_payment_id == "pay_1"
    assert all(c.status.value != "FAIL" for c in result.checks)


def test_exact_match_no_ai_invoked():
    result = _reconcile(merchant(), [razorpay()])
    assert result.ai_invoked is False


# --------------------------------------------------------- fee/tax normalization

def test_fee_tax_rounding_within_tolerance_reconciles():
    r = razorpay(net_amount_minor=97640 + POLICY.amount_tolerance_minor)  # exactly at the edge
    result = _reconcile(merchant(), [r])
    assert result.outcome == ReconciliationOutcome.RECONCILED


def test_fee_tax_arithmetic_beyond_tolerance_is_exception():
    r = razorpay(net_amount_minor=97640 + POLICY.amount_tolerance_minor + 500)
    result = _reconcile(merchant(), [r])
    assert result.outcome == ReconciliationOutcome.EXCEPTION
    failing = [c.name for c in result.checks if c.status.value == "FAIL"]
    assert "fee_tax_arithmetic" in failing


# --------------------------------------------------------------- partial refunds

def test_partial_refund_consistent_reconciles():
    m = merchant(status="partially_refunded", refund_amount_minor=30000)
    r = razorpay(status="partially_refunded", refund_amount_minor=30000, net_amount_minor=100000 - 2000 - 360 - 30000)
    result = _reconcile(m, [r])
    assert result.outcome == ReconciliationOutcome.RECONCILED


def test_refund_mismatch_is_exception():
    m = merchant(status="partially_refunded", refund_amount_minor=30000)
    r = razorpay(status="partially_refunded", refund_amount_minor=50000, net_amount_minor=100000 - 2000 - 360 - 50000)
    result = _reconcile(m, [r])
    assert result.outcome == ReconciliationOutcome.EXCEPTION
    assert any(c.name == "refund_consistency" and c.status.value == "FAIL" for c in result.checks)


# ------------------------------------------------------------- delayed settlement

def test_settlement_within_policy_window_reconciles():
    r = razorpay(settlement_date=NOW + timedelta(days=POLICY.max_settlement_delay_days))
    result = _reconcile(merchant(), [r])
    assert result.outcome == ReconciliationOutcome.RECONCILED


def test_settlement_beyond_policy_window_is_exception():
    r = razorpay(settlement_date=NOW + timedelta(days=POLICY.max_settlement_delay_days + 1))
    result = _reconcile(merchant(), [r])
    assert result.outcome == ReconciliationOutcome.EXCEPTION
    assert any(c.name == "settlement_timing" and c.status.value == "FAIL" for c in result.checks)


# ------------------------------------------------------------- missing settlement

def test_missing_settlement_with_no_plausible_candidate_is_exception():
    result = _reconcile(merchant(reference_id="ORD-999-NOTHING-CLOSE"), [], verifier=RaisingVerifier())
    assert result.outcome == ReconciliationOutcome.EXCEPTION
    assert any(c.name == "settlement_presence" for c in result.checks)


# ------------------------------------------------------------- duplicate references

def test_duplicate_reference_undisambiguated_is_human_review():
    r1 = razorpay(payment_id="pay_1")
    r2 = razorpay(payment_id="pay_2", settlement_id="setl_2")  # identical amount, same reference
    result = _reconcile(merchant(), [r1, r2])
    assert result.outcome == ReconciliationOutcome.HUMAN_REVIEW
    assert any(c.name == "duplicate_reference" for c in result.checks)


def test_duplicate_reference_disambiguated_by_amount_reconciles():
    r1 = razorpay(payment_id="pay_1")
    r2 = razorpay(payment_id="pay_2", settlement_id="setl_2", gross_amount_minor=250000, net_amount_minor=245000)
    result = _reconcile(merchant(), [r1, r2])  # merchant amount only matches r1
    assert result.outcome == ReconciliationOutcome.RECONCILED
    assert result.matched_payment_id == "pay_1"


# ------------------------------------------------------------- ambiguous matching / AI gating

def test_ambiguous_reference_resolved_by_confident_ai_reconciles():
    m = merchant(reference_id="NO-EXACT-MATCH", description="Order ORD1 - Premium Plan checkout")
    r = razorpay(order_reference="DIFFERENT-REF", description="Settlement note order premium plan")
    verifier = StubVerifier(verdict="SAME", confidence=0.95)
    result = _reconcile_fuzzy(m, [r], verifier)
    assert verifier.calls == 1
    assert result.ai_invoked is True
    assert result.outcome == ReconciliationOutcome.RECONCILED


def test_low_confidence_ai_match_is_human_review_never_reconciled():
    """The core policy guarantee: AI can never independently reconcile a
    record below the configured confidence threshold, no matter how
    clean the rest of the arithmetic looks."""
    m = merchant(reference_id="NO-EXACT-MATCH", description="Order ORD1 - Premium Plan checkout")
    r = razorpay(order_reference="DIFFERENT-REF", description="Settlement note order premium plan")
    verifier = StubVerifier(verdict="SAME", confidence=POLICY.ai_confidence_threshold - 0.01)
    result = _reconcile_fuzzy(m, [r], verifier)
    assert result.ai_invoked is True
    assert result.outcome == ReconciliationOutcome.HUMAN_REVIEW
    assert result.outcome != ReconciliationOutcome.RECONCILED


def test_ai_says_ambiguous_routes_to_human_review():
    m = merchant(reference_id="NO-EXACT-MATCH", description="Order ORD1 - Premium Plan checkout")
    r = razorpay(order_reference="DIFFERENT-REF", description="Settlement note order premium plan")
    verifier = StubVerifier(verdict="AMBIGUOUS", confidence=0.5)
    result = _reconcile_fuzzy(m, [r], verifier)
    assert result.outcome == ReconciliationOutcome.HUMAN_REVIEW


def test_ai_says_different_is_exception_not_reconciled():
    m = merchant(reference_id="NO-EXACT-MATCH", description="Order ORD1 - Premium Plan checkout")
    r = razorpay(order_reference="DIFFERENT-REF", description="Settlement note order premium plan")
    verifier = StubVerifier(verdict="DIFFERENT", confidence=0.9)
    result = _reconcile_fuzzy(m, [r], verifier)
    assert result.outcome == ReconciliationOutcome.EXCEPTION


def test_strong_fuzzy_match_never_calls_the_model():
    """Token overlap above the deterministic threshold resolves without
    ever invoking the semantic verifier — the AI is for genuinely
    ambiguous cases only."""
    m = merchant(reference_id="NO-EXACT-MATCH", description="Order ORD1 - Premium Plan")
    r = razorpay(order_reference="DIFFERENT-REF", description="Order ORD1 Premium Plan settlement")
    verifier = StubVerifier()
    result = _reconcile_fuzzy(m, [r], verifier)
    assert verifier.calls == 0
    assert result.ai_invoked is False
    assert result.outcome == ReconciliationOutcome.RECONCILED


# ------------------------------------------------------------- AI failure fallback / timeout

def test_ai_provider_failure_falls_back_to_human_review_not_crash_or_reconcile():
    """A provider outage is a visible, specific outcome — HUMAN_REVIEW,
    with the reason naming the AI failure — never a silent
    auto-reconciliation, and never an unhandled exception that would
    take down whatever batch this record is part of."""
    m = merchant(reference_id="NO-EXACT-MATCH", description="Order ORD1 - Premium Plan checkout")
    r = razorpay(order_reference="DIFFERENT-REF", description="Settlement note order premium plan")
    result = _reconcile_fuzzy(m, [r], RaisingVerifier())
    assert result.outcome == ReconciliationOutcome.HUMAN_REVIEW
    assert "provider error" in result.reason.lower()


def test_timeout_handling_bounds_a_hanging_provider(monkeypatch):
    """A provider that hangs far longer than the engine's own timeout
    ceiling must still be cut off — this monkeypatches the timeout down
    to 0.5s and uses a verifier that sleeps 3s, so a passing test proves
    the ThreadPoolExecutor-based timeout wrapper is actually enforced,
    not just that the simulated provider eventually raised on its own."""
    import time

    monkeypatch.setattr(matching, "SEMANTIC_CALL_TIMEOUT_SECONDS", 0.5)

    class SlowHangingVerifier:
        def compare(self, comparison):
            time.sleep(3)
            return SemanticVerdictResult(verdict="SAME", confidence=0.99, rationale="too slow to matter", backend="slow-stub")

    m = merchant(reference_id="NO-EXACT-MATCH", description="Order ORD1 - Premium Plan checkout")
    r = razorpay(order_reference="DIFFERENT-REF", description="Settlement note order premium plan")
    started = time.perf_counter()
    result = _reconcile_fuzzy(m, [r], SlowHangingVerifier())
    elapsed = time.perf_counter() - started
    assert result.outcome == ReconciliationOutcome.HUMAN_REVIEW
    assert "did not respond" in result.reason.lower()
    assert elapsed < 2.0, "the 0.5s timeout should have cut this off long before the verifier's 3s sleep completed"


# ------------------------------------------------------------- invalid data

def test_invalid_status_literal_rejected():
    with pytest.raises(ValidationError):
        merchant(status="not_a_real_status")


def test_negative_amount_is_rejected_before_it_reaches_the_engine():
    """A negative order amount is corrupt input, not a reconcilable
    business condition. It used to be accepted and scored, where it
    inverted the amount-agreement signal (the relative-difference term
    goes negative, pushing the component above 1.0). Rejecting it at the
    model boundary is what stops that; loaders skip and report such rows
    rather than aborting a whole batch."""
    with pytest.raises(ValidationError):
        merchant(amount_minor=-100)


def test_missing_reference_and_no_description_overlap_is_exception():
    m = merchant(reference_id=None, description="")
    result = _reconcile(m, [], verifier=RaisingVerifier())
    assert result.outcome == ReconciliationOutcome.EXCEPTION


# ------------------------------------------------------------- policy threshold enforcement

def test_policy_threshold_is_configurable_and_enforced():
    strict_policy = PolicyConfig(ai_confidence_threshold=0.99)
    m = merchant(reference_id="NO-EXACT-MATCH", description="Order ORD1 - Premium Plan checkout")
    r = razorpay(order_reference="DIFFERENT-REF", description="Settlement note order premium plan")
    verifier = StubVerifier(verdict="SAME", confidence=0.95)  # would pass the default 0.85 threshold
    index = matching.ReferenceIndex([r])
    result = reconcile(m, [], index, strict_policy, verifier, record_id=m.order_id)
    assert result.outcome == ReconciliationOutcome.HUMAN_REVIEW
    assert result.policy_threshold == 0.99
