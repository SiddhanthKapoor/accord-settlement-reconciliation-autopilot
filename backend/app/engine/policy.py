"""
The decision core. One function, `reconcile`, takes a merchant record and
whatever candidates the index found for it, runs a fixed sequence of
deterministic checks, and aggregates them into exactly one of
RECONCILED / EXCEPTION / HUMAN_REVIEW — always traceable to a specific
check, never a black-box score.

Policy, in one paragraph: a missing settlement is an EXCEPTION (a known,
certain problem). A duplicate reference that can't be disambiguated by
amount is HUMAN_REVIEW (genuine ambiguity about which record is real,
not a known-wrong value). Any deterministic arithmetic or timing check
that fails is an EXCEPTION (a known, certain problem again). An
AI-resolved reference match below the confidence threshold is
HUMAN_REVIEW, full stop, regardless of how clean everything else looks —
that gate is enforced here, not left to the model's own judgment.
Everything else is RECONCILED.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from app.domain.models import (
    CheckResult,
    CheckStatus,
    MerchantRecord,
    PolicyConfig,
    ReconciliationOutcome,
    ReconciliationResult,
    RazorpaySettlementRecord,
)
from app.engine import matching, normalize
from app.engine.semantic import SemanticVerifier


def _check(name: str, ok: bool, expected: str, observed: str, detail: str, *, warn: bool = False, confidence: float | None = None) -> CheckResult:
    status = CheckStatus.PASS if ok else (CheckStatus.WARN if warn else CheckStatus.FAIL)
    return CheckResult(name=name, status=status, expected=expected, observed=observed, detail=detail, confidence=confidence)


@dataclass
class _Resolution:
    candidate: RazorpaySettlementRecord | None
    checks: list[CheckResult]
    ai_invoked: bool = False
    ai_confidence: float | None = None
    ai_backend: str | None = None
    ai_calls: int = 0
    short_circuit: ReconciliationOutcome | None = None
    short_circuit_reason: str | None = None


def _resolve_candidate(
    merchant: MerchantRecord,
    candidates: list[RazorpaySettlementRecord],
    index: matching.ReferenceIndex,
    policy: PolicyConfig,
    semantic_verifier: SemanticVerifier,
) -> _Resolution:
    if len(candidates) == 1:
        return _Resolution(
            candidates[0],
            [_check("reference_match", True, "1 candidate", "1 candidate", "Exact normalized-reference match.")],
        )

    if len(candidates) > 1:
        disambiguated = matching.disambiguate_duplicates(merchant, candidates, policy)
        if disambiguated is not None:
            return _Resolution(
                disambiguated,
                [_check(
                    "reference_match", True, "1 of N disambiguated by amount", disambiguated.payment_id,
                    f"{len(candidates)} settlement records share this reference; exactly one matched on amount.",
                )],
            )
        return _Resolution(
            None,
            [_check(
                "duplicate_reference", False, "exactly one plausible candidate", f"{len(candidates)} equally plausible candidates",
                f"{len(candidates)} settlement records share this reference and more than one (or none) match "
                "on amount — cannot be disambiguated deterministically.",
                warn=True,
            )],
            short_circuit=ReconciliationOutcome.HUMAN_REVIEW,
            short_circuit_reason="Duplicate reference requires manual disambiguation.",
        )

    # No exact candidates at all.
    if not policy.enable_fuzzy_matching:
        return _Resolution(
            None,
            [_check("settlement_presence", False, "1 settlement record", "0 settlement records",
                    "No settlement record shares this reference, and non-exact matching is disabled by policy.")],
            short_circuit=ReconciliationOutcome.EXCEPTION,
            short_circuit_reason="No corresponding Razorpay settlement record found for this merchant record.",
        )

    # Try fuzzy/semantic resolution.
    outcome = matching.resolve_fuzzy_or_semantic(merchant, index, policy, semantic_verifier)
    if outcome.candidate is not None:
        confident_enough = outcome.ai_confidence is None or outcome.ai_confidence >= policy.ai_confidence_threshold
        check = _check(
            "reference_match", confident_enough, f">= {policy.ai_confidence_threshold} confidence", str(outcome.ai_confidence or "n/a"),
            outcome.detail, warn=not confident_enough, confidence=outcome.ai_confidence,
        )
        return _Resolution(outcome.candidate, [check], outcome.ai_invoked, outcome.ai_confidence, outcome.ai_backend,
                           ai_calls=outcome.ai_calls)

    if outcome.method == "semantic" and outcome.verdict == "AMBIGUOUS":
        return _Resolution(
            None,
            [_check("reference_match", False, "SAME", "AMBIGUOUS", outcome.detail, warn=True, confidence=outcome.ai_confidence)],
            outcome.ai_invoked, outcome.ai_confidence, outcome.ai_backend, ai_calls=outcome.ai_calls,
            short_circuit=ReconciliationOutcome.HUMAN_REVIEW,
            short_circuit_reason="Reference could not be matched deterministically, and the semantic classifier could not confidently rule it in or out.",
        )

    if outcome.method == "semantic_error":
        # AI failure fallback: a provider error or timeout is its own
        # outcome, always routed to human review — never silently
        # reconciled, and never allowed to crash the batch this record
        # is part of.
        return _Resolution(
            None,
            [_check("reference_match", False, "AI provider available", "provider error", outcome.detail, warn=True)],
            outcome.ai_invoked, outcome.ai_confidence, outcome.ai_backend, ai_calls=outcome.ai_calls,
            short_circuit=ReconciliationOutcome.HUMAN_REVIEW,
            short_circuit_reason=outcome.detail,
        )

    # method == "none", or semantic said DIFFERENT — genuinely absent.
    return _Resolution(
        None,
        [_check("settlement_presence", False, "1 settlement record", "0 settlement records", outcome.detail)],
        outcome.ai_invoked, outcome.ai_confidence, outcome.ai_backend, ai_calls=outcome.ai_calls,
        short_circuit=ReconciliationOutcome.EXCEPTION,
        short_circuit_reason="No corresponding Razorpay settlement record found for this merchant record.",
    )


def _run_financial_checks(merchant: MerchantRecord, candidate: RazorpaySettlementRecord, policy: PolicyConfig) -> list[CheckResult]:
    checks = []

    # Amounts are integers in minor units with no currency attached, so
    # 50000 paise and 50000 cents compare equal. Every other check in
    # this function would pass on a cross-currency pair. Currency is
    # therefore checked first and on its own.
    currency_ok = merchant.currency.upper() == candidate.currency.upper()
    checks.append(_check(
        "currency_match", currency_ok, merchant.currency.upper(), candidate.currency.upper(),
        "Both sides are denominated in the same currency." if currency_ok
        else f"Merchant recorded {merchant.currency.upper()} but the settlement is in "
             f"{candidate.currency.upper()}; the amounts are not comparable.",
    ))

    amount_ok = normalize.amounts_match(merchant.amount_minor, candidate.gross_amount_minor, policy.amount_tolerance_minor)
    checks.append(_check(
        "gross_amount_match", amount_ok,
        f"{merchant.amount_minor} minor units (±{policy.amount_tolerance_minor})", f"{candidate.gross_amount_minor} minor units",
        "Merchant order amount matches the Razorpay gross amount." if amount_ok
        else f"Merchant recorded {merchant.amount_minor} but Razorpay's gross amount was {candidate.gross_amount_minor}.",
    ))

    expected_net = candidate.gross_amount_minor - candidate.fee_minor - candidate.tax_minor - candidate.refund_amount_minor
    net_ok = normalize.amounts_match(candidate.net_amount_minor, expected_net, policy.amount_tolerance_minor)
    checks.append(_check(
        "fee_tax_arithmetic", net_ok,
        f"{expected_net} minor units (gross - fee - tax - refund)", f"{candidate.net_amount_minor} minor units",
        "Net settlement amount is consistent with gross, fee, tax, and refund." if net_ok
        else f"Net settlement {candidate.net_amount_minor} does not reconcile against gross-fee-tax-refund ({expected_net}).",
    ))

    delay = normalize.days_between(candidate.order_date, candidate.settlement_date)
    delay_ok = delay <= policy.max_settlement_delay_days
    checks.append(_check(
        "settlement_timing", delay_ok, f"<= {policy.max_settlement_delay_days} days", f"{delay} days",
        "Settlement occurred within the normal delay window." if delay_ok
        else f"Settlement took {delay} days, exceeding the {policy.max_settlement_delay_days}-day policy threshold.",
    ))

    merchant_refunded = merchant.status in ("refunded", "partially_refunded")
    razorpay_refunded = candidate.refund_amount_minor > 0
    refund_amounts_ok = normalize.amounts_match(merchant.refund_amount_minor, candidate.refund_amount_minor, policy.amount_tolerance_minor)
    refund_ok = (merchant_refunded == razorpay_refunded) and refund_amounts_ok
    checks.append(_check(
        "refund_consistency", refund_ok, f"{merchant.refund_amount_minor} minor units refunded", f"{candidate.refund_amount_minor} minor units refunded",
        "Refund status and amount agree on both sides." if refund_ok
        else "Refund status or amount disagrees between the merchant record and the settlement record.",
    ))

    return checks


def reconcile(
    merchant: MerchantRecord,
    candidates: list[RazorpaySettlementRecord],
    index: matching.ReferenceIndex,
    policy: PolicyConfig,
    semantic_verifier: SemanticVerifier,
    record_id: str,
) -> ReconciliationResult:
    started = time.perf_counter()

    resolution = _resolve_candidate(merchant, candidates, index, policy, semantic_verifier)
    checks = list(resolution.checks)

    if resolution.short_circuit is not None:
        latency_ms = (time.perf_counter() - started) * 1000
        return ReconciliationResult(
            record_id=record_id, outcome=resolution.short_circuit, reason=resolution.short_circuit_reason or "",
            checks=checks, matched_payment_id=None, candidate_count=len(candidates),
            ai_invoked=resolution.ai_invoked, ai_calls=resolution.ai_calls, ai_confidence=resolution.ai_confidence,
            ai_backend=resolution.ai_backend, policy_threshold=policy.ai_confidence_threshold, latency_ms=latency_ms,
        )

    assert resolution.candidate is not None
    checks.extend(_run_financial_checks(merchant, resolution.candidate, policy))

    fails = [c for c in checks if c.status == CheckStatus.FAIL]
    warns = [c for c in checks if c.status == CheckStatus.WARN]

    if fails:
        outcome = ReconciliationOutcome.EXCEPTION
        reason = "; ".join(f"{c.name}: {c.detail}" for c in fails)
    elif warns:
        outcome = ReconciliationOutcome.HUMAN_REVIEW
        reason = "; ".join(f"{c.name}: {c.detail}" for c in warns)
    else:
        outcome = ReconciliationOutcome.RECONCILED
        reason = "All checks passed."

    latency_ms = (time.perf_counter() - started) * 1000
    return ReconciliationResult(
        record_id=record_id, outcome=outcome, reason=reason, checks=checks,
        matched_payment_id=resolution.candidate.payment_id, candidate_count=len(candidates),
        ai_invoked=resolution.ai_invoked, ai_calls=resolution.ai_calls, ai_confidence=resolution.ai_confidence,
        ai_backend=resolution.ai_backend, policy_threshold=policy.ai_confidence_threshold, latency_ms=latency_ms,
    )
