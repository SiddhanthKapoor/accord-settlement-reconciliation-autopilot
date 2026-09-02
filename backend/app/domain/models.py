"""
Core domain model for Interlock.

Naming follows AP2 terminology where the concepts line up (open/closed mandate,
constraints) so the mapping to the protocol gap (T-31 replay, T-32 state
mutation, T-33 shared-budget races) is explicit rather than invented.

Money is always represented in integer minor units (paise) internally.
Floats are never hashed or compared for equality — that's how naive
"just hash the JSON" implementations silently break.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Transaction lifecycle
# ---------------------------------------------------------------------------

class TransactionState(str, Enum):
    DECLARED = "DECLARED"                # intent/constraints registered
    DISCOVERED = "DISCOVERED"            # agent observed catalog evidence
    SELECTED = "SELECTED"                # agent selected a specific product
    CARTED = "CARTED"                    # item placed in cart
    CHECKOUT_READY = "CHECKOUT_READY"    # commitment created (canonical, hashed)
    PAYMENT_REQUESTED = "PAYMENT_REQUESTED"
    ALLOWED = "ALLOWED"
    BLOCKED = "BLOCKED"
    REQUIRES_RECONFIRMATION = "REQUIRES_RECONFIRMATION"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"


class DecisionOutcome(str, Enum):
    ALLOW = "ALLOW"
    BLOCK = "BLOCK"
    REQUIRE_RECONFIRMATION = "REQUIRE_RECONFIRMATION"


class CheckStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    WARN = "WARN"          # e.g. within tolerance but non-zero drift
    SKIPPED = "SKIPPED"


# ---------------------------------------------------------------------------
# Step A: declared intent / constraints (AP2 "open mandate" analogue)
# ---------------------------------------------------------------------------

class Constraints(BaseModel):
    max_amount_minor: int = Field(..., gt=0, description="Max spend, minor units (paise)")
    currency: str = Field(default="INR")
    allowed_categories: Optional[list[str]] = None
    allowed_merchants: Optional[list[str]] = None
    max_quantity: int = Field(default=1, gt=0)
    single_use: bool = Field(
        default=True,
        description="If true, this budget may back exactly one committed transaction "
        "(closes T-31/T-33 at the model level: a consumed or reserved budget cannot "
        "be spent again or spent twice concurrently).",
    )
    expires_at: Optional[datetime] = None
    price_tolerance_pct: float = Field(
        default=0.0,
        ge=0.0,
        le=100.0,
        description="Allowed benign price drift vs. catalog ground truth before it "
        "is treated as a violation rather than normal variance.",
    )


class TransactionIntent(BaseModel):
    intent_id: str
    constraints: Constraints
    declared_natural_language: Optional[str] = Field(
        default=None,
        description="Optional free-text of what the user asked for, e.g. "
        "'a healthy snack under ₹200'. Used ONLY as input evidence to the "
        "narrow semantic classifier — never trusted directly for money decisions.",
    )
    created_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------------
# Step B: catalog evidence (independently fetched from the merchant, never
# trusted from the agent's own claim)
# ---------------------------------------------------------------------------

class ProductRef(BaseModel):
    merchant_id: str
    product_id: str
    name: str
    category: str
    price_minor: int = Field(..., ge=0)
    currency: str = "INR"
    available: bool = True


class CatalogEvidence(BaseModel):
    evidence_id: str
    intent_id: str
    product: ProductRef
    fetched_at: datetime = Field(default_factory=utcnow)
    source: str = Field(description="e.g. 'catalog_service:/products/{id}'")


# ---------------------------------------------------------------------------
# Step C: commitment (AP2 "closed mandate" analogue) — canonical, hashed
# ---------------------------------------------------------------------------

class Commitment(BaseModel):
    commitment_id: str
    intent_id: str
    merchant_id: str
    product_id: str
    product_name: str
    category: str
    quantity: int = Field(..., gt=0)
    price_minor: int = Field(..., ge=0)
    currency: str = "INR"
    evidence_id: str = Field(description="CatalogEvidence this was carted from")
    created_at: datetime = Field(default_factory=utcnow)
    version: int = 1
    content_hash: str = Field(description="sha256 of the canonical commercial fields")


# ---------------------------------------------------------------------------
# Payment request — the "final observed state" reaching the payment layer
# ---------------------------------------------------------------------------

class PaymentRequest(BaseModel):
    transaction_id: str
    commitment_id: str
    merchant_id: str
    product_id: str
    product_name: str
    category: str
    quantity: int
    price_minor: int
    currency: str = "INR"
    requested_at: datetime = Field(default_factory=utcnow)
    client_request_id: str = Field(
        description="Caller-supplied idempotency token for this specific attempt "
        "(distinct from commitment_id — lets us tell 'legitimate retry of the same "
        "commitment' apart from 'reuse of an already-consumed commitment')."
    )


# ---------------------------------------------------------------------------
# Integrity checks + decision
# ---------------------------------------------------------------------------

class IntegrityCheck(BaseModel):
    name: str
    status: CheckStatus
    expected: Optional[str] = None
    observed: Optional[str] = None
    detail: str
    confidence: Optional[float] = Field(
        default=None, description="Set only for AI-assisted checks; None for deterministic checks."
    )
    threat_ref: Optional[str] = Field(
        default=None, description="AP2 threat this check closes, e.g. 'T-31', 'T-32', 'T-33'."
    )


class Decision(BaseModel):
    transaction_id: str
    outcome: DecisionOutcome
    reason: str
    checks: list[IntegrityCheck]
    decided_at: datetime = Field(default_factory=utcnow)


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

class AuditEvent(BaseModel):
    seq: int
    timestamp: datetime
    transaction_id: str
    event_type: str
    prior_state: Optional[str]
    new_state: Optional[str]
    evidence_ref: Optional[str]
    payload: dict
    prev_hash: str
    hash: str
