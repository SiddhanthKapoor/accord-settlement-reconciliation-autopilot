"""
Admissibility, the settlement window, and explanation integrity.

The invariant under test throughout: the system must be able to say *why*
it refused, and that reason must be derived from evidence it actually
recorded. A refusal the engine cannot justify is indistinguishable from a
bug, and an explanation not grounded in recorded signals is worse — it
survives review right up until someone checks it.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.domain.models import (
    ExceptionType, MatchClassification, MerchantRecord, PolicyConfig, RazorpaySettlementRecord,
    ReconciliationOutcome, Severity,
)
from app.engine import matching
from app.engine.policy import reconcile
from app.engine.semantic import CandidateComparison, SemanticVerdictResult

NOW = datetime(2026, 3, 1, tzinfo=timezone.utc)
POLICY = PolicyConfig()


class StubVerifier:
    def __init__(self, verdict="SAME", confidence=0.95):
        self.verdict, self.confidence, self.calls = verdict, confidence, 0

    def compare(self, comparison: CandidateComparison) -> SemanticVerdictResult:
        self.calls += 1
        return SemanticVerdictResult(self.verdict, self.confidence, "stub", "stub")


def merchant(**overrides) -> MerchantRecord:
    defaults = dict(
        order_id="ORD77001", reference_id="ORD-77001", amount_minor=250000, currency="INR",
        order_date=NOW, status="captured", refund_amount_minor=0,
        description="Order 77001 Premium Plan",
    )
    defaults.update(overrides)
    return MerchantRecord(**defaults)


def settlement(**overrides) -> RazorpaySettlementRecord:
    defaults = dict(
        payment_id="pay_77001", order_reference="ORD-77001", settlement_id="setl_1",
        gross_amount_minor=250000, fee_minor=5000, tax_minor=900, net_amount_minor=244100,
        refund_amount_minor=0, order_date=NOW, settlement_date=NOW + timedelta(days=2),
        currency="INR", status="settled", description="Settlement for ORD77001 Premium Plan",
    )
    defaults.update(overrides)
    return RazorpaySettlementRecord(**defaults)


def run(m, pool, verifier=None, policy=None, as_of=None):
    index = matching.ReferenceIndex(pool)
    return reconcile(m, index.exact_candidates(m), index, policy or POLICY,
                     verifier or StubVerifier(), record_id=m.order_id, as_of=as_of)


# ---------------------------------------------------------------------------
# Amount is retrieval, not evidence
# ---------------------------------------------------------------------------

def test_an_exact_amount_alone_does_not_make_a_candidate():
    """The archetypal coincidence. In a population of thousands, two
    unrelated payments sharing an amount is ordinary."""
    m = merchant()
    coincidence = settlement(payment_id="pay_other", order_reference="RZP-99999",
                             description="Settlement for ORD99999 Gift Card")
    verifier = StubVerifier(verdict="SAME", confidence=0.99)

    result = run(m, [coincidence], verifier=verifier)

    assert result.outcome is ReconciliationOutcome.EXCEPTION
    assert result.matched_payment_id is None
    assert result.classification is MatchClassification.NO_ADMISSIBLE_CANDIDATE
    assert verifier.calls == 0, "a coincidence must not cost a model call"


def test_a_coincidence_is_still_reported_as_evidence_of_what_was_refused():
    """'No settlement found' is an assertion; naming the record that was
    considered and why it was refused is checkable."""
    m = merchant()
    coincidence = settlement(payment_id="pay_other", order_reference="RZP-99999",
                             description="Settlement for ORD99999 Gift Card")
    result = run(m, [coincidence])

    assert result.considered_candidates, "the rejected candidate must be surfaced, not discarded"
    rejected = result.considered_candidates[0]
    assert rejected.payment_id == "pay_other"
    assert rejected.admissible is False
    assert rejected.admissibility_reason
    assert "amount matches exactly" in rejected.supporting_signals
    assert rejected.contradicting_signals
    assert "pay_other" in result.explanation


def test_contradicting_references_beat_an_exact_amount_and_matching_dates():
    m = merchant(reference_id="ORD-77001")
    decoy = settlement(payment_id="pay_x", order_reference="INV-88888",
                       description="Settlement for ORD88888")
    result = run(m, [decoy])
    assert result.matched_payment_id is None
    rejected = result.considered_candidates[0]
    assert "different transactions" in rejected.admissibility_reason


def test_a_shared_reference_core_admits_a_reformatted_reference():
    """The counterpart of the rule above: the same identifier in different
    packaging is corroboration, not contradiction."""
    m = merchant(reference_id="ORD.77001.CHK")
    true_match = settlement(order_reference="RZP/77001/SETL",
                            description="Settlement note order 77001 premium plan")
    result = run(m, [true_match])
    assert result.outcome is ReconciliationOutcome.RECONCILED
    assert result.matched_payment_id == "pay_77001"


def test_disabling_the_contradiction_rule_readmits_corroborated_pairs():
    """The rule assumes the settlement reference derives from the
    merchant's. A provider using an unrelated internal id needs it off.

    What the switch governs is precisely a pair whose wording corroborates
    while the references disagree: on, that is refused as negative
    evidence; off, it reaches the semantic tier. It does not make
    amount-only candidates admissible — that is a separate guard, and it
    stays."""
    m = merchant(reference_id="ORD-77001", description="Premium Plan annual subscription renewal")
    decoy = settlement(payment_id="pay_x", order_reference="INV-88888",
                       description="Premium Plan annual subscription renewal")

    strict = StubVerifier(verdict="SAME", confidence=0.99)
    refused = run(m, [decoy], verifier=strict)
    assert refused.matched_payment_id is None, "contradicting references should refuse the pair"
    assert strict.calls == 0, "and should do so before spending a model call"

    lenient = run(m, [decoy], verifier=StubVerifier(verdict="SAME", confidence=0.99),
                  policy=PolicyConfig(treat_reference_contradiction_as_negative=False))
    assert lenient.matched_payment_id == "pay_x", \
        "with the rule off, identical wording plus an exact amount is enough to match"


def test_amount_only_stays_inadmissible_even_with_the_contradiction_rule_off():
    """The two guards are independent. Turning off contradiction detection
    must not quietly re-admit pure coincidences."""
    m = merchant()
    coincidence = settlement(payment_id="pay_other", order_reference="RZP-99999",
                             description="Settlement for ORD99999 Gift Card")
    verifier = StubVerifier(verdict="SAME", confidence=0.99)
    result = run(m, [coincidence], verifier=verifier,
                 policy=PolicyConfig(treat_reference_contradiction_as_negative=False))
    assert verifier.calls == 0
    assert result.matched_payment_id is None


def test_identity_evidence_requirement_can_be_disabled_entirely():
    m = merchant()
    coincidence = settlement(payment_id="pay_other", order_reference="RZP-99999",
                             description="Settlement for ORD99999 Gift Card")
    permissive = PolicyConfig(require_identity_evidence=False)
    verifier = StubVerifier(verdict="DIFFERENT", confidence=0.9)
    run(m, [coincidence], verifier=verifier, policy=permissive)
    assert verifier.calls >= 1


# ---------------------------------------------------------------------------
# Missing vs not yet due
# ---------------------------------------------------------------------------

def test_a_settlement_that_is_not_due_yet_is_not_reported_as_missing():
    """Chasing a provider for money that was never late is a false
    positive with a phone call attached."""
    m = merchant(order_date=NOW)
    result = run(m, [], as_of=NOW + timedelta(hours=6))

    assert result.classification is MatchClassification.PENDING_SETTLEMENT_WINDOW
    assert result.exception_type is ExceptionType.PENDING_SETTLEMENT
    assert result.severity is Severity.LOW
    assert "not due" in result.reason.lower() or "due" in result.reason.lower()
    assert "re-run" in result.recommended_action.lower() or "wait" in result.recommended_action.lower()


def test_a_settlement_past_the_window_is_genuinely_missing():
    m = merchant(order_date=NOW)
    result = run(m, [], as_of=NOW + timedelta(days=30))
    assert result.exception_type is ExceptionType.MISSING_SETTLEMENT
    assert result.severity is Severity.HIGH


def test_exactly_at_the_settlement_boundary_counts_as_due():
    m = merchant(order_date=NOW)
    at_boundary = NOW + timedelta(days=POLICY.settlement_expected_days)
    assert run(m, [], as_of=at_boundary).exception_type is ExceptionType.MISSING_SETTLEMENT


def test_without_an_observation_point_the_distinction_is_skipped_not_guessed():
    """No as_of means the question cannot be answered. Guessing at it
    would invent a finding."""
    m = merchant(order_date=NOW)
    result = run(m, [], as_of=None)
    assert result.classification is not MatchClassification.PENDING_SETTLEMENT_WINDOW
    assert result.exception_type is ExceptionType.MISSING_SETTLEMENT


def test_pending_does_not_override_a_real_match():
    """A settlement that exists is reconciled even if it arrived early."""
    m = merchant(order_date=NOW)
    result = run(m, [settlement()], as_of=NOW + timedelta(hours=1))
    assert result.outcome is ReconciliationOutcome.RECONCILED


# ---------------------------------------------------------------------------
# Explanation integrity
# ---------------------------------------------------------------------------

def test_every_non_reconciled_outcome_carries_an_actionable_explanation():
    cases = [
        ("missing", merchant(reference_id="NOPE-1"), []),
        ("coincidence", merchant(), [settlement(payment_id="pay_o", order_reference="RZP-99999",
                                                description="Settlement ORD99999 Gift Card")]),
        ("amount mismatch", merchant(), [settlement(gross_amount_minor=999999,
                                                    net_amount_minor=994099)]),
        ("currency", merchant(), [settlement(currency="USD")]),
    ]
    for label, m, pool in cases:
        result = run(m, pool)
        assert result.outcome is not ReconciliationOutcome.RECONCILED, label
        assert result.explanation, f"{label}: no explanation"
        assert result.recommended_action, f"{label}: no recommended action"
        assert result.exception_type is not None, f"{label}: unclassified exception"
        assert result.severity is not None, f"{label}: no severity"


def test_a_failing_check_drives_the_exception_type():
    assert run(merchant(), [settlement(currency="USD")]).exception_type is ExceptionType.CURRENCY_MISMATCH
    assert run(merchant(), [settlement(gross_amount_minor=999999, net_amount_minor=994099)]) \
        .exception_type is ExceptionType.AMOUNT_MISMATCH
    late = settlement(settlement_date=NOW + timedelta(days=POLICY.max_settlement_delay_days + 5))
    assert run(merchant(), [late]).exception_type is ExceptionType.SETTLEMENT_DELAYED


def test_currency_outranks_delay_when_both_fail():
    """Severity ordering has to be stable: money in the wrong currency is
    a different conversation from money that is late."""
    bad = settlement(currency="USD",
                     settlement_date=NOW + timedelta(days=POLICY.max_settlement_delay_days + 5))
    result = run(merchant(), [bad])
    assert result.exception_type is ExceptionType.CURRENCY_MISMATCH
    assert result.severity is Severity.HIGH


def test_a_clean_reconcile_carries_no_exception_type():
    result = run(merchant(), [settlement()])
    assert result.outcome is ReconciliationOutcome.RECONCILED
    assert result.exception_type is None
    assert result.severity is None
    assert result.explanation


def test_explanations_never_come_from_the_model():
    """The verifier returns a rationale; it must not leak into the
    operator-facing explanation. A plausible narration of a decision the
    model did not make is worse than none."""
    class ChattyVerifier:
        def compare(self, comparison):
            return SemanticVerdictResult("DIFFERENT", 0.9,
                                         "TOTALLY-FABRICATED-MODEL-PROSE", "stub")

    m = merchant(reference_id="NOEXACT")
    pool = [settlement(payment_id="pay_z", order_reference="NOEXACT2",
                       description="Settlement note premium plan order")]
    result = run(m, pool, verifier=ChattyVerifier())
    assert "TOTALLY-FABRICATED-MODEL-PROSE" not in result.explanation
    assert "TOTALLY-FABRICATED-MODEL-PROSE" not in result.recommended_action


def test_the_model_cannot_reconcile_a_record_whose_checks_fail():
    """The gate that matters: a confident SAME does not override
    deterministic arithmetic."""
    class ConfidentVerifier:
        def compare(self, comparison):
            return SemanticVerdictResult("SAME", 1.0, "sure", "stub")

    m = merchant(reference_id="NOEXACT")
    wrong_currency = settlement(payment_id="pay_z", order_reference="NOEXACT2", currency="USD",
                                description="Settlement note premium plan order 77001")
    result = run(m, [wrong_currency], verifier=ConfidentVerifier())
    assert result.outcome is not ReconciliationOutcome.RECONCILED


def test_candidate_assessments_are_serialisable():
    """They cross the API boundary and land in SQLite; a type that cannot
    round-trip breaks the review queue rather than the engine."""
    m = merchant()
    coincidence = settlement(payment_id="pay_other", order_reference="RZP-99999",
                             description="Settlement ORD99999 Gift Card")
    result = run(m, [coincidence])
    for assessment in result.considered_candidates:
        dumped = assessment.model_dump(mode="json")
        assert isinstance(dumped["evidence_score"], float)
        assert isinstance(dumped["supporting_signals"], list)
