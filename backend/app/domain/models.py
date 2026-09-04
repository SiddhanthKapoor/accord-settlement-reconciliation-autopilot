"""
Domain model for the Settlement Reconciliation Autopilot.

Two sides, always: a merchant's own order record, and a Razorpay-style
settlement record. Reconciliation asks one question — does the merchant
side and the Razorpay side describe the same real-world money movement,
correctly accounted for (fees, tax, refunds, timing)? — and answers it
with exactly one of three outcomes, never a fourth "silently fine"
state.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ReconciliationOutcome(str, Enum):
    RECONCILED = "RECONCILED"
    EXCEPTION = "EXCEPTION"
    HUMAN_REVIEW = "HUMAN_REVIEW"


class CheckStatus(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


# ---------------------------------------------------------------------------
# The two source records
# ---------------------------------------------------------------------------

class MerchantRecord(BaseModel):
    """A row from the merchant's own order/transaction ledger — what the
    merchant believes happened. Never trusted alone; always reconciled
    against the Razorpay side."""

    order_id: str
    reference_id: Optional[str] = Field(
        default=None,
        description="The merchant's own record of the gateway reference for this "
        "order. Can be missing, malformed, or reformatted relative to what "
        "Razorpay actually stored — that mismatch is exactly what this system "
        "exists to resolve.",
    )
    amount_minor: int = Field(
        ..., ge=0,
        description="Order amount the merchant expects to receive credit for, in paise. Non-negative: a "
        "negative order amount is malformed input, and is rejected at the boundary rather than flowing "
        "into the scorer, where it would silently invert the amount-agreement signal.",
    )
    currency: str = "INR"
    order_date: datetime
    status: Literal["captured", "refunded", "partially_refunded"]
    refund_amount_minor: int = Field(default=0, ge=0, description="Merchant's own record of any refund issued.")
    description: str = Field(default="", description="Free-text order description, e.g. 'Order #58291 - Premium Plan'.")


class RazorpaySettlementRecord(BaseModel):
    """A row from Razorpay's settlement/payment data — what actually
    happened to the money. `gross_amount_minor` is what the customer paid;
    `net_amount_minor` is what actually settles to the merchant after fees
    and tax; the two are related by `net = gross - fee - tax - refund`,
    which is itself one of the deterministic checks, not an assumption."""

    payment_id: str
    order_reference: str = Field(description="Razorpay's own record of the merchant reference for this payment.")
    settlement_id: str
    gross_amount_minor: int = Field(ge=0)
    fee_minor: int = Field(ge=0)
    tax_minor: int = Field(ge=0)
    net_amount_minor: int
    refund_amount_minor: int = Field(default=0, ge=0)
    order_date: datetime = Field(description="When the payment was captured.")
    settlement_date: datetime = Field(description="When funds actually settled — can lag order_date by days.")
    currency: str = "INR"
    status: Literal["settled", "refunded", "partially_refunded"]
    description: str = Field(default="", description="Free-text settlement note from Razorpay's side.")


# ---------------------------------------------------------------------------
# Ground truth (synthetic evaluation only — never available to the engine
# at decision time, only used afterward to score it)
# ---------------------------------------------------------------------------

class GroundTruth(BaseModel):
    case: str = Field(description="The synthetic scenario category this record was generated to exercise.")
    expected_outcome: ReconciliationOutcome


# ---------------------------------------------------------------------------
# A unit of work: one merchant record to reconcile. Candidates are NOT
# stored here — they're looked up at processing time from the batch's
# shared ReferenceIndex over the full Razorpay-side population, which is
# both more realistic (a real system doesn't pre-know its matches) and
# necessary (the fuzzy/semantic fallback needs the full population, not
# just whatever one record's exact matches happened to be).
# ---------------------------------------------------------------------------

class ReconciliationRecord(BaseModel):
    record_id: str
    merchant: MerchantRecord
    ground_truth: Optional[GroundTruth] = None


# ---------------------------------------------------------------------------
# Decision output
# ---------------------------------------------------------------------------

class CheckResult(BaseModel):
    name: str
    status: CheckStatus
    expected: Optional[str] = None
    observed: Optional[str] = None
    detail: str
    confidence: Optional[float] = Field(default=None, description="Set only for the AI-assisted reference check.")


class ReconciliationResult(BaseModel):
    record_id: str
    outcome: ReconciliationOutcome
    reason: str
    checks: list[CheckResult]
    matched_payment_id: Optional[str] = None
    candidate_count: int
    ai_invoked: bool = False
    ai_calls: int = Field(default=0, description="Model calls actually spent on this record — the unit real API cost is billed in.")
    ai_confidence: Optional[float] = None
    ai_backend: Optional[str] = None
    policy_threshold: float
    latency_ms: float
    decided_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------------
# Policy — the one place thresholds live, so they're inspectable and never
# implicit in scattered code.
# ---------------------------------------------------------------------------

class PolicyConfig(BaseModel):
    amount_tolerance_minor: int = Field(default=2, description="Rounding tolerance for amount/arithmetic checks, in paise.")
    max_settlement_delay_days: int = Field(default=21, description="Beyond this, a settlement delay is treated as an exception, not normal variance.")
    ai_confidence_threshold: float = Field(
        default=0.85,
        description="An AI-resolved reference match below this confidence can never be auto-RECONCILED — "
        "it is routed to HUMAN_REVIEW regardless of how clean the rest of the arithmetic looks. This is "
        "enforced in policy.py, not by trusting the model to self-limit.",
    )
    fuzzy_reference_jaccard_strong: float = Field(default=0.6, description="Token-overlap threshold above which a fuzzy reference match is resolved deterministically, without calling the model.")
    fuzzy_reference_jaccard_floor: float = Field(default=0.2, description="Below this, there's not even enough textual overlap to justify escalating to the model — treated as no candidate.")
    candidate_search_window_days: int = Field(default=21, description="How far from the merchant's order_date to look for a fuzzy/semantic candidate — bounds the search, mirrors how a real system would window-scan rather than full-scan.")

    max_window_scan_candidates: int = Field(
        default=400,
        description="Upper bound on how many date-window candidates are scored when neither the amount index "
        "nor the reference-core index produced anything. Keeps a batch close to linear in record count instead "
        "of records x settlement population; see matching.ReferenceIndex.nearby_by_date.",
    )
    candidate_shortlist_size: int = Field(
        default=5,
        description="How many ranked candidates are kept for consideration. Bounds both the deterministic "
        "margin check and the maximum semantic escalation.",
    )
    deterministic_match_score: float = Field(
        default=0.70,
        description="Composite evidence score at or above which a candidate can be matched without a model "
        "call — but only when at least two independent signals corroborate (see matching.CandidateSignals). "
        "Kept below the 0.75 ceiling that a candidate with no shared reference core can reach, so an exact "
        "amount on the same day with strongly corroborating wording still resolves without a model call.",
    )
    deterministic_min_text_similarity: float = Field(
        default=0.25,
        description="Descriptions must corroborate the subject this much before a match is resolved without "
        "a model call. Identifier and amount agreement alone is not proof: an invoice counter on one side "
        "can collide with an order number on the other, at the same amount, days apart. When the wording "
        "does not back up the identifiers, that is ambiguity and it escalates.",
    )
    deterministic_match_margin: float = Field(
        default=0.10,
        description="How far the best candidate must lead the runner-up to be resolved deterministically. "
        "A near-tie is genuine ambiguity and belongs to the semantic verifier or a human, not to whichever "
        "record happened to sort first.",
    )
    max_semantic_calls_per_record: int = Field(
        default=3,
        description="Hard ceiling on model calls for a single record, so an unresolvable record cannot fan "
        "out into unbounded API cost or latency.",
    )
    enable_fuzzy_matching: bool = Field(
        default=True,
        description="Ablation/production switch: when off, only exact normalized-reference matching is used.",
    )
    enable_semantic_matching: bool = Field(
        default=True,
        description="Ablation/production switch: when off, no model is ever called and unresolved ambiguity "
        "falls through to the deterministic outcome. A merchant that wants a strictly deterministic pipeline "
        "sets this and loses recall, not safety.",
    )

    model_config = {"frozen": True}


DEFAULT_POLICY = PolicyConfig()
