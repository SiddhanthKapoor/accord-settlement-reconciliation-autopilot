"""
Candidate resolution: given a merchant record, find the Razorpay
record(s) it should be checked against. Three tiers, cheapest first:

1. Exact normalized-reference lookup (O(1) via a prebuilt index).
2. Fuzzy description match within a bounded date window — deterministic,
   no model call, resolves cases where the reference truly doesn't match
   but the overlap is strong enough to be confident without judgment.
3. Semantic match via the model (semantic.py) — only for the residual
   cases where (2) found plausible-but-inconclusive overlap.

Anything that falls through all three is a genuine absence: no
settlement record exists for this merchant record, full stop — that's
not ambiguous, it's the MISSING_SETTLEMENT case, handled as a
deterministic exception in policy.py.
"""

from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass

from app.domain.models import MerchantRecord, PolicyConfig, RazorpaySettlementRecord
from app.engine import normalize
from app.engine.semantic import MatchCandidateText, SemanticVerifier

# A real wall-clock ceiling on the one external call this system makes.
# Enforced with a worker thread + future.result(timeout=...) rather than
# trusting the provider's own client-side timeout, so a genuinely hung
# call can never stall a batch of thousands of records indefinitely.
SEMANTIC_CALL_TIMEOUT_SECONDS = 10.0

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="semantic-call")


def call_with_timeout(fn, *args, timeout_seconds: float):
    future = _executor.submit(fn, *args)
    try:
        return future.result(timeout=timeout_seconds)
    except FutureTimeoutError as exc:
        raise TimeoutError(f"semantic verifier did not respond within {timeout_seconds}s") from exc


class ReferenceIndex:
    """Built once per batch. Both the exact-match index and the
    date-sorted list for fuzzy fallback live here so matching a batch of
    N records against M Razorpay records doesn't redo O(M) work per
    record for the exact-match path."""

    def __init__(self, razorpay_records: list[RazorpaySettlementRecord]) -> None:
        self.by_reference: dict[str, list[RazorpaySettlementRecord]] = defaultdict(list)
        self.all_records = razorpay_records
        for r in razorpay_records:
            self.by_reference[normalize.normalize_reference(r.order_reference)].append(r)

    def exact_candidates(self, merchant: MerchantRecord) -> list[RazorpaySettlementRecord]:
        key = normalize.normalize_reference(merchant.reference_id)
        if not key:
            return []
        return self.by_reference.get(key, [])

    def nearby_by_date(self, merchant: MerchantRecord, window_days: int) -> list[RazorpaySettlementRecord]:
        return [r for r in self.all_records if normalize.days_between(merchant.order_date, r.order_date) <= window_days]


@dataclass
class FuzzyMatchOutcome:
    candidate: RazorpaySettlementRecord | None
    method: str  # "fuzzy_deterministic" | "semantic" | "none"
    ai_invoked: bool
    ai_confidence: float | None
    ai_backend: str | None
    verdict: str | None  # SAME / DIFFERENT / AMBIGUOUS / None
    detail: str


def _amount_plausible(merchant_amount: int, candidate_amount: int) -> bool:
    """Generous on purpose (0.5x-2x) — this only filters out wildly
    implausible candidates before ranking by text similarity; the real
    amount check still happens exactly in policy.py's financial checks."""
    if merchant_amount <= 0:
        return True
    ratio = candidate_amount / merchant_amount
    return 0.5 <= ratio <= 2.0


def resolve_fuzzy_or_semantic(
    merchant: MerchantRecord,
    index: ReferenceIndex,
    policy: PolicyConfig,
    semantic_verifier: SemanticVerifier,
) -> FuzzyMatchOutcome:
    """Called only when exact reference lookup found nothing."""
    nearby = index.nearby_by_date(merchant, policy.candidate_search_window_days)
    if not nearby:
        return FuzzyMatchOutcome(None, "none", False, None, None, None, "No Razorpay record found within the search window.")

    # Amount-plausible pre-filter: a candidate whose amount is wildly
    # different from the merchant's isn't a real candidate no matter how
    # similar the wording is — this is what a human analyst would check
    # first, before even reading the description closely. Without this,
    # coincidental wording overlap with an unrelated nearby transaction
    # can outrank the (nonexistent) true match.
    plausible = [r for r in nearby if _amount_plausible(merchant.amount_minor, r.gross_amount_minor)]
    pool = plausible or nearby

    scored = sorted(
        ((normalize.jaccard(merchant.description, r.description), r) for r in pool),
        key=lambda pair: pair[0], reverse=True,
    )
    best_score, best = scored[0]

    if best_score >= policy.fuzzy_reference_jaccard_strong:
        return FuzzyMatchOutcome(
            best, "fuzzy_deterministic", False, None, None, "SAME",
            f"Description token overlap {best_score:.2f} exceeds the deterministic threshold "
            f"({policy.fuzzy_reference_jaccard_strong}) — resolved without a model call.",
        )

    if best_score < policy.fuzzy_reference_jaccard_floor:
        return FuzzyMatchOutcome(None, "none", False, None, None, None, "No candidate with plausible textual overlap found.")

    # Genuinely ambiguous zone: escalate to the model with structured,
    # bounded input — exactly the two descriptions, amounts, and dates.
    # A provider failure or timeout here must never crash a batch of
    # thousands of records over one bad call, and must never be silently
    # treated as a match — it is its own explicit outcome, always routed
    # to HUMAN_REVIEW (see policy.py), never RECONCILED.
    try:
        result = call_with_timeout(
            semantic_verifier.compare,
            MatchCandidateText(merchant.description, merchant.amount_minor, merchant.order_date),
            MatchCandidateText(best.description, best.gross_amount_minor, best.order_date),
            timeout_seconds=SEMANTIC_CALL_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 — deliberately broad: any AI failure degrades the same way
        return FuzzyMatchOutcome(
            None, "semantic_error", True, None, None, None,
            f"AI provider error during ambiguous match resolution ({type(exc).__name__}: {exc}) — "
            "routed to human review as a safe fallback, not auto-reconciled.",
        )

    candidate = best if result.verdict == "SAME" else None
    return FuzzyMatchOutcome(
        candidate, "semantic", True, result.confidence, result.backend, result.verdict,
        f"[{result.backend}] {result.verdict} (confidence {result.confidence:.2f}): {result.rationale}",
    )


def disambiguate_duplicates(
    merchant: MerchantRecord, candidates: list[RazorpaySettlementRecord], policy: PolicyConfig
) -> RazorpaySettlementRecord | None:
    """>1 candidate shares the same reference. If exactly one plausibly
    matches on amount, that's a real disambiguation, not a guess. If
    more than one (or none) do, this returns None — the caller routes
    that to HUMAN_REVIEW, because picking one would be a guess dressed
    up as a decision."""
    plausible = [c for c in candidates if normalize.amounts_match(merchant.amount_minor, c.gross_amount_minor, policy.amount_tolerance_minor)]
    return plausible[0] if len(plausible) == 1 else None
