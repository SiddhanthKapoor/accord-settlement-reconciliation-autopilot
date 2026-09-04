"""
Decision explanation, built from recorded signals.

Every string here is derived from structured evidence the engine already
produced. Nothing in this module calls a model, and the model is never
asked to narrate a decision it did not make: an explanation that sounds
plausible but is not the actual reason is worse than no explanation, and
in a finance tool it is the kind of thing that survives right up until
someone audits it.

Three levels of disclosure, from the same evidence:

    level 1  one sentence a finance operator can act on
    level 2  the signals that decided it
    level 3  scores, thresholds, and the policy rule that fired
"""

from __future__ import annotations

from app.domain.models import (
    CandidateAssessment, CheckResult, CheckStatus, ExceptionType, MatchClassification,
    MerchantRecord, PolicyConfig, ReconciliationOutcome, Severity,
)

# Plain-English account of each way the settlement side can resolve, and
# what a finance operator should do about it.
_CLASSIFICATION_COPY: dict[MatchClassification, tuple[str, str]] = {
    MatchClassification.EXACT_REFERENCE: (
        "The settlement reference matched this order's reference exactly after normalisation.",
        "No action needed.",
    ),
    MatchClassification.DISAMBIGUATED_BY_AMOUNT: (
        "Several settlements shared this reference and exactly one matched on amount.",
        "No action needed, though repeated duplicate references are worth raising with the provider.",
    ),
    MatchClassification.CORROBORATED: (
        "The reference did not match exactly, but independent signals agreed on the same settlement "
        "without needing the semantic classifier.",
        "No action needed.",
    ),
    MatchClassification.SEMANTIC_CONFIRMED: (
        "Deterministic matching could not settle it, so the semantic classifier confirmed the pairing "
        "above the required confidence.",
        "No action needed. Spot-check if reference formats are drifting.",
    ),
    MatchClassification.NO_CANDIDATES: (
        "No settlement record was retrieved for this order at all.",
        "Confirm the payment was captured, then raise a missing-settlement query with the provider.",
    ),
    MatchClassification.NO_ADMISSIBLE_CANDIDATE: (
        "No candidate satisfied the minimum independent evidence requirements.",
        "Treat as a genuinely missing settlement. The rejected records below are listed with the reason "
        "each was refused, so a reviewer can confirm none is the real counterpart.",
    ),
    MatchClassification.ALL_CANDIDATES_REJECTED: (
        "Every candidate with enough evidence to be considered was then judged to be a different payment.",
        "Treat as a missing settlement, and check whether the provider recorded the payment under a "
        "different reference.",
    ),
    MatchClassification.AMBIGUOUS_MULTIPLE: (
        "More than one settlement remains plausible and none dominates enough to decide automatically.",
        "Human review: pick the correct settlement, or confirm the duplicate with the provider.",
    ),
    MatchClassification.SEMANTIC_UNRESOLVED: (
        "The semantic classifier could neither confirm nor rule out the best candidate.",
        "Human review: a person should compare the two records directly.",
    ),
    MatchClassification.PENDING_SETTLEMENT_WINDOW: (
        "This payment is too recent for a settlement to exist yet.",
        "No action needed now. Re-run reconciliation once the settlement window has passed.",
    ),
    MatchClassification.PROVIDER_ERROR: (
        "The semantic classifier was unavailable, so this record could not be resolved automatically.",
        "Retry once the provider recovers; review manually if it persists.",
    ),
}

# Which failing deterministic check maps to which operator-facing
# exception, most severe first — a currency mismatch is a different
# conversation from a settlement that arrived late.
_CHECK_TO_EXCEPTION: list[tuple[str, ExceptionType, Severity]] = [
    ("currency_match", ExceptionType.CURRENCY_MISMATCH, Severity.HIGH),
    ("gross_amount_match", ExceptionType.AMOUNT_MISMATCH, Severity.HIGH),
    ("fee_tax_arithmetic", ExceptionType.FEE_TAX_INCONSISTENT, Severity.HIGH),
    ("refund_consistency", ExceptionType.REFUND_MISMATCH, Severity.MEDIUM),
    ("settlement_timing", ExceptionType.SETTLEMENT_DELAYED, Severity.MEDIUM),
]

_CHECK_COPY: dict[str, str] = {
    "currency_match": "the two sides are denominated in different currencies, so their amounts are not comparable",
    "gross_amount_match": "the settled gross amount does not equal the order amount",
    "fee_tax_arithmetic": "the net settled amount does not reconcile against gross minus fee, tax and refund",
    "settlement_timing": "the settlement arrived later than the configured window allows",
    "refund_consistency": "the refund recorded by the merchant does not agree with the settlement",
}


def classify_exception(
    classification: MatchClassification,
    checks: list[CheckResult],
    matched: bool,
) -> tuple[ExceptionType | None, Severity | None]:
    """What kind of problem this is, in operator terms."""
    if matched:
        failing = [c.name for c in checks if c.status == CheckStatus.FAIL]
        for name, exception_type, severity in _CHECK_TO_EXCEPTION:
            if name in failing:
                return exception_type, severity
        warning = [c.name for c in checks if c.status == CheckStatus.WARN]
        if "reference_match" in warning:
            return ExceptionType.LOW_CONFIDENCE_MATCH, Severity.MEDIUM
        if "duplicate_reference" in warning:
            return ExceptionType.DUPLICATE_REFERENCE, Severity.MEDIUM
        return None, None

    return {
        MatchClassification.PENDING_SETTLEMENT_WINDOW: (ExceptionType.PENDING_SETTLEMENT, Severity.LOW),
        MatchClassification.AMBIGUOUS_MULTIPLE: (ExceptionType.AMBIGUOUS_MATCH, Severity.MEDIUM),
        MatchClassification.SEMANTIC_UNRESOLVED: (ExceptionType.AMBIGUOUS_MATCH, Severity.MEDIUM),
        MatchClassification.PROVIDER_ERROR: (ExceptionType.PROVIDER_ERROR, Severity.MEDIUM),
    }.get(classification, (ExceptionType.MISSING_SETTLEMENT, Severity.HIGH))


def build_explanation(
    merchant: MerchantRecord,
    classification: MatchClassification,
    checks: list[CheckResult],
    matched: bool,
    considered: list[CandidateAssessment],
) -> str:
    """Level 1: one sentence, in the operator's language."""
    headline, _ = _CLASSIFICATION_COPY.get(
        classification, ("This record could not be resolved automatically.", "")
    )

    if matched:
        failing = [c for c in checks if c.status == CheckStatus.FAIL]
        if failing:
            reasons = [_CHECK_COPY.get(c.name, c.name) for c in failing]
            return (
                f"A settlement was matched to this order, but {_join(reasons)}."
            )
        warnings = [c for c in checks if c.status == CheckStatus.WARN]
        if warnings:
            return (
                "A settlement was matched, but not with enough confidence to reconcile it automatically."
            )
        return headline

    if classification is MatchClassification.NO_ADMISSIBLE_CANDIDATE and considered:
        closest = considered[0]
        return (
            f"{headline} The closest record ({closest.payment_id}) was rejected because "
            f"{closest.admissibility_reason}."
        )
    return headline


def recommended_action(
    classification: MatchClassification,
    exception_type: ExceptionType | None,
    matched: bool,
) -> str:
    """What to actually do next."""
    if matched and exception_type is not None:
        return {
            ExceptionType.CURRENCY_MISMATCH: "Do not settle. Confirm which currency the order was placed in.",
            ExceptionType.AMOUNT_MISMATCH: "Compare the order and settlement amounts with the provider before writing off the difference.",
            ExceptionType.FEE_TAX_INCONSISTENT: "Recompute the fee schedule for this payment method; the deduction does not match the stated rate.",
            ExceptionType.REFUND_MISMATCH: "Reconcile the refund ledger against the provider's refund record.",
            ExceptionType.SETTLEMENT_DELAYED: "Chase the provider for the delayed settlement; the money is identified but late.",
            ExceptionType.LOW_CONFIDENCE_MATCH: "Human review: confirm the suggested settlement is the right one.",
            ExceptionType.DUPLICATE_REFERENCE: "Human review: decide which settlement this order corresponds to.",
        }.get(exception_type, "Review this record manually.")

    _, action = _CLASSIFICATION_COPY.get(classification, ("", "Review this record manually."))
    return action


def evidence_summary(
    checks: list[CheckResult], considered: list[CandidateAssessment], matched_payment_id: str | None
) -> dict:
    """Level 2: the signals, without the arithmetic."""
    matched = next((c for c in considered if c.payment_id == matched_payment_id), None)
    return {
        "checks": [
            {"name": c.name, "status": c.status.value, "detail": c.detail}
            for c in checks
        ],
        "matched_candidate": matched.model_dump(mode="json") if matched else None,
        "rejected_candidates": [
            c.model_dump(mode="json") for c in considered if c.payment_id != matched_payment_id
        ],
    }


def _join(items: list[str]) -> str:
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + f", and {items[-1]}"
