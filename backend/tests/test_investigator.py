"""
Breakpoint analysis and the exception investigator.

Every test here is offline. The provider chain is always injected as a
fake, because the properties being pinned are exactly the ones a live
provider would make unfalsifiable: that deterministic arithmetic needs no
model call at all, that a stage nobody supplied data for is reported as
not evaluated rather than missing, that a settlement which is not due yet
is never called missing, and that anything a model asserts which the
evidence does not contain is removed before a caller can see it.

The tests that matter most are the refusals. An investigator that
produces a confident-sounding narrative is easy; one that declines to say
"no bank credit found" when nobody uploaded a bank statement is the
product.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.domain.models import (
    MerchantRecord,
    PolicyConfig,
    RazorpaySettlementRecord,
    ReconciliationRecord,
)
from app.engine import investigate as inv
from app.engine.investigate import (
    InvestigationContext,
    Investigator,
    SourceRoles,
    build_trace,
)
from app.ledger import audit, db

NOW = datetime(2026, 3, 1, tzinfo=timezone.utc)
BATCH = "batch_investigate"


# ---------------------------------------------------------------------------
# Fixtures and builders
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "investigate.db")
    db._local.__dict__.clear()
    db.init_db()
    yield
    db._local.__dict__.clear()


class FakeChain:
    """Stands in for `providers.FallbackChain`, and records every call so a
    test can assert that no call happened at all."""

    def __init__(self, payload=None, *, status="AI_AVAILABLE", provider="fake-gemini", error=None):
        self._payload = payload
        self._status = status
        self._provider = provider
        self._error = error
        self.calls: list[dict] = []

    @property
    def status(self) -> str:
        return self._status

    def complete_json(self, *, system: str, user: str, schema: dict, timeout_s: float = 30.0):
        self.calls.append({"system": system, "user": user, "schema": schema, "timeout_s": timeout_s})
        if self._error is not None:
            raise self._error
        return self._payload, self._provider


def _merchant_dict(order_id: str, **overrides) -> dict:
    merchant = {
        "order_id": order_id,
        "reference_id": f"{order_id}-REF",
        "amount_minor": 50000,
        "currency": "INR",
        "order_date": NOW.isoformat(),
        "status": "captured",
        "refund_amount_minor": 0,
        "description": f"Order {order_id} - Premium Plan",
    }
    merchant.update(overrides)
    return merchant


def _row(record_id: str, *, merchant: dict | None = None, outcome="EXCEPTION",
         classification="NO_CANDIDATES", exception_type="MISSING_SETTLEMENT",
         severity="HIGH", reason="", checks=None, considered=None, candidates=None,
         matched_payment_id=None, batch_id=BATCH) -> dict:
    """A stored decision, exactly as `store.get_record` returns one."""
    return {
        "record_id": record_id,
        "batch_id": batch_id,
        "seq_in_batch": 0,
        "merchant_json": json.dumps(merchant or _merchant_dict(record_id)),
        "checks_json": json.dumps(checks or []),
        "considered_json": json.dumps(considered or []),
        "candidates_json": json.dumps(candidates or []),
        "candidate_count": len(candidates or []),
        "matched_payment_id": matched_payment_id,
        "outcome": outcome,
        "reason": reason,
        "classification": classification,
        "exception_type": exception_type,
        "severity": severity,
        "explanation": "",
        "recommended_action": "",
        "review_state": "OPEN",
    }


def _assessment(payment_id: str, *, reference="RZP/9999/S", gross=50000, admissible=False,
                admissibility_reason="amount agreement is the only signal, which is not evidence of identity",
                supporting=None, contradicting=None, score=0.55) -> dict:
    return {
        "payment_id": payment_id,
        "order_reference": reference,
        "gross_amount_minor": gross,
        "settlement_date": None,
        "supporting_signals": supporting if supporting is not None else ["amount matches exactly"],
        "contradicting_signals": contradicting if contradicting is not None else [],
        "evidence_score": score,
        "admissible": admissible,
        "admissibility_reason": admissibility_reason,
        "semantic_verdict": None,
        "semantic_confidence": None,
    }


def _settlement(payment_id: str, *, reference="RZP/1/S", gross=50000, fee=0, tax=0,
                net=None, description="Razorpay payout", when=NOW) -> RazorpaySettlementRecord:
    return RazorpaySettlementRecord(
        payment_id=payment_id,
        order_reference=reference,
        settlement_id=f"setl_{payment_id}",
        gross_amount_minor=gross,
        fee_minor=fee,
        tax_minor=tax,
        net_amount_minor=net if net is not None else gross - fee - tax,
        refund_amount_minor=0,
        order_date=when,
        settlement_date=when + timedelta(days=2),
        currency="INR",
        status="settled",
        description=description,
    )


GATEWAY_RUN = [
    {"source_id": "src_orders", "source_type": "ORDERS", "role": "LEDGER", "row_count": 3},
    {"source_id": "src_gw", "source_type": "PAYMENT_GATEWAY", "role": "SETTLEMENT", "row_count": 3},
]


def _context(record: dict, **kwargs) -> InvestigationContext:
    kwargs.setdefault("sources", list(GATEWAY_RUN))
    kwargs.setdefault("policy", PolicyConfig())
    return InvestigationContext(record=record, **kwargs)


def _stage(investigation, name: str):
    return next(s for s in investigation.trace if s.stage == name)


# ---------------------------------------------------------------------------
# Deterministic arithmetic — the model is not involved and must not be
# ---------------------------------------------------------------------------

def test_aggregated_settlement_arithmetic_is_confirmed_without_a_model_call():
    """Three unmatched orders whose total, less the fee and tax the payout
    file records, equals one credit. That is arithmetic, and arithmetic is
    not something to ask a language model about."""
    chain = FakeChain({"hypotheses": [], "explanation": "should never be called"})

    a = _row("ORD-A", merchant=_merchant_dict("ORD-A", amount_minor=20000))
    b = _row("ORD-B", merchant=_merchant_dict("ORD-B", amount_minor=18000))
    c = _row("ORD-C", merchant=_merchant_dict("ORD-C", amount_minor=12000))
    credit = _settlement("BANKCR1", gross=48200, fee=1525, tax=275, net=48200,
                         description="NEFT credit Razorpay payout")

    context = _context(a, siblings=[b, c], settlements=[credit])
    result = Investigator(chain=chain).investigate(context, write_audit=False)

    assert chain.calls == [], "deterministic arithmetic must not spend a model call"
    assert result.ai_used is False

    labels = [h.label for h in result.hypotheses]
    assert "AGGREGATED_SETTLEMENT" == labels[0]
    assert "GATEWAY_FEE_DEDUCTION" in labels
    assert "TAX_DEDUCTION" in labels
    assert all(h.source == "DETERMINISTIC" for h in result.hypotheses)

    arithmetic = next(e for e in result.confirmed_evidence if "BANKCR1" in e)
    for expected in ("₹500.00", "₹482.00", "₹18.00", "₹15.25", "₹2.75"):
        assert expected in arithmetic, f"{expected} missing from: {arithmetic}"
    assert result.recommended_action == "HUMAN_REVIEW"
    assert any("cannot be booked automatically" in u for u in result.unresolved)


def test_an_aggregation_the_batch_pass_already_found_is_surfaced_without_a_model_call():
    """`batch.detect_aggregated_settlements` runs during reconciliation and
    records what it found. Investigation surfaces that finding; it does not
    re-derive it, and it certainly does not ask a model to confirm it."""
    chain = FakeChain({"hypotheses": [], "explanation": "should never be called"})
    row = _row(
        "ORD-AGG",
        outcome="HUMAN_REVIEW",
        classification="NO_ADMISSIBLE_CANDIDATE",
        exception_type="AGGREGATED_SETTLEMENT",
        severity="MEDIUM",
        reason="Settlement pay_bundle matches the combined total of this record and ORD-B, ORD-C.",
        checks=[{"name": "aggregated_settlement", "status": "WARN",
                 "expected": "one settlement per record",
                 "observed": "3 records sum to pay_bundle",
                 "detail": "Grouping is proposed, not applied."}],
    )
    result = Investigator(chain=chain).investigate(_context(row), write_audit=False)

    assert chain.calls == []
    top = result.hypotheses[0]
    assert top.label == "AGGREGATED_SETTLEMENT"
    assert top.source == "DETERMINISTIC"
    assert "3 records sum to pay_bundle" in top.rationale
    assert _stage(result, "PAYMENT").status == "AMBIGUOUS"
    assert result.recommended_action == "HUMAN_REVIEW"


def test_aggregation_is_not_claimed_when_more_than_one_grouping_adds_up():
    """A lump sum that decomposes several ways is evidence of nothing —
    the same discipline batch.py applies when it proposes a grouping."""
    a = _row("ORD-A", merchant=_merchant_dict("ORD-A", amount_minor=10000))
    b = _row("ORD-B", merchant=_merchant_dict("ORD-B", amount_minor=10000))
    c = _row("ORD-C", merchant=_merchant_dict("ORD-C", amount_minor=10000))
    credit = _settlement("BANKCR2", gross=20000)

    context = _context(a, siblings=[b, c], settlements=[credit])
    result = Investigator(chain=None).investigate(context, use_ai=False, write_audit=False)

    aggregated = [h for h in result.hypotheses if h.label == "AGGREGATED_SETTLEMENT"]
    assert aggregated, "the grouping is still worth surfacing"
    assert aggregated[0].confidence < 0.5, "but not as an established fact"
    assert any("more than one combination" in u.lower() for u in result.unresolved)


# ---------------------------------------------------------------------------
# Pending is not missing
# ---------------------------------------------------------------------------

def test_pending_settlement_and_missing_settlement_are_different_breakpoints():
    """'Wait, it is not due yet' and 'chase the provider' are different
    instructions, and the whole product's credibility rests on the
    difference."""
    due = (NOW + timedelta(days=2)).date().isoformat()
    pending = _row(
        "ORD-PENDING",
        classification="PENDING_SETTLEMENT_WINDOW",
        exception_type="PENDING_SETTLEMENT",
        severity="LOW",
        reason=f"No settlement yet, but none is due until {due} (T+2).",
        checks=[{"name": "settlement_presence", "status": "PASS",
                 "expected": f"settlement due {due}", "observed": "not yet due",
                 "detail": f"No settlement yet, but none is due until {due} (T+2)."}],
    )
    missing = _row(
        "ORD-MISSING",
        classification="NO_CANDIDATES",
        exception_type="MISSING_SETTLEMENT",
        reason="No settlement record was retrieved within the search window.",
    )

    pending_result = Investigator(chain=None).investigate(_context(pending), use_ai=False, write_audit=False)
    missing_result = Investigator(chain=None).investigate(_context(missing), use_ai=False, write_audit=False)

    assert pending_result.breakpoint_kind == "PENDING"
    assert missing_result.breakpoint_kind == "MISSING"
    assert pending_result.breakpoint_kind != missing_result.breakpoint_kind

    assert _stage(pending_result, "PAYMENT").status == "PENDING"
    assert _stage(missing_result, "PAYMENT").status == "MISSING"

    assert [h.label for h in pending_result.hypotheses][0] == "PENDING_SETTLEMENT"
    assert "GENUINELY_MISSING" in [h.label for h in missing_result.hypotheses]

    # And the action differs: one waits, the other is raised.
    assert pending_result.recommended_action == "INVESTIGATE"
    assert missing_result.recommended_action == "EXCEPTION"


def test_pending_record_never_reports_a_missing_stage():
    due = (NOW + timedelta(days=2)).date().isoformat()
    pending = _row(
        "ORD-PENDING",
        classification="PENDING_SETTLEMENT_WINDOW",
        exception_type="PENDING_SETTLEMENT",
        reason=f"No settlement yet, but none is due until {due} (T+2).",
    )
    result = Investigator(chain=None).investigate(_context(pending), use_ai=False, write_audit=False)
    assert not any(s.status == "MISSING" for s in result.trace)


# ---------------------------------------------------------------------------
# A stage nobody supplied data for was not evaluated — it is not missing
# ---------------------------------------------------------------------------

def test_a_run_with_no_bank_source_marks_bank_not_evaluated():
    """Claiming 'no bank credit found' for a run that never received a
    bank statement is a fabricated finding, not a conservative one."""
    reconciled = _row(
        "ORD-OK",
        outcome="RECONCILED",
        classification="EXACT_REFERENCE",
        exception_type=None,
        severity=None,
        reason="All checks passed.",
        matched_payment_id="pay_1",
        checks=[{"name": "gross_amount_match", "status": "PASS", "expected": "50000", "observed": "50000",
                 "detail": "Merchant order amount matches the Razorpay gross amount."}],
        candidates=[{
            "payment_id": "pay_1", "order_reference": "ORD-OK-REF", "gross_amount_minor": 50000,
            "fee_minor": 1000, "tax_minor": 180, "net_amount_minor": 48820,
            "settlement_date": (NOW + timedelta(days=2)).isoformat(), "description": "Razorpay payout",
        }],
    )
    result = Investigator(chain=None).investigate(_context(reconciled), use_ai=False, write_audit=False)

    bank = _stage(result, "BANK")
    assert bank.status == "NOT_EVALUATED"
    assert bank.status != "MISSING"
    assert "No bank statement was included" in bank.detail
    assert _stage(result, "BOOKS").status == "NOT_EVALUATED"
    assert any("No bank statement was supplied" in u for u in result.unresolved)
    # Nothing beyond the two stages the run could actually check was claimed.
    assert result.breakpoint_stage is None
    assert result.breakpoint_kind == "NONE"


def test_bank_is_never_missing_even_when_the_settlement_side_broke():
    missing = _row("ORD-MISSING", reason="No settlement record was retrieved.")
    result = Investigator(chain=None).investigate(_context(missing), use_ai=False, write_audit=False)
    assert _stage(result, "BANK").status == "NOT_EVALUATED"
    assert result.breakpoint_stage == "PAYMENT"


def test_downstream_of_a_break_is_not_evaluated_rather_than_missing():
    """Once the trace stops there is nothing to say about the hops beyond
    it; asserting anything there would be a claim the run never tested."""
    missing = _row("ORD-MISSING")
    sources = [
        {"source_id": "o", "source_type": "ORDERS", "role": "LEDGER", "row_count": 1},
        {"source_id": "g", "source_type": "PAYMENT_GATEWAY", "role": "SETTLEMENT", "row_count": 1},
        {"source_id": "b", "source_type": "BANK_STATEMENT", "role": "SETTLEMENT", "row_count": 1},
    ]
    result = Investigator(chain=None).investigate(
        _context(missing, sources=sources), use_ai=False, write_audit=False
    )
    assert _stage(result, "SETTLEMENT").status == "NOT_EVALUATED"
    assert "trace stops at PAYMENT" in _stage(result, "SETTLEMENT").detail


def test_a_reference_contradiction_names_both_identifiers():
    contradicted = _row(
        "ORD-9001",
        merchant=_merchant_dict("ORD-9001", reference_id="ORD-9001"),
        classification="NO_ADMISSIBLE_CANDIDATE",
        considered=[_assessment(
            "pay_other", reference="RZP/7777/S",
            admissibility_reason="references identify different transactions",
            contradicting=["references identify different transactions"],
        )],
    )
    result = Investigator(chain=None).investigate(_context(contradicted), use_ai=False, write_audit=False)

    payment = _stage(result, "PAYMENT")
    assert payment.status == "CONTRADICTORY"
    assert result.breakpoint_kind == "CONTRADICTORY"
    assert "ORD-9001" in payment.detail and "RZP/7777/S" in payment.detail
    assert "CONFLICTING_IDENTITY" in [h.label for h in result.hypotheses]
    assert result.recommended_action == "HUMAN_REVIEW"


def test_ambiguous_multiple_lists_the_competing_candidates():
    ambiguous = _row(
        "ORD-DUP",
        outcome="HUMAN_REVIEW",
        classification="AMBIGUOUS_MULTIPLE",
        exception_type="AMBIGUOUS_MATCH",
        severity="MEDIUM",
        reason="Duplicate reference requires manual disambiguation.",
        considered=[
            _assessment("pay_a", reference="RZP/1/S", admissible=True, admissibility_reason="shared reference identifier"),
            _assessment("pay_b", reference="RZP/2/S", admissible=True, admissibility_reason="shared reference identifier"),
        ],
    )
    result = Investigator(chain=None).investigate(_context(ambiguous), use_ai=False, write_audit=False)
    payment = _stage(result, "PAYMENT")
    assert payment.status == "AMBIGUOUS"
    assert result.breakpoint_kind == "AMBIGUOUS"
    assert any("pay_a" in e for e in payment.evidence)
    assert any("pay_b" in e for e in payment.evidence)
    assert result.recommended_action == "HUMAN_REVIEW"


# ---------------------------------------------------------------------------
# The semantic residue is where — and only where — the model is used
# ---------------------------------------------------------------------------

def _alias_case(candidate_description: str) -> tuple[dict, list[RazorpaySettlementRecord]]:
    row = _row(
        "ORD-4410",
        merchant=_merchant_dict(
            "ORD-4410", reference_id="ORD-4410",
            description="Order 4410 - Acme Retail Private Limited subscription",
        ),
        classification="NO_ADMISSIBLE_CANDIDATE",
        reason="1 record was retrieved but none carried enough independent evidence.",
        considered=[_assessment("pay_alias", reference="RZP/5150/S")],
    )
    settlements = [_settlement("pay_alias", reference="RZP/5150/S", description=candidate_description)]
    return row, settlements


def test_a_merchant_alias_case_reaches_the_model_with_the_wording_in_the_prompt():
    chain = FakeChain({
        "hypotheses": [{
            "label": "MERCHANT_ALIAS",
            "confidence": 0.72,
            "rationale": "The settlement names ACME RTL, which is the same merchant as Acme Retail "
                         "Private Limited on the order.",
            "evidence_keys": ["CANDIDATE_1", "ORDER_DESCRIPTION"],
        }],
        "explanation": "The two sides name the same merchant under different trading names.",
    })
    row, settlements = _alias_case("ACME RTL settlement")
    result = Investigator(chain=chain).investigate(
        _context(row, settlements=settlements), write_audit=False
    )

    assert len(chain.calls) == 1, "the semantic residue is exactly one bounded call"
    prompt = chain.calls[0]["user"]
    assert "ACME RTL settlement" in prompt, "the model must be given the wording it is asked about"
    assert "Acme Retail Private Limited" in prompt

    alias = next(h for h in result.hypotheses if h.label == "MERCHANT_ALIAS")
    assert alias.source == "AI"
    assert result.ai_used is True
    assert result.ai_provider == "fake-gemini"
    assert result.ai_status == "AI_AVAILABLE"
    assert result.explanation == "The two sides name the same merchant under different trading names."


def test_a_truncated_narration_case_reaches_the_model():
    chain = FakeChain({
        "hypotheses": [{
            "label": "TRUNCATED_NARRATION",
            "confidence": 0.66,
            "rationale": "The bank narration is cut off mid-name, so it cannot be compared literally.",
            "evidence_keys": ["CANDIDATE_1"],
        }],
        "explanation": "The counterparty narration appears truncated by the bank's field length.",
    })
    row, settlements = _alias_case("NEFT/ACME RETAIL PRIVATE LIM")
    result = Investigator(chain=chain).investigate(
        _context(row, settlements=settlements), write_audit=False
    )

    assert len(chain.calls) == 1
    assert "NEFT/ACME RETAIL PRIVATE LIM" in chain.calls[0]["user"]
    assert "TRUNCATED_NARRATION" in [h.label for h in result.hypotheses]


def test_the_model_is_only_given_the_bounded_evidence():
    """It never sees the batch, the stored narrative, or anything it was
    not structurally handed."""
    chain = FakeChain({"hypotheses": [], "explanation": ""})
    row, settlements = _alias_case("ACME RTL settlement")
    row["explanation"] = "SECRET-ENGINE-NARRATIVE"
    Investigator(chain=chain).investigate(_context(row, settlements=settlements), write_audit=False)

    payload = json.loads(chain.calls[0]["user"])
    assert set(payload) == {
        "record_id", "breakpoint", "order", "settlement_side_candidates",
        "evidence", "already_established_by_code",
    }
    assert "SECRET-ENGINE-NARRATIVE" not in chain.calls[0]["user"]


# ---------------------------------------------------------------------------
# The grounding filter
# ---------------------------------------------------------------------------

def test_a_model_claim_absent_from_the_evidence_is_stripped():
    """The single most important behaviour in this module: a plausible
    sentence containing an amount nobody supplied is removed entirely
    rather than shown, edited, or softened."""
    chain = FakeChain({
        "hypotheses": [{
            "label": "MERCHANT_ALIAS",
            "confidence": 0.91,
            "rationale": "A bank credit of ₹99,999.00 on 2027-04-19 under reference UTR8813347 settles this "
                         "order.",
            "evidence_keys": ["CANDIDATE_1"],
        }],
        "explanation": "The order was settled by credit UTR8813347 for ₹99,999.00.",
    })
    row, settlements = _alias_case("ACME RTL settlement")
    result = Investigator(chain=chain).investigate(
        _context(row, settlements=settlements), write_audit=False
    )

    assert result.ai_claims_dropped >= 2, "both the hypothesis and the explanation are ungrounded"
    assert all(h.source == "DETERMINISTIC" for h in result.hypotheses)
    serialised = json.dumps(result.to_dict(), default=str)
    assert "99,999" not in serialised
    assert "UTR8813347" not in serialised
    assert any("did not support" in u for u in result.unresolved)


def test_a_hypothesis_citing_no_real_evidence_key_is_dropped():
    kept, explanation, dropped = inv.filter_model_payload(
        {
            "hypotheses": [
                {"label": "MERCHANT_ALIAS", "confidence": 0.8, "rationale": "Same trading name.",
                 "evidence_keys": ["INVENTED_KEY"]},
                {"label": "TRUNCATED_NARRATION", "confidence": 0.7, "rationale": "Cut off.",
                 "evidence_keys": ["CANDIDATE_1"]},
            ],
            "explanation": "Both sides name one merchant.",
        },
        evidence_index={"CANDIDATE_1": "Candidate pay_alias reads ACME RTL."},
        deterministic_labels=set(),
    )
    assert [h.label for h in kept] == ["TRUNCATED_NARRATION"]
    assert dropped == 1
    assert explanation == "Both sides name one merchant."


def test_labels_outside_the_models_remit_are_dropped():
    """Arithmetic conclusions are not the model's to draw, however
    confidently it draws them."""
    kept, _, dropped = inv.filter_model_payload(
        {
            "hypotheses": [
                {"label": "AGGREGATED_SETTLEMENT", "confidence": 0.99, "rationale": "It adds up.",
                 "evidence_keys": ["CANDIDATE_1"]},
                {"label": "PENDING_SETTLEMENT", "confidence": 0.99, "rationale": "Not due.",
                 "evidence_keys": ["CANDIDATE_1"]},
                {"label": "TOTALLY_MADE_UP", "confidence": 0.99, "rationale": "Trust me.",
                 "evidence_keys": ["CANDIDATE_1"]},
            ],
            "explanation": "",
        },
        evidence_index={"CANDIDATE_1": "Candidate pay_alias reads ACME RTL."},
        deterministic_labels=set(),
    )
    assert kept == []
    assert dropped == 3


# ---------------------------------------------------------------------------
# Policy decides, always
# ---------------------------------------------------------------------------

def test_two_unrelated_records_at_the_same_amount_never_yield_reconcile():
    """An exact amount collision is ordinary in a population of thousands.
    The engine refuses it as evidence of identity, and no amount of model
    enthusiasm may turn it into a reconciliation."""
    chain = FakeChain({
        "hypotheses": [{
            "label": "MERCHANT_ALIAS", "confidence": 0.99,
            "rationale": "These are obviously the same payment and should be reconciled.",
            "evidence_keys": ["CANDIDATE_1"],
        }],
        "explanation": "Reconcile this record against the candidate.",
    })
    twin = _row("ORD-TWIN", merchant=_merchant_dict("ORD-TWIN", amount_minor=50000))
    subject = _row(
        "ORD-5001",
        merchant=_merchant_dict("ORD-5001", amount_minor=50000, reference_id="ORD-5001"),
        classification="NO_ADMISSIBLE_CANDIDATE",
        considered=[_assessment("pay_coincidence", reference="RZP/8888/S", gross=50000)],
    )
    result = Investigator(chain=chain).investigate(
        _context(subject, siblings=[twin]), write_audit=False
    )

    assert result.recommended_action != "RECONCILE"
    assert result.recommended_action in ("EXCEPTION", "HUMAN_REVIEW", "INVESTIGATE")
    collision = next(e for e in result.confirmed_evidence if "identical amount" in e)
    assert "₹500.00" in collision


def test_an_amount_mismatch_can_never_be_reconciled():
    matched = _row(
        "ORD-7000",
        classification="EXACT_REFERENCE",
        exception_type="AMOUNT_MISMATCH",
        matched_payment_id="pay_7000",
        reason="gross_amount_match: amounts differ",
        checks=[
            {"name": "gross_amount_match", "status": "FAIL", "expected": "50000 minor units",
             "observed": "45000 minor units", "detail": "Merchant recorded 50000 but gross was 45000."},
        ],
        candidates=[{
            "payment_id": "pay_7000", "order_reference": "ORD-7000-REF", "gross_amount_minor": 45000,
            "fee_minor": 900, "tax_minor": 162, "net_amount_minor": 43938,
            "settlement_date": (NOW + timedelta(days=2)).isoformat(), "description": "payout",
        }],
    )
    result = Investigator(chain=None).investigate(_context(matched), use_ai=False, write_audit=False)

    assert result.recommended_action == "EXCEPTION"
    assert _stage(result, "PAYMENT").status == "FOUND"
    assert _stage(result, "SETTLEMENT").status == "CONTRADICTORY"
    assert result.breakpoint_stage == "SETTLEMENT"


def test_a_currency_mismatch_can_never_be_reconciled():
    matched = _row(
        "ORD-7100",
        classification="EXACT_REFERENCE",
        exception_type="CURRENCY_MISMATCH",
        matched_payment_id="pay_7100",
        checks=[{"name": "currency_match", "status": "FAIL", "expected": "INR", "observed": "USD",
                 "detail": "Merchant recorded INR but the settlement is in USD."}],
    )
    result = Investigator(chain=None).investigate(_context(matched), use_ai=False, write_audit=False)
    assert result.recommended_action == "EXCEPTION"


# ---------------------------------------------------------------------------
# Degrading is a correct outcome, not an error
# ---------------------------------------------------------------------------

def test_both_providers_unavailable_returns_the_deterministic_investigation():
    chain = FakeChain(None, status="AI_UNAVAILABLE")
    row, settlements = _alias_case("ACME RTL settlement")
    result = Investigator(chain=chain).investigate(
        _context(row, settlements=settlements), write_audit=False
    )

    assert chain.calls == []
    assert result.ai_used is False
    assert result.ai_provider is None
    assert result.ai_status == "AI_UNAVAILABLE"
    assert result.hypotheses, "deterministic findings still stand"
    assert result.explanation, "and are still explained, deterministically"
    assert any("No language provider was available" in u for u in result.unresolved)


def test_a_provider_layer_that_does_not_exist_yet_degrades_cleanly():
    """`providers.py` is written by another agent and may simply not be
    importable. That is a degraded outcome, not a 500."""
    def _explode():
        raise ImportError("no module named app.engine.providers")

    row, settlements = _alias_case("ACME RTL settlement")
    result = Investigator(chain_factory=_explode).investigate(
        _context(row, settlements=settlements), write_audit=False
    )
    assert result.ai_used is False
    assert result.ai_status == "AI_UNAVAILABLE"
    assert result.recommended_action in ("EXCEPTION", "HUMAN_REVIEW", "INVESTIGATE")


def test_a_provider_that_raises_mid_call_does_not_fabricate_an_explanation():
    chain = FakeChain(None, error=RuntimeError("rate limited"))
    row, settlements = _alias_case("ACME RTL settlement")
    result = Investigator(chain=chain).investigate(
        _context(row, settlements=settlements), write_audit=False
    )
    assert len(chain.calls) == 1
    assert result.ai_used is False
    assert result.ai_status == "AI_UNAVAILABLE"
    assert any("did not answer" in u for u in result.unresolved)
    assert "The money trail stops at" in result.explanation


def test_a_malformed_provider_response_is_not_trusted():
    chain = FakeChain("not a json object")
    row, settlements = _alias_case("ACME RTL settlement")
    result = Investigator(chain=chain).investigate(
        _context(row, settlements=settlements), write_audit=False
    )
    assert result.ai_used is False
    assert all(h.source == "DETERMINISTIC" for h in result.hypotheses)


# ---------------------------------------------------------------------------
# Read-only, and auditable
# ---------------------------------------------------------------------------

def _persist_one_record() -> tuple[str, str]:
    """Run a real record through the engine and store it, so the audit test
    is asserting against a genuine stored decision rather than a fixture."""
    from app.engine.batch import process_batch
    from app.engine.matching import ReferenceIndex
    from app.ledger import store

    merchant = MerchantRecord(
        order_id="ORD-AUDIT", reference_id="ORD-AUDIT-REF", amount_minor=50000,
        currency="INR", order_date=NOW, status="captured", description="Order AUDIT - Widget",
    )
    record = ReconciliationRecord(record_id="ORD-AUDIT", merchant=merchant)
    results = process_batch([record], [], as_of=NOW + timedelta(days=10))
    store.create_batch(BATCH, "investigation test", "upload", 1)
    index = ReferenceIndex([])
    store.save_record(BATCH, 0, record, results[0], index.exact_candidates(merchant))
    return "ORD-AUDIT", results[0].outcome.value


def test_investigation_writes_exactly_one_audit_event_and_changes_nothing():
    from app.ledger import store

    record_id, outcome_before = _persist_one_record()
    context = inv.load_context(record_id, BATCH)
    assert context is not None

    result = Investigator(chain=None).investigate(context, use_ai=False)

    events = [e for e in audit.get_trail(record_id) if e["event_type"] == "AI_INVESTIGATION"]
    assert len(events) == 1
    payload = json.loads(events[0]["payload_json"])
    assert payload["breakpoint_stage"] == result.breakpoint_stage
    assert payload["hypothesis_labels"] == [h.label for h in result.hypotheses]
    assert payload["ai_used"] is False
    assert payload["read_only"] is True
    assert events[0]["prior_state"] == events[0]["new_state"] == outcome_before

    stored = store.get_record(record_id, BATCH)
    assert stored["outcome"] == outcome_before, "an investigation must never move a record"
    assert stored["review_state"] == "OPEN"
    assert audit.verify_chain()["intact"] is True


def test_the_audit_event_carries_no_prompt_and_no_model_text():
    record_id, _ = _persist_one_record()
    chain = FakeChain({
        "hypotheses": [{"label": "MERCHANT_ALIAS", "confidence": 0.6,
                        "rationale": "MODEL-PROSE-MARKER same trading name.",
                        "evidence_keys": ["ORDER_AMOUNT"]}],
        "explanation": "MODEL-PROSE-MARKER explanation.",
    })
    context = inv.load_context(record_id, BATCH)
    context.record["considered_json"] = json.dumps([_assessment("pay_x")])
    Investigator(chain=chain).investigate(context)

    events = [e for e in audit.get_trail(record_id) if e["event_type"] == "AI_INVESTIGATION"]
    assert len(events) == 1
    blob = events[0]["payload_json"]
    assert "MODEL-PROSE-MARKER" not in blob
    assert "You are a reconciliation analyst" not in blob
    payload = json.loads(blob)
    assert payload["ai_provider"] == "fake-gemini"
    assert payload["ai_hypothesis_labels"] == ["MERCHANT_ALIAS"]


def test_the_investigation_exposes_no_hidden_reasoning_field():
    chain = FakeChain({
        "hypotheses": [{"label": "MERCHANT_ALIAS", "confidence": 0.6, "rationale": "Same trading name.",
                        "evidence_keys": ["ORDER_AMOUNT"]}],
        "explanation": "The two sides name one merchant.",
        "reasoning": "STEP 1 I considered... STEP 2 I concluded...",
        "chain_of_thought": "hidden working that must never surface",
        "recommended_action": "RECONCILE",
    })
    row, settlements = _alias_case("ACME RTL settlement")
    result = Investigator(chain=chain).investigate(
        _context(row, settlements=settlements), write_audit=False
    )

    payload = result.to_dict()
    forbidden = {"reasoning", "chain_of_thought", "thinking", "scratchpad", "raw_response", "prompt"}
    assert forbidden.isdisjoint(payload)
    for hypothesis in payload["hypotheses"]:
        assert forbidden.isdisjoint(hypothesis)
    serialised = json.dumps(payload, default=str)
    assert "STEP 1" not in serialised
    assert "hidden working" not in serialised
    # And the model's own action suggestion is ignored — policy decides.
    assert result.recommended_action != "RECONCILE"


def test_at_most_one_model_call_is_spent_per_record():
    chain = FakeChain({"hypotheses": [], "explanation": ""})
    row, settlements = _alias_case("ACME RTL settlement")
    Investigator(chain=chain).investigate(_context(row, settlements=settlements), write_audit=False)
    assert len(chain.calls) <= inv.MAX_AI_CALLS


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(monkeypatch):
    from fastapi.testclient import TestClient

    import app.api.investigate as api_investigate
    from app.main import app

    # No provider, and no 48k-record dataset pool: these endpoints are
    # tested for their own behaviour, offline.
    monkeypatch.setattr(api_investigate, "_chain", lambda: None)
    monkeypatch.setattr(api_investigate, "_dataset_settlements", lambda: [])
    # The orchestrator wires this router into main.py; until it does, the
    # endpoint tests register it themselves so they test the router rather
    # than the wiring.
    registered = {getattr(route, "path", None) for route in app.routes}
    if "/records/{record_id}/investigate" not in registered:
        app.include_router(api_investigate.router)
    with TestClient(app) as c:
        yield c


def _persist_batch() -> str:
    from app.engine.batch import process_batch
    from app.engine.matching import ReferenceIndex
    from app.ledger import store

    merchants = [
        MerchantRecord(order_id="ORD-1", reference_id="ORD-1-REF", amount_minor=50000,
                       order_date=NOW, status="captured", description="Order 1 - Widget"),
        MerchantRecord(order_id="ORD-2", reference_id="ORD-2-REF", amount_minor=25000,
                       order_date=NOW, status="captured", description="Order 2 - Gadget"),
    ]
    settlements = [_settlement("pay_1", reference="ORD-1-REF", gross=50000, fee=1000, tax=180)]
    records = [ReconciliationRecord(record_id=m.order_id, merchant=m) for m in merchants]
    results = process_batch(records, settlements, as_of=NOW + timedelta(days=10))

    store.create_batch(BATCH, "api test", "upload", len(records))
    index = ReferenceIndex(settlements)
    for i, (record, result) in enumerate(zip(records, results)):
        store.save_record(BATCH, i, record, result, index.exact_candidates(record.merchant))
    store.mark_batch_complete(BATCH)
    return BATCH


def test_investigate_endpoint_returns_an_investigation(client):
    batch_id = _persist_batch()
    response = client.post(f"/records/ORD-2/investigate?batch_id={batch_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["record_id"] == "ORD-2"
    assert body["breakpoint_stage"] == "PAYMENT"
    assert body["breakpoint_kind"] in ("MISSING", "CONTRADICTORY")
    assert body["ai_used"] is False
    assert [s["stage"] for s in body["trace"]] == list(inv.STAGES)
    assert body["confirmed_evidence"]
    assert body["recommended_action"] in ("RECONCILE", "EXCEPTION", "HUMAN_REVIEW", "INVESTIGATE")


def test_investigate_endpoint_404s_for_an_unknown_record(client):
    _persist_batch()
    assert client.post("/records/NOPE/investigate?batch_id=" + BATCH).status_code == 404


def test_breakpoints_endpoint_counts_by_stage_and_kind(client):
    batch_id = _persist_batch()
    response = client.get(f"/batch/{batch_id}/breakpoints")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total_records"] == 2
    assert sum(body["by_breakpoint_kind"].values()) == 2
    assert sum(body["by_breakpoint_stage"].values()) == 2
    assert body["by_breakpoint_stage"].get("NONE") == 1, "the matched record has no breakpoint"
    assert body["by_breakpoint_stage"].get("PAYMENT") == 1
    assert body["not_evaluated_counts"]["BANK"] == 2, "no bank statement in this run"


def test_breakpoints_endpoint_404s_for_an_unknown_batch(client):
    assert client.get("/batch/nope/breakpoints").status_code == 404


# ---------------------------------------------------------------------------
# Per-record source attribution.
#
# The trace used to fold every settlement-side match into PAYMENT and then
# report BANK as unevaluable, because mapped rows carried no per-file
# provenance. They do now. These pin the three answers apart, because the
# difference between "we checked the bank and found the credit", "we never
# checked the bank for this record" and "there was no bank statement" is
# the entire value of the stage.
# ---------------------------------------------------------------------------

def _view_with_settlement_origin(origin: dict | None):
    from app.engine.investigate import RecordView

    row = {
        "record_id": "ORD-5010",
        "batch_id": "run_prov",
        "outcome": "RECONCILED",
        "reason": "matched",
        "classification": "SEMANTIC_CONFIRMED",
        "matched_payment_id": "pay_TRACE01",
        "merchant_json": '{"amount_minor": 500000, "currency": "INR",'
                         ' "order_date": "2026-01-04T00:00:00+00:00"}',
        "checks_json": "[]",
        "considered_json": "[]",
        "candidates_json": "[]",
    }
    if origin is not None:
        row["provenance_json"] = json.dumps({"settlement": origin})
    return RecordView.from_row(row)


def _trace_stage(trace, name):
    return next(s for s in trace if s.stage == name)


BOTH_SIDES = SourceRoles(has_gateway=True, has_bank=True, has_orders=True, declared=True)


def test_match_traced_to_a_bank_row_reports_bank_found_and_cites_the_line():
    view = _view_with_settlement_origin(
        {"filename": "ICICI_January.csv", "source_type": "BANK_STATEMENT", "file_row": 4182}
    )
    trace = build_trace(view, BOTH_SIDES, PolicyConfig())

    bank = _trace_stage(trace, "BANK")
    assert bank.status == "FOUND"
    assert "matched ICICI_January.csv row 4182" in bank.evidence

    # A gateway file exists in the run but did not produce this match, and
    # the copy must say that rather than claiming none was uploaded.
    payment = _trace_stage(trace, "PAYMENT")
    assert payment.status == "NOT_EVALUATED"
    assert "No payment-gateway file was included" not in payment.detail


def test_match_traced_to_a_gateway_row_leaves_bank_unevaluated_not_missing():
    view = _view_with_settlement_origin(
        {"filename": "razorpay_jan.csv", "source_type": "PAYMENT_GATEWAY", "file_row": 88}
    )
    trace = build_trace(view, BOTH_SIDES, PolicyConfig())

    assert _trace_stage(trace, "PAYMENT").status == "FOUND"
    bank = _trace_stage(trace, "BANK")
    # The engine does not chain gateway -> bank, so the credit is unchecked.
    # Calling it MISSING would be a fabricated finding.
    assert bank.status == "NOT_EVALUATED"
    assert bank.status != "MISSING"


def test_absent_provenance_falls_back_to_the_run_level_answer():
    """A dataset run, or one recorded before provenance existed, must still
    produce a trace rather than crashing or inventing an attribution."""
    view = _view_with_settlement_origin(None)
    trace = build_trace(view, BOTH_SIDES, PolicyConfig())

    assert view.matched_source_type is None
    assert view.source_citation is None
    assert _trace_stage(trace, "PAYMENT").status == "FOUND"
    assert _trace_stage(trace, "BANK").status == "NOT_EVALUATED"


# ---------------------------------------------------------------------------
# "Not needed" is not "not working".
#
# When the arithmetic settles a record, no model is called. Reporting that
# as AI_UNAVAILABLE told the operator the system was degraded on exactly the
# records where it was working best.
# ---------------------------------------------------------------------------

def test_a_record_settled_deterministically_reports_not_consulted_not_unavailable():
    from app.engine.investigate import AI_NOT_CONSULTED, AI_UNAVAILABLE

    chain = FakeChain()
    reconciled = _row(
        "ORD-EXACT",
        outcome="RECONCILED",
        classification="EXACT_REFERENCE",
        exception_type=None,
        severity=None,
        matched_payment_id="pay_EXACT01",
    )
    result = Investigator(chain=chain).investigate(_context(reconciled), write_audit=False)

    assert chain.calls == []                      # no model call was made
    assert result.ai_used is False
    assert result.ai_status == AI_NOT_CONSULTED
    assert result.ai_status != AI_UNAVAILABLE     # the bug this pins


def test_a_pending_settlement_does_not_also_allege_conflicting_identity():
    """Nothing is due yet, so a same-amount row the index happened to
    surface contradicts nothing. Offering it at 0.85 sends an operator
    chasing a data problem that does not exist."""
    due = (NOW + timedelta(days=2)).date().isoformat()
    pending = _row(
        "ORD-PENDING-2",
        classification="PENDING_SETTLEMENT_WINDOW",
        exception_type="PENDING_SETTLEMENT",
        reason=f"No settlement yet, but none is due until {due} (T+2).",
        considered=[_assessment("pay_COINCIDENCE",
                                contradicting=["references identify different transactions"])],
    )
    result = Investigator(chain=None).investigate(_context(pending), use_ai=False, write_audit=False)

    labels = [h.label for h in result.hypotheses]
    assert "PENDING_SETTLEMENT" in labels
    assert "CONFLICTING_IDENTITY" not in labels
