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
    amount_minor: int = Field(..., description="Order amount the merchant expects to receive credit for, in paise.")
    currency: str = "INR"
    order_date: datetime
    status: Literal["captured", "refunded", "partially_refunded"]
    refund_amount_minor: int = Field(default=0, description="Merchant's own record of any refund issued.")
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
    gross_amount_minor: int
    fee_minor: int
    tax_minor: int
    net_amount_minor: int
    refund_amount_minor: int = 0
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

    model_config = {"frozen": True}


DEFAULT_POLICY = PolicyConfig()
