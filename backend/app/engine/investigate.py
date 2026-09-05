"""
Why the books don't close, for one record.

Two things live here, and the separation between them is the whole point:

1. **Breakpoint analysis** — a deterministic money-flow trace over the
   five hops a payment makes (ORDER -> PAYMENT -> SETTLEMENT -> BANK ->
   BOOKS), saying for each hop whether it was found, is missing, is not
   due yet, is ambiguous, contradicts itself, or was never evaluated at
   all. Every status is derived from what the engine already recorded for
   that record — its classification, its exception type, the candidates it
   considered and the signals for and against each — plus which kinds of
   file the run actually contained. Nothing here calls a model.

   The distinctions this module refuses to blur, because the product's
   credibility is exactly these three:

     - A stage with no source file behind it is NOT_EVALUATED, never
       MISSING. Reporting "no bank credit found" for a run that never
       received a bank statement is a fabricated finding.
     - A settlement that is not due yet is PENDING, never MISSING. "Wait"
       and "chase the provider" are different instructions.
     - Two identifiers that name different transactions are
       CONTRADICTORY, and both identifiers are named.

2. **The exception investigator** — bounded evidence gathering that
   produces three sections which must never blur into each other:

     confirmed_evidence  deterministic facts, computed here, in code.
     hypotheses          ranked possible explanations, each labelled.
     unresolved          what the available data genuinely cannot settle.

   Deterministic hypotheses (arithmetic that actually adds up, an
   aggregation, a pending window) are produced with no model call at all.
   The model is asked only about the semantic/linguistic residue — is
   "ACME RTL" the same merchant as "Acme Retail Pvt Ltd", is this
   narration truncated — and for a short explanation grounded in the
   evidence it was handed. Anything it returns that asserts a fact not
   present in that evidence is dropped before it reaches the caller.

   `recommended_action` is decided here, from the hypotheses and the same
   policy the engine uses. The model may suggest; policy decides. An
   amount or currency mismatch can never yield RECONCILE.

Investigation is read-only. It never mutates a record's outcome. It does
append one audit event (`AI_INVESTIGATION`) carrying the breakpoint, the
hypothesis labels, and whether a model was used and which provider — and
never the prompt, and never raw model reasoning.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from itertools import combinations
from typing import Any, Callable, Optional, Sequence

from app.domain.models import (
    ExceptionType,
    MatchClassification,
    PolicyConfig,
    RazorpaySettlementRecord,
    ReconciliationOutcome,
)
from app.engine import normalize

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

STAGES: tuple[str, ...] = ("ORDER", "PAYMENT", "SETTLEMENT", "BANK", "BOOKS")

FOUND = "FOUND"
MISSING = "MISSING"
PENDING = "PENDING"
AMBIGUOUS = "AMBIGUOUS"
CONTRADICTORY = "CONTRADICTORY"
NOT_EVALUATED = "NOT_EVALUATED"

#: A stage that is not FOUND and not NOT_EVALUATED is where the trace broke.
_BREAKING_STATUSES = (MISSING, PENDING, AMBIGUOUS, CONTRADICTORY)

KIND_NONE = "NONE"
KIND_DATA_UNAVAILABLE = "DATA_UNAVAILABLE"

# Every hypothesis label the system can emit. A label outside this set is
# a bug (or a model hallucinating a category) and is dropped.
HYPOTHESIS_LABELS = frozenset({
    "PENDING_SETTLEMENT",
    "MISSING_SETTLEMENT",
    "AGGREGATED_SETTLEMENT",
    "SPLIT_SETTLEMENT",
    "GATEWAY_FEE_DEDUCTION",
    "TAX_DEDUCTION",
    "REFUND_OFFSET",
    "DUPLICATE_TRANSACTION",
    "MERCHANT_ALIAS",
    "TRUNCATED_NARRATION",
    "REFERENCE_MISMATCH",
    "GENUINELY_MISSING",
    "CONFLICTING_IDENTITY",
})

# The only labels a model is allowed to contribute. Everything else is a
# question about arithmetic, timing or identity that this module answers
# deterministically — a model "agreeing" with an aggregation it cannot
# verify adds no information and would launder a guess into evidence.
AI_ALLOWED_LABELS = frozenset({
    "MERCHANT_ALIAS",
    "TRUNCATED_NARRATION",
    "REFERENCE_MISMATCH",
    "CONFLICTING_IDENTITY",
})

RECONCILE = "RECONCILE"
EXCEPTION = "EXCEPTION"
HUMAN_REVIEW = "HUMAN_REVIEW"
INVESTIGATE = "INVESTIGATE"

AI_AVAILABLE = "AI_AVAILABLE"
AI_FALLBACK_ACTIVE = "AI_FALLBACK_ACTIVE"
AI_UNAVAILABLE = "AI_UNAVAILABLE"
# Distinct from AI_UNAVAILABLE on purpose. "We did not need to ask" and
# "we asked and could not reach anyone" are different facts, and showing
# an operator "AI unavailable" on a record the arithmetic already settled
# would be a false statement about the system's health.
AI_NOT_CONSULTED = "AI_NOT_CONSULTED"

#: A deterministic hypothesis at or above this confidence settles the
#: record on its own, so no model call is spent on it.
AI_SKIP_CONFIDENCE = 0.85

#: Hard wall-clock ceiling for the whole AI step, enforced with the same
#: worker-thread pattern the matching engine uses for its one external
#: call, so a hung provider cannot stall an investigation.
AI_TIMEOUT_SECONDS = 12.0
AI_PROVIDER_TIMEOUT_SECONDS = 10.0

#: At most this many model calls per record. Investigation is an
#: interactive action; it has a latency budget, not a research budget.
MAX_AI_CALLS = 2

# Bounds for the arithmetic search. Subset-sum is exponential and an
# investigation that hangs is worse than one that misses a grouping.
MAX_AGGREGATION_POOL = 24
MAX_AGGREGATION_GROUP = 3
MAX_UNCLAIMED_SETTLEMENTS = 60


# ---------------------------------------------------------------------------
# Output types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TraceStage:
    """One hop of the money-flow trace for a record."""

    stage: str
    status: str
    detail: str
    amount_minor: Optional[int]
    evidence: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Hypothesis:
    """One ranked possible explanation.

    `source` is not decoration: it is how a reader tells a fact that was
    computed from the data apart from a reading the model proposed.
    """

    label: str
    confidence: float
    rationale: str
    evidence_keys: list[str] = field(default_factory=list)
    source: str = "DETERMINISTIC"  # "DETERMINISTIC" | "AI"


@dataclass
class Investigation:
    record_id: str
    breakpoint_stage: Optional[str]
    breakpoint_kind: str
    trace: list[TraceStage]
    confirmed_evidence: list[str]
    hypotheses: list[Hypothesis]
    unresolved: list[str]
    recommended_action: str
    ai_used: bool
    ai_provider: Optional[str]
    ai_status: str
    # Additive beyond the shared contract, and deliberately narrow:
    # `explanation` is one grounded sentence (deterministic unless the
    # model produced one that survived the grounding filter),
    # `evidence_index` lets a caller resolve a hypothesis's evidence_keys,
    # and `ai_claims_dropped` counts what the filter removed — the removed
    # text itself is never carried out of this module.
    explanation: str = ""
    evidence_index: dict[str, str] = field(default_factory=dict)
    ai_claims_dropped: int = 0
    batch_id: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------

@dataclass
class SourceRoles:
    """Which kinds of file the run actually contained.

    This is the difference between "we looked at the bank and found
    nothing" and "nobody gave us a bank statement", and it is the single
    most important input to the trace.
    """

    has_gateway: bool = False
    has_bank: bool = False
    has_books: bool = False
    has_orders: bool = False
    declared: bool = False  # were there any uploaded sources at all?

    @classmethod
    def from_sources(cls, sources: Sequence[dict] | None) -> "SourceRoles":
        if not sources:
            # A dataset-driven batch has no uploaded sources. Its
            # settlement-side population is gateway payment/settlement
            # data, and there is no bank statement and no accounting
            # export anywhere in it.
            return cls(has_gateway=True, declared=False)
        kinds = {str(s.get("source_type") or "").upper() for s in sources}
        return cls(
            has_gateway="PAYMENT_GATEWAY" in kinds,
            has_bank="BANK_STATEMENT" in kinds,
            has_books="ACCOUNTING" in kinds,
            has_orders="ORDERS" in kinds,
            declared=True,
        )

    @property
    def settlement_side_stage(self) -> Optional[str]:
        """Which stage the settlement-side match is attributed to.

        Run-level fallback only. The engine has one settlement-side
        population, so with both a bank statement and a gateway payout in
        the same run this cannot tell them apart on its own.

        When the matched row carries provenance, `build_trace` overrides
        this with the file the match actually came from — which is the
        honest answer and the one an operator can open. This stays as the
        answer for dataset-driven runs and for runs recorded before
        provenance was captured.
        """
        if self.has_gateway:
            return "PAYMENT"
        if self.has_bank:
            return "BANK"
        return None


@dataclass
class RecordView:
    """The stored decision, parsed once.

    Reads a row exactly as `store.get_record` returns it, and also the
    hydrated shape the API hands around, so callers do not have to
    normalise before asking a question about a record.
    """

    record_id: str
    batch_id: Optional[str]
    merchant: dict
    checks: list[dict]
    considered: list[dict]
    candidates: list[dict]
    classification: Optional[str]
    exception_type: Optional[str]
    severity: Optional[str]
    outcome: str
    reason: str
    matched_payment_id: Optional[str]
    engine_explanation: str
    engine_action: str
    review_state: str
    provenance: dict = field(default_factory=dict)

    @classmethod
    def from_row(cls, row: dict) -> "RecordView":
        def _blob(hydrated_key: str, raw_key: str) -> Any:
            if hydrated_key in row and row[hydrated_key] is not None:
                return row[hydrated_key]
            raw = row.get(raw_key)
            if not raw:
                return None
            if isinstance(raw, (dict, list)):
                return raw
            try:
                return json.loads(raw)
            except (TypeError, ValueError):
                return None

        return cls(
            record_id=str(row.get("record_id") or ""),
            batch_id=row.get("batch_id"),
            merchant=_blob("merchant", "merchant_json") or {},
            checks=_blob("checks", "checks_json") or [],
            considered=_blob("considered_candidates", "considered_json") or [],
            candidates=_blob("candidates", "candidates_json") or [],
            classification=row.get("classification"),
            exception_type=row.get("exception_type"),
            severity=row.get("severity"),
            outcome=str(row.get("outcome") or ""),
            reason=str(row.get("reason") or ""),
            matched_payment_id=row.get("matched_payment_id"),
            engine_explanation=str(row.get("explanation") or ""),
            engine_action=str(row.get("recommended_action") or ""),
            review_state=str(row.get("review_state") or "OPEN"),
            provenance=_blob("provenance", "provenance_json") or {},
        )

    # -- provenance ------------------------------------------------------
    @property
    def matched_source(self) -> dict:
        """Which uploaded file the winning settlement row came from.

        Empty when the run was dataset-driven, when nothing matched, or
        when the run predates provenance capture — all three mean "we
        cannot say", never "it came from nowhere".
        """
        side = self.provenance.get("settlement") if isinstance(self.provenance, dict) else None
        return side if isinstance(side, dict) else {}

    @property
    def ledger_source_filename(self) -> Optional[str]:
        """Which uploaded file this ledger row came from, when known."""
        side = self.provenance.get("ledger") if isinstance(self.provenance, dict) else None
        name = side.get("filename") if isinstance(side, dict) else None
        return str(name) if name else None

    @property
    def matched_source_type(self) -> Optional[str]:
        kind = self.matched_source.get("source_type")
        return str(kind).upper() if kind else None

    @property
    def source_citation(self) -> Optional[str]:
        """`ICICI_January.csv row 4182` — the line an operator can open."""
        origin = self.matched_source
        name = origin.get("filename")
        if not name:
            return None
        row = origin.get("file_row", origin.get("row"))
        return f"{name} row {row}" if row is not None else str(name)

    # -- convenience -----------------------------------------------------
    @property
    def amount_minor(self) -> int:
        try:
            return int(self.merchant.get("amount_minor") or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def currency(self) -> str:
        return str(self.merchant.get("currency") or "INR")

    @property
    def reference_id(self) -> Optional[str]:
        ref = self.merchant.get("reference_id")
        return str(ref) if ref else None

    @property
    def description(self) -> str:
        return str(self.merchant.get("description") or "")

    @property
    def order_date(self) -> Optional[datetime]:
        return _parse_dt(self.merchant.get("order_date"))

    @property
    def refund_minor(self) -> int:
        try:
            return int(self.merchant.get("refund_amount_minor") or 0)
        except (TypeError, ValueError):
            return 0

    def failing_checks(self) -> list[dict]:
        return [c for c in self.checks if str(c.get("status")) == "FAIL"]

    def warning_checks(self) -> list[dict]:
        return [c for c in self.checks if str(c.get("status")) == "WARN"]

    def check(self, name: str) -> Optional[dict]:
        return next((c for c in self.checks if c.get("name") == name), None)

    def matched_candidate(self) -> Optional[dict]:
        """The settlement row or assessment behind the match, whichever
        the store kept. `candidates_json` holds full settlement rows (with
        fee and tax) for exact-reference matches; `considered_json` holds
        assessments, which carry the gross amount but no breakdown."""
        if not self.matched_payment_id:
            return None
        for row in self.candidates:
            if row.get("payment_id") == self.matched_payment_id:
                return row
        for row in self.considered:
            if row.get("payment_id") == self.matched_payment_id:
                return row
        return None


@dataclass
class InvestigationContext:
    """Everything an investigation is allowed to look at.

    Assembled by the caller so the engine has no hidden reach into the
    database mid-analysis, and so a test can hand it an exact world.
    """

    record: dict
    batch: Optional[dict] = None
    sources: list[dict] = field(default_factory=list)
    siblings: list[dict] = field(default_factory=list)
    settlements: list[RazorpaySettlementRecord] = field(default_factory=list)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    settlement_population_complete: bool = True
    _settlement_index: Optional[dict[str, RazorpaySettlementRecord]] = field(default=None, repr=False)

    def settlement(self, payment_id: Optional[str]) -> Optional[RazorpaySettlementRecord]:
        """The full settlement row behind a candidate, when the run's
        population is available.

        A stored `CandidateAssessment` keeps the signals but not the
        counterparty's wording, and wording is exactly what the semantic
        tier is asked about — so it is resolved here rather than guessed
        at or omitted.
        """
        if not payment_id or not self.settlements:
            return None
        if self._settlement_index is None:
            self._settlement_index = {s.payment_id: s for s in self.settlements}
        return self._settlement_index.get(payment_id)


def load_context(
    record_id: str,
    batch_id: Optional[str] = None,
    *,
    settlements: Optional[Sequence[RazorpaySettlementRecord]] = None,
    policy: Optional[PolicyConfig] = None,
    sibling_limit: int = 400,
) -> Optional[InvestigationContext]:
    """Assemble a context from the ledger. Returns None if unknown."""
    from app.ledger import store  # local import: keeps the engine importable without a DB

    row = store.get_record(record_id, batch_id)
    if not row:
        return None
    resolved_batch = batch_id or row.get("batch_id")
    siblings = store.list_records(resolved_batch, limit=sibling_limit) if resolved_batch else []
    sources = store.list_sources(resolved_batch) if resolved_batch else []
    batch = store.get_batch(resolved_batch) if resolved_batch else None
    return InvestigationContext(
        record=row,
        batch=batch,
        sources=sources,
        siblings=[s for s in siblings if s.get("record_id") != row.get("record_id")],
        settlements=list(settlements or []),
        policy=policy or PolicyConfig(),
        settlement_population_complete=settlements is not None,
    )


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _parse_dt(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _money(minor: Optional[int], currency: str = "INR") -> str:
    if minor is None:
        return "an unknown amount"
    sign = "-" if minor < 0 else ""
    whole, frac = divmod(abs(int(minor)), 100)
    body = f"{whole:,}.{frac:02d}"
    return f"{sign}₹{body}" if currency.upper() == "INR" else f"{sign}{currency.upper()} {body}"


def _day(value: Any) -> str:
    dt = _parse_dt(value)
    return dt.date().isoformat() if dt else "an unknown date"


def _join(items: Sequence[str]) -> str:
    items = [i for i in items if i]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + f" and {items[-1]}"


def _contradicts_identity(candidate: dict) -> bool:
    return any(
        "identify different transactions" in str(s)
        for s in (candidate.get("contradicting_signals") or [])
    )


# ---------------------------------------------------------------------------
# (1) Breakpoint analysis — deterministic, no model
# ---------------------------------------------------------------------------

_PENDING_CLASSIFICATIONS = {MatchClassification.PENDING_SETTLEMENT_WINDOW.value}
_AMBIGUOUS_CLASSIFICATIONS = {
    MatchClassification.AMBIGUOUS_MULTIPLE.value,
    MatchClassification.SEMANTIC_UNRESOLVED.value,
}
_SETTLEMENT_CHECKS = (
    "currency_match",
    "gross_amount_match",
    "fee_tax_arithmetic",
    "refund_consistency",
    "settlement_timing",
)


def build_trace(view: RecordView, roles: SourceRoles, policy: PolicyConfig) -> list[TraceStage]:
    """The five-hop money-flow trace, from the engine's own recorded output."""
    stages: dict[str, TraceStage] = {}

    stages["ORDER"] = _order_stage(view)

    # Per-record attribution beats the run-level guess. If the winning
    # settlement row carries provenance, we know which file it came from,
    # so a bank credit is reported as a bank credit — with the line an
    # operator can open — instead of being folded into PAYMENT.
    attributed = {"BANK_STATEMENT": "BANK", "PAYMENT_GATEWAY": "PAYMENT"}.get(
        view.matched_source_type or ""
    )
    settlement_stage_name = attributed or roles.settlement_side_stage
    match_stage = _match_stage(view, settlement_stage_name or "PAYMENT")
    citation = view.source_citation
    if citation and match_stage.status == FOUND:
        match_stage.evidence.append(f"matched {citation}")

    if settlement_stage_name is None:
        # No settlement-side file at all: there is nothing to compare the
        # order against, and saying "missing" would blame the data for the
        # upload's shape.
        stages["PAYMENT"] = _not_evaluated(
            "PAYMENT", "No payment-gateway file was included in this run, so the gateway side was not checked."
        )
        stages["SETTLEMENT"] = _not_evaluated(
            "SETTLEMENT", "No settlement-side file was included in this run."
        )
        stages["BANK"] = _not_evaluated(
            "BANK", "No bank statement was included in this run, so bank credit was not checked."
        )
    elif settlement_stage_name == "PAYMENT":
        stages["PAYMENT"] = match_stage
        stages["SETTLEMENT"] = _settlement_stage(view, match_stage)
        stages["BANK"] = _bank_stage(view, roles)
    else:  # this record was matched against a bank statement row
        # Two different reasons the gateway hops are unevaluated, and they
        # are not interchangeable: either no gateway file exists at all, or
        # one exists and this particular record was traced straight to the
        # bank line instead. Reporting the first when the second is true
        # would misdescribe the run the operator is looking at.
        if roles.has_gateway:
            stages["PAYMENT"] = _not_evaluated(
                "PAYMENT",
                "This record was matched directly against the bank statement. A gateway payout "
                "file is present in this run but was not the source of this match.",
            )
            stages["SETTLEMENT"] = _not_evaluated(
                "SETTLEMENT",
                "No gateway payout row was matched to this record, so there is no fee, tax or "
                "payout breakdown to check against it.",
            )
        else:
            stages["PAYMENT"] = _not_evaluated(
                "PAYMENT",
                "No payment-gateway file was included in this run; the bank statement is the only "
                "settlement-side evidence.",
            )
            stages["SETTLEMENT"] = _not_evaluated(
                "SETTLEMENT",
                "No gateway payout file was included, so there is no fee, tax or payout breakdown to check.",
            )
        stages["BANK"] = match_stage

    stages["BOOKS"] = _books_stage(view, roles)

    ordered = [stages[name] for name in STAGES]
    return _mask_downstream(ordered)


def _order_stage(view: RecordView) -> TraceStage:
    evidence = [f"ledger row {view.record_id}"]
    if view.reference_id:
        evidence.append(f"reference {view.reference_id}")
    if view.description:
        evidence.append(f'description "{view.description}"')
    status = str(view.merchant.get("status") or "captured")
    if status != "captured":
        evidence.append(f"merchant status {status}")
    if view.refund_minor:
        evidence.append(f"merchant records a refund of {_money(view.refund_minor, view.currency)}")
    return TraceStage(
        stage="ORDER",
        status=FOUND,
        detail=(
            f"The ledger records order {view.merchant.get('order_id') or view.record_id} for "
            f"{_money(view.amount_minor, view.currency)} on {_day(view.merchant.get('order_date'))}."
        ),
        amount_minor=view.amount_minor,
        evidence=evidence,
    )


def _match_stage(view: RecordView, stage_name: str) -> TraceStage:
    """Whether a settlement-side record was identified for this order.

    Everything here is read off the engine's recorded decision — the
    classification it reached, the exception it typed, and the candidates
    it kept with the signals for and against each.
    """
    classification = view.classification or ""
    exception_type = view.exception_type or ""
    considered = view.considered

    # Matched, but the batch pass found another order claiming the same
    # payment. The match exists and is disputed — that is ambiguity, not
    # absence.
    if exception_type == ExceptionType.DUPLICATE_CLAIM.value:
        check = view.check("claim_uniqueness") or {}
        competing = str(check.get("detail") or view.reason)
        return TraceStage(
            stage=stage_name,
            status=AMBIGUOUS,
            detail=(
                f"Settlement {view.matched_payment_id} was matched to this order and to at least one "
                "other order in the same run."
            ),
            amount_minor=_candidate_amount(view),
            evidence=[competing] if competing else [],
        )

    if exception_type == ExceptionType.AGGREGATED_SETTLEMENT.value:
        check = view.check("aggregated_settlement") or {}
        return TraceStage(
            stage=stage_name,
            status=AMBIGUOUS,
            detail=view.reason or "One settlement appears to cover this record together with others.",
            amount_minor=None,
            evidence=[str(check.get("observed"))] if check.get("observed") else [],
        )

    if view.matched_payment_id:
        candidate = view.matched_candidate() or {}
        evidence = [f"settlement {view.matched_payment_id}"]
        if candidate.get("order_reference"):
            evidence.append(f"settlement reference {candidate['order_reference']}")
        evidence.extend(str(s) for s in (candidate.get("supporting_signals") or []))
        if not candidate.get("supporting_signals"):
            evidence.append(_classification_phrase(classification))
        return TraceStage(
            stage=stage_name,
            status=FOUND,
            detail=(
                f"Settlement {view.matched_payment_id} was identified for this order "
                f"({_classification_phrase(classification)})."
            ),
            amount_minor=_candidate_amount(view),
            evidence=evidence,
        )

    if classification in _PENDING_CLASSIFICATIONS:
        return TraceStage(
            stage=stage_name,
            status=PENDING,
            detail=view.reason or "No settlement has been published for this order yet, and none is due yet.",
            amount_minor=None,
            evidence=[c.get("detail", "") for c in view.checks if c.get("name") == "settlement_presence"],
        )

    if classification in _AMBIGUOUS_CLASSIFICATIONS:
        competing = [
            f"{c.get('payment_id')} ({_money(c.get('gross_amount_minor'), view.currency)}"
            f", reference {c.get('order_reference') or 'none'})"
            for c in considered[:4]
        ]
        if competing:
            detail = (
                f"{len(competing)} settlements remain plausible for this order and none dominates enough "
                "to decide automatically."
            )
        else:
            duplicate = view.check("duplicate_reference") or {}
            detail = str(
                duplicate.get("detail")
                or "More than one settlement remains plausible and none dominates enough to decide automatically."
            )
        return TraceStage(
            stage=stage_name,
            status=AMBIGUOUS,
            detail=detail,
            amount_minor=None,
            evidence=competing,
        )

    contradicting = [c for c in considered if _contradicts_identity(c)]
    if contradicting:
        top = contradicting[0]
        mine = view.reference_id or "no reference"
        theirs = top.get("order_reference") or "no reference"
        return TraceStage(
            stage=stage_name,
            status=CONTRADICTORY,
            detail=(
                f"The closest settlement-side record ({top.get('payment_id')}) names a different "
                f"transaction: this order carries reference {mine}, that record carries {theirs}."
            ),
            amount_minor=top.get("gross_amount_minor"),
            evidence=[
                f"order reference {mine}",
                f"settlement reference {theirs}",
                *[str(s) for s in (top.get("contradicting_signals") or [])],
            ],
        )

    if classification == MatchClassification.PROVIDER_ERROR.value:
        return TraceStage(
            stage=stage_name,
            status=AMBIGUOUS,
            detail="The semantic classifier was unavailable, so the best candidate could not be ruled in or out.",
            amount_minor=None,
            evidence=[view.reason] if view.reason else [],
        )

    # Genuinely nothing credible. Whether records were retrieved and
    # refused, or nothing was retrieved at all, is a real distinction and
    # is carried in the detail.
    if considered:
        top = considered[0]
        detail = (
            f"No settlement-side record could be matched to this order. {len(considered)} were retrieved "
            f"and refused; the closest ({top.get('payment_id')}) was rejected because "
            f"{top.get('admissibility_reason') or 'it carried no identity evidence'}."
        )
        evidence = [
            f"{c.get('payment_id')} rejected: {c.get('admissibility_reason') or 'no identity evidence'}"
            for c in considered[:4]
        ]
    else:
        detail = "No settlement-side record was retrieved for this order at all."
        evidence = []
    return TraceStage(stage=stage_name, status=MISSING, detail=detail, amount_minor=None, evidence=evidence)


def _settlement_stage(view: RecordView, match_stage: TraceStage) -> TraceStage:
    """Funds actually settling, given that a gateway record was identified."""
    if match_stage.status != FOUND:
        return _not_evaluated(
            "SETTLEMENT",
            f"Not evaluated — the trace stops at {match_stage.stage}.",
        )

    candidate = view.matched_candidate() or {}
    failing = [c for c in view.failing_checks() if c.get("name") in _SETTLEMENT_CHECKS]
    if failing:
        first = failing[0]
        return TraceStage(
            stage="SETTLEMENT",
            status=CONTRADICTORY,
            detail=str(first.get("detail") or f"{first.get('name')} failed."),
            amount_minor=candidate.get("net_amount_minor") or candidate.get("gross_amount_minor"),
            evidence=[
                f"{c.get('name')}: expected {c.get('expected')}, observed {c.get('observed')}"
                for c in failing
            ],
        )

    settled_on = candidate.get("settlement_date")
    gross = candidate.get("gross_amount_minor")
    fee = candidate.get("fee_minor")
    tax = candidate.get("tax_minor")
    net = candidate.get("net_amount_minor")
    evidence = []
    if gross is not None and fee is not None and tax is not None and net is not None:
        evidence.append(
            f"gross {_money(gross, view.currency)} - fee {_money(fee, view.currency)} - "
            f"tax {_money(tax, view.currency)} = net {_money(net, view.currency)}"
        )
    for name in ("fee_tax_arithmetic", "settlement_timing"):
        check = view.check(name)
        if check:
            evidence.append(f"{name}: {check.get('detail')}")

    detail = (
        f"Settlement {view.matched_payment_id} settled on {_day(settled_on)}."
        if settled_on else
        f"Settlement {view.matched_payment_id} was identified and its arithmetic checks passed."
    )
    return TraceStage(
        stage="SETTLEMENT",
        status=FOUND,
        detail=detail,
        amount_minor=net if net is not None else gross,
        evidence=evidence,
    )


def _bank_stage(view: RecordView, roles: SourceRoles) -> TraceStage:
    """Bank credit, for a record whose match came from a gateway file.

    Three distinct answers, and the distinction is the whole point:

    - No bank statement was uploaded. Nothing was checked, and asserting
      a missing credit would invent a finding out of the upload's shape.
    - A bank statement was uploaded, and this record matched a *gateway*
      row. The engine reconciles one pooled settlement side; it does not
      then chain gateway -> bank as a second hop. So the credit was not
      checked for this record, and saying so is the honest answer.
    - This record matched a bank row directly. That case never reaches
      here — `build_trace` attributes it to BANK from the row's own
      provenance, and it is reported as FOUND with the file and line.
    """
    if not roles.has_bank:
        return _not_evaluated(
            "BANK",
            "No bank statement was included in this run, so bank credit was not checked.",
        )
    return _not_evaluated(
        "BANK",
        "This record was matched against the gateway payout file. Accord reconciles the "
        "settlement side as one population rather than tracing gateway to bank as a further "
        "hop, so no bank credit was checked for this record.",
    )


def _books_stage(view: RecordView, roles: SourceRoles) -> TraceStage:
    """Was this order booked in the accounting ledger?

    Accord cannot answer that, and the reason is architectural rather than
    a gap in the data. Orders and accounting exports are both *ledger*
    sources: they pool into one side and are reconciled against the
    settlement side, not against each other. Answering "is this order in
    the books" would require matching a ledger row to another ledger row,
    which is a different pass than the one this engine runs.

    An earlier version blamed missing per-file provenance. That reason was
    conservative but wrong — provenance exists now, and the stage still
    cannot be evaluated. Naming the wrong obstacle invites someone to
    "fix" it and find the stage no better off.
    """
    if not roles.has_books:
        return _not_evaluated(
            "BOOKS",
            "No accounting export was included in this run, so the books side was not checked.",
        )
    if roles.has_orders:
        origin = view.ledger_source_filename
        came_from = f" This row came from {origin}." if origin else ""
        return _not_evaluated(
            "BOOKS",
            "Orders and accounting exports are both ledger-side sources, so Accord reconciles them "
            "against settlements rather than against each other. Whether this order also appears as an "
            f"accounting entry was not checked.{came_from}",
        )
    return TraceStage(
        stage="BOOKS",
        status=FOUND,
        detail="The ledger side of this run is the accounting export, so this row is the books entry.",
        amount_minor=view.amount_minor,
        evidence=[f"ledger row {view.record_id}"],
    )


def _not_evaluated(stage: str, detail: str) -> TraceStage:
    return TraceStage(stage=stage, status=NOT_EVALUATED, detail=detail, amount_minor=None, evidence=[])


def _mask_downstream(stages: list[TraceStage]) -> list[TraceStage]:
    """Everything after the first break is NOT_EVALUATED, not MISSING.

    Once the trace stops there is nothing to say about the hops beyond it,
    and MISSING there would be an assertion the run never tested.
    """
    out: list[TraceStage] = []
    broken_at: Optional[str] = None
    for stage in stages:
        if broken_at is not None and stage.status != NOT_EVALUATED:
            out.append(_not_evaluated(stage.stage, f"Not evaluated — the trace stops at {broken_at}."))
            continue
        out.append(stage)
        if broken_at is None and stage.status in _BREAKING_STATUSES:
            broken_at = stage.stage
    return out


def breakpoint_of(trace: Sequence[TraceStage]) -> tuple[Optional[str], str]:
    """First non-FOUND stage that was actually evaluated, and its kind."""
    for stage in trace:
        if stage.status == NOT_EVALUATED:
            continue
        if stage.status != FOUND:
            return stage.stage, stage.status
    evaluated = [s for s in trace if s.status != NOT_EVALUATED]
    if len(evaluated) <= 1:
        # Only the order itself could be checked — nothing was reconciled
        # against anything, and that is a data problem, not a finding.
        return None, KIND_DATA_UNAVAILABLE
    return None, KIND_NONE


def _classification_phrase(classification: Optional[str]) -> str:
    return {
        MatchClassification.EXACT_REFERENCE.value: "exact reference match",
        MatchClassification.DISAMBIGUATED_BY_AMOUNT.value: "disambiguated by amount",
        MatchClassification.CORROBORATED.value: "corroborated by independent signals",
        MatchClassification.SEMANTIC_CONFIRMED.value: "confirmed by the semantic classifier",
    }.get(classification or "", (classification or "unclassified").replace("_", " ").lower())


def _candidate_amount(view: RecordView) -> Optional[int]:
    candidate = view.matched_candidate() or {}
    return candidate.get("gross_amount_minor")


# ---------------------------------------------------------------------------
# (2) Evidence — deterministic facts, keyed so a hypothesis can cite them
# ---------------------------------------------------------------------------

@dataclass
class EvidenceLedger:
    """Keyed facts. Every string here is computed from stored data; none
    of it is written by a model, and a hypothesis that cites nothing here
    does not survive."""

    items: list[tuple[str, str]] = field(default_factory=list)

    def add(self, key: str, text: str) -> str:
        if any(k == key for k, _ in self.items):
            suffix = sum(1 for k, _ in self.items if k == key or k.startswith(key + "_"))
            key = f"{key}_{suffix + 1}"
        self.items.append((key, text))
        return key

    @property
    def keys(self) -> list[str]:
        return [k for k, _ in self.items]

    @property
    def texts(self) -> list[str]:
        return [t for _, t in self.items]

    def as_index(self) -> dict[str, str]:
        return {k: t for k, t in self.items}


def build_evidence(
    view: RecordView,
    context: InvestigationContext,
    trace: Sequence[TraceStage],
) -> EvidenceLedger:
    ledger = EvidenceLedger()
    currency = view.currency

    ledger.add(
        "ORDER_AMOUNT",
        f"Order {view.merchant.get('order_id') or view.record_id} is recorded at "
        f"{_money(view.amount_minor, currency)} on {_day(view.merchant.get('order_date'))}.",
    )
    if view.reference_id:
        ledger.add("ORDER_REFERENCE", f"The order carries reference {view.reference_id}.")
    if view.description:
        ledger.add("ORDER_DESCRIPTION", f'The order description reads "{view.description}".')
    if view.refund_minor:
        ledger.add(
            "ORDER_REFUND",
            f"The merchant records a refund of {_money(view.refund_minor, currency)} against this order.",
        )

    ledger.add(
        "ENGINE_DECISION",
        f"The engine decided {view.outcome}"
        + (f" ({view.exception_type})" if view.exception_type else "")
        + f", classified as {view.classification or 'unclassified'}.",
    )
    if view.reason:
        ledger.add("ENGINE_REASON", f"The engine recorded: {view.reason}")

    for check in view.failing_checks():
        ledger.add(
            f"CHECK_{str(check.get('name') or 'unknown').upper()}",
            f"Check {check.get('name')} failed: expected {check.get('expected')}, "
            f"observed {check.get('observed')}.",
        )

    for i, candidate in enumerate(view.considered[:4], start=1):
        supporting = _join([str(s) for s in (candidate.get("supporting_signals") or [])])
        contradicting = _join([str(s) for s in (candidate.get("contradicting_signals") or [])])
        parts = [
            f"Candidate {candidate.get('payment_id')} (reference "
            f"{candidate.get('order_reference') or 'none'}, "
            f"{_money(candidate.get('gross_amount_minor'), currency)})"
        ]
        if supporting:
            parts.append(f"supports: {supporting}")
        if contradicting:
            parts.append(f"contradicts: {contradicting}")
        wording = candidate_wording(view, context, candidate.get("payment_id"))
        if wording:
            parts.append(f'its text reads "{wording}"')
        if not candidate.get("admissible"):
            parts.append(f"refused because {candidate.get('admissibility_reason')}")
        ledger.add(f"CANDIDATE_{i}", "; ".join(parts) + ".")

    matched = view.matched_candidate()
    if matched and matched.get("fee_minor") is not None:
        ledger.add(
            "SETTLEMENT_BREAKDOWN",
            f"Settlement {matched.get('payment_id')}: gross "
            f"{_money(matched.get('gross_amount_minor'), currency)}, fee "
            f"{_money(matched.get('fee_minor'), currency)}, tax {_money(matched.get('tax_minor'), currency)}, "
            f"net {_money(matched.get('net_amount_minor'), currency)}.",
        )

    # The identical-amount trap, stated as a fact rather than left implicit:
    # in a population of thousands an exact amount collision is ordinary.
    twins = [
        s for s in context.siblings
        if _sibling_amount(s) == view.amount_minor and s.get("record_id") != view.record_id
    ]
    if twins:
        ledger.add(
            "AMOUNT_COLLISION",
            f"{len(twins)} other record(s) in this run carry the identical amount "
            f"{_money(view.amount_minor, currency)}, so amount agreement alone does not identify a counterpart.",
        )

    for stage in trace:
        if stage.status == NOT_EVALUATED and "was not checked" in stage.detail:
            ledger.add(f"STAGE_{stage.stage}", stage.detail)

    return ledger


def candidate_wording(view: RecordView, context: InvestigationContext, payment_id: Optional[str]) -> str:
    """The counterparty's free text, from wherever the run kept it."""
    if not payment_id:
        return ""
    for row in view.candidates:
        if row.get("payment_id") == payment_id and row.get("description"):
            return str(row["description"])
    settlement = context.settlement(payment_id)
    return settlement.description if settlement else ""


def _sibling_amount(row: dict) -> int:
    try:
        merchant = row.get("merchant")
        if merchant is None and row.get("merchant_json"):
            merchant = json.loads(row["merchant_json"])
        return int((merchant or {}).get("amount_minor") or 0)
    except (TypeError, ValueError):
        return 0


def _sibling_date(row: dict) -> Optional[datetime]:
    merchant = row.get("merchant")
    if merchant is None and row.get("merchant_json"):
        try:
            merchant = json.loads(row["merchant_json"])
        except (TypeError, ValueError):
            merchant = {}
    return _parse_dt((merchant or {}).get("order_date"))


# ---------------------------------------------------------------------------
# Deterministic arithmetic: aggregation and split settlements
# ---------------------------------------------------------------------------

@dataclass
class AggregationFinding:
    payment_id: str
    member_record_ids: list[str]
    member_total_minor: int
    settlement_amount_minor: int
    fee_minor: int
    tax_minor: int
    unique: bool
    kind: str  # "GROSS" | "NET_OF_FEES"


def unclaimed_in_window(
    view: RecordView, context: InvestigationContext
) -> tuple[list[RazorpaySettlementRecord], bool]:
    """Settlements nobody claimed, near this record's date — and whether
    there are too many of them for a grouping search to mean anything.

    Same reasoning as `batch.detect_aggregated_settlements`' own cap:
    among thousands of unclaimed settlements, a combination that happens
    to add up is a coincidence rather than the actual grouping, so beyond
    the cap the search is abandoned rather than throttled. Reporting a
    coincidence as a finding is worse than reporting nothing.
    """
    settlements = context.settlements
    if not settlements:
        return [], False
    policy = context.policy
    my_date = view.order_date
    claimed = {s.get("matched_payment_id") for s in context.siblings if s.get("matched_payment_id")}
    claimed.discard(None)
    if view.matched_payment_id:
        claimed.add(view.matched_payment_id)

    out: list[RazorpaySettlementRecord] = []
    for settlement in settlements:
        if settlement.payment_id in claimed:
            continue
        if my_date is not None and normalize.days_between(my_date, settlement.order_date) > policy.candidate_search_window_days:
            continue
        out.append(settlement)
        if len(out) > MAX_UNCLAIMED_SETTLEMENTS:
            return [], True
    return out, False


def find_aggregation(
    view: RecordView,
    context: InvestigationContext,
    unclaimed: Optional[Sequence[RazorpaySettlementRecord]] = None,
) -> Optional[AggregationFinding]:
    """Does one unclaimed settlement equal this record plus a few others?

    Bounded on purpose, and by the same discipline `batch.py` uses: a lump
    sum can decompose several ways, so a decomposition is only strong
    evidence when it is the only one. Two arithmetic shapes are tested —
    the settlement equal to the gross total, and the settlement equal to
    the total net of the fee and tax the file itself records, which is
    what a bank credit for a bundled payout actually looks like.
    """
    if not context.settlements or view.matched_payment_id:
        return None

    policy = context.policy
    tolerance = policy.amount_tolerance_minor
    my_date = view.order_date

    pool: list[tuple[str, int]] = []
    for sibling in context.siblings:
        if sibling.get("matched_payment_id") or sibling.get("outcome") == ReconciliationOutcome.RECONCILED.value:
            continue
        amount = _sibling_amount(sibling)
        if amount <= 0:
            continue
        if my_date is not None:
            other_date = _sibling_date(sibling)
            if other_date is not None and normalize.days_between(my_date, other_date) > policy.candidate_search_window_days:
                continue
        pool.append((str(sibling.get("record_id")), amount))
        if len(pool) >= MAX_AGGREGATION_POOL:
            break

    if not pool:
        return None

    if unclaimed is None:
        unclaimed, _ = unclaimed_in_window(view, context)
    for settlement in unclaimed:
        targets = [
            ("GROSS", settlement.gross_amount_minor, 0, 0),
            (
                "NET_OF_FEES",
                settlement.gross_amount_minor + settlement.fee_minor + settlement.tax_minor,
                settlement.fee_minor,
                settlement.tax_minor,
            ),
        ]
        for kind, target, fee, tax in targets:
            if kind == "NET_OF_FEES" and fee == 0 and tax == 0:
                continue
            matches: list[tuple[str, ...]] = []
            for size in range(1, MAX_AGGREGATION_GROUP):
                for others in combinations(pool, size):
                    total = view.amount_minor + sum(a for _, a in others)
                    if abs(total - target) <= tolerance:
                        matches.append(tuple([view.record_id] + [r for r, _ in others]))
                        if len(matches) > 1:
                            break
                if len(matches) > 1:
                    break
            if matches:
                group = matches[0]
                member_total = view.amount_minor + sum(
                    a for r, a in pool if r in set(group[1:])
                )
                return AggregationFinding(
                    payment_id=settlement.payment_id,
                    member_record_ids=list(group),
                    member_total_minor=member_total,
                    settlement_amount_minor=settlement.gross_amount_minor,
                    fee_minor=fee,
                    tax_minor=tax,
                    unique=len(matches) == 1,
                    kind=kind,
                )
    return None


def find_split(
    view: RecordView,
    context: InvestigationContext,
    unclaimed: Optional[Sequence[RazorpaySettlementRecord]] = None,
) -> Optional[list[RazorpaySettlementRecord]]:
    """The mirror case: this one order paid out across several settlements."""
    if not context.settlements or view.matched_payment_id:
        return None
    if unclaimed is None:
        unclaimed, _ = unclaimed_in_window(view, context)
    pool = [s for s in unclaimed if s.gross_amount_minor < view.amount_minor][:MAX_AGGREGATION_POOL]

    matches: list[tuple[RazorpaySettlementRecord, ...]] = []
    for size in (2, 3):
        for group in combinations(pool, size):
            total = sum(s.gross_amount_minor for s in group)
            if abs(total - view.amount_minor) <= context.policy.amount_tolerance_minor:
                matches.append(group)
                if len(matches) > 1:
                    return None  # several decompositions: evidence of nothing
    return list(matches[0]) if len(matches) == 1 else None


# ---------------------------------------------------------------------------
# Deterministic hypotheses
# ---------------------------------------------------------------------------

def deterministic_hypotheses(
    view: RecordView,
    context: InvestigationContext,
    roles: SourceRoles,
    trace: Sequence[TraceStage],
    ledger: EvidenceLedger,
) -> tuple[list[Hypothesis], list[str]]:
    """Explanations the data itself supports, plus what it cannot settle."""
    hypotheses: list[Hypothesis] = []
    unresolved: list[str] = []
    currency = view.currency
    keys = set(ledger.keys)

    def cite(*candidates: str) -> list[str]:
        return [k for k in candidates if k in keys]

    classification = view.classification or ""
    exception_type = view.exception_type or ""

    # -- not due yet ----------------------------------------------------
    if classification in _PENDING_CLASSIFICATIONS:
        hypotheses.append(Hypothesis(
            label="PENDING_SETTLEMENT",
            confidence=0.95,
            rationale=(
                "The payment is too recent for a settlement to exist: the engine recorded that none is due "
                f"until T+{context.policy.settlement_expected_days} from the order date."
            ),
            evidence_keys=cite("ENGINE_REASON", "ORDER_AMOUNT", "ENGINE_DECISION"),
        ))
        unresolved.append(
            "Whether the settlement will actually arrive cannot be known until the settlement window "
            "has passed; re-run this record after that date."
        )

    # -- aggregation, from the engine's own batch pass -------------------
    if exception_type == ExceptionType.AGGREGATED_SETTLEMENT.value:
        check = view.check("aggregated_settlement") or {}
        hypotheses.append(Hypothesis(
            label="AGGREGATED_SETTLEMENT",
            confidence=0.90,
            rationale=(
                "The batch pass found exactly one settlement equal to the combined total of this record "
                f"and others in the same window: {check.get('observed') or view.reason}"
            ),
            evidence_keys=cite("ENGINE_REASON", "ORDER_AMOUNT", "ENGINE_DECISION"),
        ))
        unresolved.append(
            "No settlement breakdown file was supplied, so the aggregation cannot be booked "
            "automatically — a lump sum can decompose more than one way."
        )

    # -- aggregation, from arithmetic over unmatched siblings ------------
    # The settlement scan happens once and both searches share it, so an
    # investigation costs one pass over the population, not two.
    unclaimed, too_many_settlements = unclaimed_in_window(view, context)
    aggregation = find_aggregation(view, context, unclaimed=unclaimed)
    if aggregation is not None:
        others = [r for r in aggregation.member_record_ids if r != view.record_id]
        if aggregation.kind == "NET_OF_FEES":
            difference = aggregation.member_total_minor - aggregation.settlement_amount_minor
            arithmetic = (
                f"{len(aggregation.member_record_ids)} unmatched records in the window "
                f"({_join(aggregation.member_record_ids)}) total "
                f"{_money(aggregation.member_total_minor, currency)}; settlement "
                f"{aggregation.payment_id} credits {_money(aggregation.settlement_amount_minor, currency)}; "
                f"the difference of {_money(difference, currency)} equals the recorded fee "
                f"{_money(aggregation.fee_minor, currency)} plus tax {_money(aggregation.tax_minor, currency)}."
            )
        else:
            arithmetic = (
                f"{len(aggregation.member_record_ids)} unmatched records in the window "
                f"({_join(aggregation.member_record_ids)}) total "
                f"{_money(aggregation.member_total_minor, currency)}, which equals settlement "
                f"{aggregation.payment_id} at {_money(aggregation.settlement_amount_minor, currency)}."
            )
        agg_key = ledger.add("AGGREGATION_ARITHMETIC", arithmetic)
        keys.add(agg_key)
        hypotheses.append(Hypothesis(
            label="AGGREGATED_SETTLEMENT",
            confidence=0.90 if aggregation.unique else 0.45,
            rationale=(
                f"Settlement {aggregation.payment_id} is not claimed by any record, and this record plus "
                f"{_join(others) or 'no others'} add up to it exactly."
                if aggregation.unique else
                f"This record could belong to settlement {aggregation.payment_id}, but more than one "
                "combination of unmatched records adds up to it, so the grouping is not established."
            ),
            evidence_keys=[agg_key] + cite("ORDER_AMOUNT"),
        ))
        if aggregation.kind == "NET_OF_FEES":
            if aggregation.fee_minor:
                hypotheses.append(Hypothesis(
                    label="GATEWAY_FEE_DEDUCTION",
                    confidence=0.88,
                    rationale=(
                        f"The shortfall against the order total is accounted for exactly by the gateway fee "
                        f"of {_money(aggregation.fee_minor, currency)} recorded on settlement "
                        f"{aggregation.payment_id}."
                    ),
                    evidence_keys=[agg_key],
                ))
            if aggregation.tax_minor:
                hypotheses.append(Hypothesis(
                    label="TAX_DEDUCTION",
                    confidence=0.85,
                    rationale=(
                        f"Tax of {_money(aggregation.tax_minor, currency)} recorded on settlement "
                        f"{aggregation.payment_id} makes up the remainder of the shortfall."
                    ),
                    evidence_keys=[agg_key],
                ))
        if not aggregation.unique:
            unresolved.append(
                f"More than one combination of unmatched records sums to settlement {aggregation.payment_id}; "
                "the grouping needs an operator or a settlement breakdown file to settle."
            )
        else:
            unresolved.append(
                "No settlement breakdown file was supplied, so the aggregation cannot be booked "
                "automatically."
            )

    # -- one order paid out in pieces ------------------------------------
    split = find_split(view, context, unclaimed=unclaimed)
    if split:
        split_key = ledger.add(
            "SPLIT_ARITHMETIC",
            f"{len(split)} unclaimed settlements ({_join([s.payment_id for s in split])}) total "
            f"{_money(sum(s.gross_amount_minor for s in split), currency)}, which equals this order's amount.",
        )
        keys.add(split_key)
        hypotheses.append(Hypothesis(
            label="SPLIT_SETTLEMENT",
            confidence=0.75,
            rationale=(
                "This order's amount is matched exactly by the combined total of settlements that no other "
                "record claims, which is what a payout split across several transfers looks like."
            ),
            evidence_keys=[split_key] + cite("ORDER_AMOUNT"),
        ))

    # -- money that is present but does not agree ------------------------
    failing = {str(c.get("name")) for c in view.failing_checks()}
    matched = view.matched_candidate() or {}
    if "gross_amount_match" in failing and matched:
        delta = view.amount_minor - int(matched.get("gross_amount_minor") or 0)
        if view.refund_minor and abs(abs(delta) - view.refund_minor) <= context.policy.amount_tolerance_minor:
            hypotheses.append(Hypothesis(
                label="REFUND_OFFSET",
                confidence=0.80,
                rationale=(
                    f"The gap between the order and the settlement is {_money(abs(delta), currency)}, which "
                    f"equals the refund the merchant recorded against this order."
                ),
                evidence_keys=cite("ORDER_REFUND", "CHECK_GROSS_AMOUNT_MATCH", "ORDER_AMOUNT"),
            ))
        elif matched.get("fee_minor") is not None and abs(
            abs(delta) - int(matched.get("fee_minor") or 0) - int(matched.get("tax_minor") or 0)
        ) <= context.policy.amount_tolerance_minor:
            hypotheses.append(Hypothesis(
                label="GATEWAY_FEE_DEDUCTION",
                confidence=0.85,
                rationale=(
                    f"The gap of {_money(abs(delta), currency)} equals the fee and tax recorded on "
                    f"settlement {matched.get('payment_id')}, so the order was likely booked gross and the "
                    "settlement net."
                ),
                evidence_keys=cite("SETTLEMENT_BREAKDOWN", "CHECK_GROSS_AMOUNT_MATCH"),
            ))

    if "refund_consistency" in failing:
        hypotheses.append(Hypothesis(
            label="REFUND_OFFSET",
            confidence=0.70,
            rationale="The refund recorded by the merchant does not agree with the refund on the settlement.",
            evidence_keys=cite("CHECK_REFUND_CONSISTENCY", "ORDER_REFUND"),
        ))

    if "fee_tax_arithmetic" in failing:
        hypotheses.append(Hypothesis(
            label="GATEWAY_FEE_DEDUCTION",
            confidence=0.65,
            rationale=(
                "The settlement's own net does not reconcile against its gross minus fee, tax and refund, "
                "so the deduction schedule on the payout file is inconsistent."
            ),
            evidence_keys=cite("CHECK_FEE_TAX_ARITHMETIC", "SETTLEMENT_BREAKDOWN"),
        ))

    # -- identity -------------------------------------------------------
    if exception_type in (ExceptionType.DUPLICATE_CLAIM.value, ExceptionType.DUPLICATE_REFERENCE.value):
        hypotheses.append(Hypothesis(
            label="DUPLICATE_TRANSACTION",
            confidence=0.85,
            rationale=(
                "More than one record claims the same settlement, which is what a duplicated order or a "
                "re-submitted payment looks like."
            ),
            evidence_keys=cite("ENGINE_REASON", "ENGINE_DECISION"),
        ))

    # A settlement that is not due yet has no counterpart to contradict.
    # Whatever the retrieval happened to surface at the same amount is
    # noise, and offering "these name different transactions" at 0.85
    # invites an operator to chase a data problem that does not exist.
    settlement_not_due = view.classification == MatchClassification.PENDING_SETTLEMENT_WINDOW.value

    contradicting = [c for c in view.considered if _contradicts_identity(c)]
    if contradicting and not settlement_not_due:
        top = contradicting[0]
        hypotheses.append(Hypothesis(
            label="CONFLICTING_IDENTITY",
            confidence=0.85,
            rationale=(
                f"This order's reference ({view.reference_id or 'none'}) and the closest settlement's "
                f"reference ({top.get('order_reference') or 'none'}) name different transactions, so the "
                "amount agreement between them is a coincidence."
            ),
            evidence_keys=cite("ORDER_REFERENCE", "CANDIDATE_1", "AMOUNT_COLLISION"),
        ))

    # A candidate whose amount and date agree but whose identifiers do not
    # overlap at all is the classic reformatted-reference case — worth
    # naming, and deliberately not confident enough to act on.
    amount_only = [
        c for c in view.considered
        if not c.get("admissible")
        and "amount agreement is the only signal" in str(c.get("admissibility_reason") or "")
    ]
    if amount_only and not contradicting:
        hypotheses.append(Hypothesis(
            label="REFERENCE_MISMATCH",
            confidence=0.55,
            rationale=(
                "A settlement at exactly this amount was retrieved but refused because amount agreement "
                "alone is not evidence of identity. If the provider reformatted the reference, this could "
                "still be the counterpart."
            ),
            evidence_keys=cite("CANDIDATE_1", "AMOUNT_COLLISION", "ORDER_REFERENCE"),
        ))

    # -- absence --------------------------------------------------------
    if not view.matched_payment_id and classification not in _PENDING_CLASSIFICATIONS:
        if classification == MatchClassification.NO_CANDIDATES.value:
            hypotheses.append(Hypothesis(
                label="GENUINELY_MISSING",
                confidence=0.70,
                rationale=(
                    "Nothing at all was retrieved for this order — no record at this amount, no shared "
                    "identifier, nothing in the date window — which is what a payment that never reached "
                    "the provider looks like."
                ),
                evidence_keys=cite("ENGINE_REASON", "ORDER_AMOUNT", "ENGINE_DECISION"),
            ))
        elif classification in (
            MatchClassification.NO_ADMISSIBLE_CANDIDATE.value,
            MatchClassification.ALL_CANDIDATES_REJECTED.value,
        ) and not aggregation:
            hypotheses.append(Hypothesis(
                label="MISSING_SETTLEMENT",
                confidence=0.65,
                rationale=(
                    "Records were retrieved and every one was refused or judged to be a different payment, "
                    "so the provider appears not to have settled this order under any reference this run "
                    "can see."
                ),
                evidence_keys=cite("CANDIDATE_1", "ENGINE_REASON", "ENGINE_DECISION"),
            ))

    # -- what the data cannot settle -------------------------------------
    for stage in trace:
        if stage.stage == "BANK" and stage.status == NOT_EVALUATED and not roles.has_bank:
            unresolved.append(
                "No bank statement was supplied, so whether this money reached the bank account could "
                "not be checked at all."
            )
        if stage.stage == "BOOKS" and stage.status == NOT_EVALUATED and not roles.has_books:
            unresolved.append(
                "No accounting export was supplied, so whether this order was booked could not be checked."
            )
    if roles.has_bank and roles.has_gateway and not view.matched_source_type:
        # Only when the winning row carries no provenance. When it does, the
        # match is attributed to the file it actually came from and the
        # trace says so — repeating "cannot be attributed" there would be a
        # limitation the run does not have.
        unresolved.append(
            "A bank statement and a gateway file were reconciled as one settlement-side population, and "
            "this record's match carries no source provenance, so a bank credit cannot be attributed to "
            "it individually."
        )
    if not context.settlements and not view.matched_payment_id:
        unresolved.append(
            "The run's settlement-side population was not available to this investigation, so grouped or "
            "split payouts could not be tested arithmetically."
        )
    elif too_many_settlements and not view.matched_payment_id:
        unresolved.append(
            "Too many settlements in this window are unclaimed for a grouping search to be meaningful — a "
            "combination that happens to add up would be a coincidence, not the actual grouping."
        )

    hypotheses.sort(key=lambda h: h.confidence, reverse=True)
    return hypotheses, _dedupe(unresolved)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


# ---------------------------------------------------------------------------
# The grounding filter — the load-bearing safety property of the AI step
# ---------------------------------------------------------------------------

_NUMERIC = re.compile(r"\d[\d,._]*")
_IDENTIFIER = re.compile(r"\b(?=[A-Za-z0-9_\-]{6,})(?=[A-Za-z0-9_\-]*\d)[A-Za-z0-9_\-]+\b")
_MIN_GROUNDED_DIGITS = 3


def grounding_tokens(texts: Sequence[str]) -> tuple[set[str], set[str]]:
    """Every number and identifier the model is permitted to mention.

    Numbers are compared digits-only, so "1,525.00" and "1525" ground each
    other. Deliberately lenient in that one direction and nowhere else: a
    value that appears nowhere in the evidence has no way to pass.
    """
    numbers: set[str] = set()
    identifiers: set[str] = set()
    for text in texts:
        for token in _NUMERIC.findall(text or ""):
            digits = re.sub(r"\D", "", token)
            if len(digits) >= _MIN_GROUNDED_DIGITS:
                numbers.add(digits)
        for token in _IDENTIFIER.findall(text or ""):
            identifiers.add(token.upper())
    return numbers, identifiers


def ungrounded_claims(text: str, numbers: set[str], identifiers: set[str]) -> list[str]:
    """Numbers/identifiers asserted in `text` that the evidence never stated."""
    offenders: list[str] = []
    for token in _NUMERIC.findall(text or ""):
        digits = re.sub(r"\D", "", token)
        if len(digits) < _MIN_GROUNDED_DIGITS:
            continue
        if not any(digits in known or known in digits for known in numbers):
            offenders.append(token)
    for token in _IDENTIFIER.findall(text or ""):
        upper = token.upper()
        if upper in identifiers:
            continue
        if any(upper in known or known in upper for known in identifiers):
            continue
        digits = re.sub(r"\D", "", upper)
        if digits and any(digits in known or known in digits for known in numbers):
            continue
        offenders.append(token)
    return offenders


def filter_model_payload(
    payload: dict,
    *,
    evidence_index: dict[str, str],
    deterministic_labels: set[str],
) -> tuple[list[Hypothesis], Optional[str], int]:
    """Everything the model said, reduced to what the evidence supports.

    Four rules, all of them refusals:
      - only the semantic labels the model is allowed to contribute;
      - only evidence keys that actually exist;
      - a hypothesis citing nothing real is dropped entirely;
      - any amount or identifier asserted that the evidence never stated
        drops the whole claim, rather than being edited to look correct.

    Fields the model invents beyond the schema — a reasoning trace, a
    recommendation, a confidence in its own confidence — are never read
    and therefore can never reach a caller.
    """
    numbers, identifiers = grounding_tokens(list(evidence_index.values()))
    dropped = 0
    kept: list[Hypothesis] = []

    for raw in (payload.get("hypotheses") or [])[:6]:
        if not isinstance(raw, dict):
            dropped += 1
            continue
        label = str(raw.get("label") or "").strip().upper()
        if label not in AI_ALLOWED_LABELS or label in deterministic_labels:
            dropped += 1
            continue
        rationale = str(raw.get("rationale") or "").strip()
        if not rationale:
            dropped += 1
            continue
        if ungrounded_claims(rationale, numbers, identifiers):
            dropped += 1
            continue
        cited = [k for k in (raw.get("evidence_keys") or []) if k in evidence_index]
        if not cited:
            dropped += 1
            continue
        try:
            confidence = float(raw.get("confidence"))
        except (TypeError, ValueError):
            confidence = 0.5
        confidence = max(0.0, min(1.0, confidence))
        kept.append(Hypothesis(
            label=label,
            confidence=round(confidence, 3),
            rationale=rationale[:600],
            evidence_keys=cited[:6],
            source="AI",
        ))

    explanation = str(payload.get("explanation") or "").strip()
    if explanation and ungrounded_claims(explanation, numbers, identifiers):
        explanation = ""
        dropped += 1

    return kept, (explanation[:600] or None), dropped


# ---------------------------------------------------------------------------
# The model step
# ---------------------------------------------------------------------------

_AI_SYSTEM = """You are a reconciliation analyst's assistant inside a finance system. \
Deterministic code has already computed every amount, date and arithmetic relationship \
you are shown, and has already produced the explanations that arithmetic supports.

Your job is only the part arithmetic cannot do: reading whether two pieces of free text \
or two differently-formatted references denote the same merchant or the same payment.

Rules you must follow exactly:
- Use ONLY the facts in the evidence given to you. Never introduce an amount, a date, a \
reference, a payment id or a party that is not present in that evidence.
- Never restate a conclusion that deterministic code already reached.
- Every hypothesis must cite at least one evidence key, verbatim, from the list given.
- Allowed labels, and nothing else: MERCHANT_ALIAS (the two sides name the same merchant \
under different names), TRUNCATED_NARRATION (one side's text is a cut-off version of the \
other), REFERENCE_MISMATCH (the same underlying reference is formatted differently), \
CONFLICTING_IDENTITY (the two sides name genuinely different transactions).
- If the evidence does not support any of those, return an empty hypotheses list.
- Answer with JSON only, matching the schema. Do not include reasoning, working, or any \
field the schema does not name."""

_AI_SCHEMA = {
    "type": "object",
    "properties": {
        "hypotheses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string", "enum": sorted(AI_ALLOWED_LABELS)},
                    "confidence": {"type": "number"},
                    "rationale": {"type": "string"},
                    "evidence_keys": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["label", "confidence", "rationale", "evidence_keys"],
                "additionalProperties": False,
            },
        },
        "explanation": {"type": "string"},
    },
    "required": ["hypotheses", "explanation"],
    "additionalProperties": False,
}


def build_model_input(
    view: RecordView,
    context: InvestigationContext,
    ledger: EvidenceLedger,
    breakpoint_stage: Optional[str],
    breakpoint_kind: str,
    deterministic_labels: Sequence[str],
) -> str:
    """The bounded evidence bundle, and nothing else.

    The model never sees the database, the batch, or the record's stored
    outcome narrative — only keyed facts this module computed and the two
    pieces of free text whose meaning is actually in question.
    """
    candidates = [
        {
            "payment_id": c.get("payment_id"),
            "reference": c.get("order_reference"),
            "description": candidate_wording(view, context, c.get("payment_id")),
            "amount": _money(c.get("gross_amount_minor"), view.currency),
            "supporting": list(c.get("supporting_signals") or [])[:4],
            "contradicting": list(c.get("contradicting_signals") or [])[:4],
            "refused_because": c.get("admissibility_reason"),
        }
        for c in view.considered[:3]
    ]
    bundle = {
        "record_id": view.record_id,
        "breakpoint": {"stage": breakpoint_stage, "kind": breakpoint_kind},
        "order": {
            "reference": view.reference_id,
            "description": view.description,
            "amount": _money(view.amount_minor, view.currency),
            "date": _day(view.merchant.get("order_date")),
        },
        "settlement_side_candidates": candidates,
        "evidence": [{"key": k, "fact": t} for k, t in ledger.items],
        "already_established_by_code": list(deterministic_labels),
    }
    return json.dumps(bundle, indent=2, default=str)


# ---------------------------------------------------------------------------
# Policy — the action is decided here, never by the model
# ---------------------------------------------------------------------------

#: Failing checks that can never be argued away. Money known to be wrong
#: is not a matching question, and no hypothesis makes it one.
_UNRECONCILABLE_CHECKS = {"gross_amount_match", "currency_match", "fee_tax_arithmetic", "refund_consistency"}

_HUMAN_REVIEW_LABELS = {
    "AGGREGATED_SETTLEMENT", "SPLIT_SETTLEMENT", "DUPLICATE_TRANSACTION",
    "MERCHANT_ALIAS", "TRUNCATED_NARRATION", "REFERENCE_MISMATCH",
}


def decide_action(
    view: RecordView,
    breakpoint_kind: str,
    hypotheses: Sequence[Hypothesis],
) -> str:
    """RECONCILE | EXCEPTION | HUMAN_REVIEW | INVESTIGATE.

      RECONCILE     the record already reconciled and nothing here disputes it
      EXCEPTION     a known, certain problem — raise it
      HUMAN_REVIEW  a person must choose between competing readings
      INVESTIGATE   more data is needed before it can be settled

    Deliberately conservative in one direction only. An amount or currency
    mismatch, or any deterministic check that failed on the money itself,
    can never produce RECONCILE no matter how confident a hypothesis is —
    the model may suggest, policy decides.
    """
    failing = {str(c.get("name")) for c in view.failing_checks()}
    if failing & _UNRECONCILABLE_CHECKS:
        return EXCEPTION

    labels = {h.label for h in hypotheses}

    if view.outcome == ReconciliationOutcome.RECONCILED.value and not failing:
        # Nothing in an investigation books money; a clean record stays clean.
        return HUMAN_REVIEW if labels & {"DUPLICATE_TRANSACTION"} else RECONCILE

    if breakpoint_kind == PENDING or "PENDING_SETTLEMENT" in labels:
        return INVESTIGATE

    if breakpoint_kind == KIND_DATA_UNAVAILABLE:
        return INVESTIGATE

    if breakpoint_kind in (AMBIGUOUS, CONTRADICTORY):
        return HUMAN_REVIEW

    if labels & _HUMAN_REVIEW_LABELS:
        return HUMAN_REVIEW

    if labels & {"GENUINELY_MISSING", "MISSING_SETTLEMENT"}:
        return EXCEPTION

    if failing:
        return EXCEPTION

    return INVESTIGATE


def compose_explanation(
    trace: Sequence[TraceStage],
    breakpoint_stage: Optional[str],
    breakpoint_kind: str,
    hypotheses: Sequence[Hypothesis],
) -> str:
    """One deterministic sentence, in the operator's language.

    Built the same way `explain.py` builds its own: from recorded signals,
    never narrated by a model. If the model produced a grounded sentence
    it replaces this one at the call site; this is what stands otherwise,
    and what stands when the provider is down.
    """
    if breakpoint_stage is None:
        if breakpoint_kind == KIND_DATA_UNAVAILABLE:
            return (
                "This run had nothing to reconcile this order against, so no stage beyond the ledger row "
                "itself could be evaluated."
            )
        return "Every stage this run could evaluate was found, and nothing in the evidence disputes it."

    stage = next((s for s in trace if s.stage == breakpoint_stage), None)
    lead = f"The money trail stops at {breakpoint_stage.lower()}: {stage.detail if stage else ''}".strip()
    top = hypotheses[0] if hypotheses else None
    if top is None:
        return lead
    return f"{lead} The most likely explanation is {top.label.replace('_', ' ').lower()} — {top.rationale}"


# ---------------------------------------------------------------------------
# The investigator
# ---------------------------------------------------------------------------

class Investigator:
    """Read-only. Gathers bounded evidence, ranks explanations, decides an
    action from policy, and writes exactly one audit event.

    The provider chain is injected so every test is offline and
    deterministic. `Investigator()` with no chain builds one lazily at use
    time, and a chain that cannot be built is a degraded but correct
    outcome — deterministic evidence with `ai_used` false — not an error.
    """

    def __init__(
        self,
        chain: Any = None,
        *,
        timeout_s: float = AI_TIMEOUT_SECONDS,
        max_calls: int = MAX_AI_CALLS,
        chain_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._chain = chain
        self._chain_attempted = chain is not None
        self._chain_factory = chain_factory
        self._timeout_s = timeout_s
        self._max_calls = max_calls

    # -- provider plumbing ----------------------------------------------
    def _get_chain(self) -> Any:
        if not self._chain_attempted:
            self._chain_attempted = True
            factory = self._chain_factory
            if factory is None:
                def factory():  # pragma: no cover - trivial indirection
                    from app.engine.providers import build_chain  # imported lazily: owned by another module
                    return build_chain()
            try:
                self._chain = factory()
            except Exception:  # noqa: BLE001 — a missing or broken provider layer degrades, never raises
                self._chain = None
        return self._chain

    @staticmethod
    def _chain_status(chain: Any) -> str:
        if chain is None:
            return AI_UNAVAILABLE
        try:
            status = str(chain.status)
        except Exception:  # noqa: BLE001
            return AI_UNAVAILABLE
        return status if status in (AI_AVAILABLE, AI_FALLBACK_ACTIVE, AI_UNAVAILABLE) else AI_UNAVAILABLE

    # -- the public entry point ------------------------------------------
    def investigate(
        self,
        context: InvestigationContext,
        *,
        use_ai: bool = True,
        write_audit: bool = True,
    ) -> Investigation:
        view = RecordView.from_row(context.record)
        roles = SourceRoles.from_sources(context.sources)
        trace = build_trace(view, roles, context.policy)
        breakpoint_stage, breakpoint_kind = breakpoint_of(trace)

        ledger = build_evidence(view, context, trace)
        hypotheses, unresolved = deterministic_hypotheses(view, context, roles, trace, ledger)

        ai_used = False
        ai_provider: Optional[str] = None
        ai_status = AI_UNAVAILABLE
        dropped = 0
        model_explanation: Optional[str] = None

        consult = use_ai and self._should_consult_model(view, hypotheses)
        if consult:
            chain = self._get_chain()
            ai_status = self._chain_status(chain)
            if chain is None or ai_status == AI_UNAVAILABLE:
                # Degrading to deterministic-only is a correct outcome, not
                # an error: the findings above stand on their own and
                # nothing is invented to fill the gap.
                unresolved.append(
                    "No language provider was available, so wording-based explanations such as a merchant "
                    "alias or a truncated narration were not explored; the findings above are entirely "
                    "deterministic."
                )
            else:
                payload, provider, failure = self._ask_model(
                    view, context, ledger, breakpoint_stage, breakpoint_kind,
                    [h.label for h in hypotheses],
                )
                if payload is None:
                    ai_status = AI_UNAVAILABLE
                    unresolved.append(
                        f"The semantic tier did not answer ({failure}), so wording-based explanations "
                        "such as a merchant alias or a truncated narration were not explored."
                    )
                else:
                    ai_used = True
                    ai_provider = provider
                    kept, model_explanation, dropped = filter_model_payload(
                        payload,
                        evidence_index=ledger.as_index(),
                        deterministic_labels={h.label for h in hypotheses},
                    )
                    hypotheses = sorted(hypotheses + kept, key=lambda h: h.confidence, reverse=True)
                    if not kept and dropped:
                        unresolved.append(
                            "The semantic tier proposed an explanation the evidence did not support, so it "
                            "was discarded rather than shown."
                        )
        else:
            # Not consulted, because the deterministic evidence already
            # settles the record. Reporting AI_UNAVAILABLE here would be a
            # claim about provider health that nothing has established —
            # and probing a provider purely to render a status label would
            # spend quota to answer a question nobody asked.
            ai_status = AI_NOT_CONSULTED

        action = decide_action(view, breakpoint_kind, hypotheses)
        explanation = model_explanation or compose_explanation(
            trace, breakpoint_stage, breakpoint_kind, hypotheses
        )

        investigation = Investigation(
            record_id=view.record_id,
            breakpoint_stage=breakpoint_stage,
            breakpoint_kind=breakpoint_kind,
            trace=list(trace),
            confirmed_evidence=ledger.texts,
            hypotheses=hypotheses,
            unresolved=_dedupe(unresolved),
            recommended_action=action,
            ai_used=ai_used,
            ai_provider=ai_provider,
            ai_status=ai_status,
            explanation=explanation,
            evidence_index=ledger.as_index(),
            ai_claims_dropped=dropped,
            batch_id=view.batch_id or (context.batch or {}).get("batch_id"),
        )

        if write_audit:
            self._write_audit(view, investigation)
        return investigation

    # -- internals -------------------------------------------------------
    @staticmethod
    def _should_consult_model(view: RecordView, hypotheses: Sequence[Hypothesis]) -> bool:
        """Spend a call only where wording is actually the open question.

        Not on a record the engine already reconciled, not when the
        arithmetic already settles it, and not when there is no free text
        to read.
        """
        if view.outcome == ReconciliationOutcome.RECONCILED.value:
            return False
        if any(h.confidence >= AI_SKIP_CONFIDENCE for h in hypotheses):
            return False
        has_text = bool(view.description) or bool(view.reference_id)
        candidate_text = any(
            c.get("order_reference") or c.get("payment_id") for c in view.considered
        )
        return has_text and candidate_text

    def _ask_model(
        self,
        view: RecordView,
        context: InvestigationContext,
        ledger: EvidenceLedger,
        breakpoint_stage: Optional[str],
        breakpoint_kind: str,
        deterministic_labels: Sequence[str],
    ) -> tuple[Optional[dict], Optional[str], str]:
        """One bounded call, hard wall-clock ceiling, never fatal."""
        from app.engine.matching import call_with_timeout  # reuse the engine's one timeout primitive

        user = build_model_input(
            view, context, ledger, breakpoint_stage, breakpoint_kind, deterministic_labels
        )
        chain = self._chain

        def _call():
            return chain.complete_json(
                system=_AI_SYSTEM,
                user=user,
                schema=_AI_SCHEMA,
                timeout_s=min(AI_PROVIDER_TIMEOUT_SECONDS, self._timeout_s),
            )

        try:
            result = call_with_timeout(_call, timeout_seconds=self._timeout_s)
        except Exception as exc:  # noqa: BLE001 — any provider failure degrades identically
            return None, None, type(exc).__name__

        payload, provider = _unpack_chain_result(result)
        if not isinstance(payload, dict):
            return None, None, "malformed provider response"
        return payload, provider, ""

    @staticmethod
    def _write_audit(view: RecordView, investigation: Investigation) -> None:
        """One event, carrying the finding and nothing sensitive.

        `prior_state` and `new_state` are deliberately identical: the
        audit trail should show on its face that an investigation observed
        a record and did not move it. The prompt and any raw model text
        are never written.
        """
        from app.ledger import audit  # local import: the engine stays importable without a DB

        try:
            audit.append_event(
                transaction_id=view.record_id,
                event_type="AI_INVESTIGATION",
                prior_state=view.outcome or None,
                new_state=view.outcome or None,
                evidence_ref=investigation.batch_id,
                payload={
                    "record_id": view.record_id,
                    "batch_id": investigation.batch_id,
                    "breakpoint_stage": investigation.breakpoint_stage,
                    "breakpoint_kind": investigation.breakpoint_kind,
                    "hypothesis_labels": [h.label for h in investigation.hypotheses],
                    "ai_hypothesis_labels": [h.label for h in investigation.hypotheses if h.source == "AI"],
                    "recommended_action": investigation.recommended_action,
                    "ai_used": investigation.ai_used,
                    "ai_provider": investigation.ai_provider,
                    "ai_status": investigation.ai_status,
                    "ai_claims_dropped": investigation.ai_claims_dropped,
                    "read_only": True,
                },
            )
        except Exception:  # noqa: BLE001 — a ledger failure must not lose the investigation
            pass


def _unpack_chain_result(result: Any) -> tuple[Any, Optional[str]]:
    """`FallbackChain.complete_json` returns (payload, provider_name)."""
    if isinstance(result, tuple) and len(result) == 2:
        return result[0], (str(result[1]) if result[1] is not None else None)
    return result, None


# ---------------------------------------------------------------------------
# Batch-level aggregate, for the results dashboard
# ---------------------------------------------------------------------------

def breakpoint_summary(rows: Sequence[dict], sources: Sequence[dict] | None, policy: PolicyConfig) -> dict:
    """Counts by breakpoint stage and kind across a whole batch.

    Purely deterministic and model-free: this is the same trace analysis
    every record gets, aggregated, so the dashboard and the drill-in can
    never disagree.
    """
    roles = SourceRoles.from_sources(sources)
    by_stage: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    by_stage_kind: dict[str, dict[str, int]] = {}
    not_evaluated: dict[str, int] = {}

    for row in rows:
        view = RecordView.from_row(row)
        trace = build_trace(view, roles, policy)
        stage, kind = breakpoint_of(trace)
        stage_key = stage or "NONE"
        by_stage[stage_key] = by_stage.get(stage_key, 0) + 1
        by_kind[kind] = by_kind.get(kind, 0) + 1
        by_stage_kind.setdefault(stage_key, {})
        by_stage_kind[stage_key][kind] = by_stage_kind[stage_key].get(kind, 0) + 1
        for t in trace:
            if t.status == NOT_EVALUATED:
                not_evaluated[t.stage] = not_evaluated.get(t.stage, 0) + 1

    return {
        "total_records": len(rows),
        "stages": list(STAGES),
        "by_breakpoint_stage": by_stage,
        "by_breakpoint_kind": by_kind,
        "by_stage_and_kind": by_stage_kind,
        "not_evaluated_counts": not_evaluated,
        "sources_present": {
            "gateway": roles.has_gateway,
            "bank": roles.has_bank,
            "accounting": roles.has_books,
            "orders": roles.has_orders,
            "declared": roles.declared,
        },
    }
