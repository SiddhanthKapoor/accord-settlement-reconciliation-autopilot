"""
Candidate resolution: given a merchant record, find the Razorpay
record(s) it should be checked against. Tiers, cheapest first:

1. Exact normalized-reference lookup (O(1) via a prebuilt index).
2. Deterministic corroborated match — a shortlist ranked on real
   evidence (amount agreement, shared reference core, date proximity,
   IDF-weighted description similarity), resolved without a model call
   when two independent signals agree.
3. Semantic match via the model (semantic.py) — only for the residual
   shortlist entries that tier 2 could not settle.

Anything that falls through all three is a genuine absence: no
settlement record exists for this merchant record, full stop — that's
not ambiguous, it's the MISSING_SETTLEMENT case, handled as a
deterministic exception in policy.py.

On ranking, and why it is not plain text similarity: the first version
of this module ranked candidates on unweighted description Jaccard
alone. Every description in a settlement population is built from the
same handful of template words, so shared boilerplate reliably outranked
the genuine counterpart — the true match sat at a median rank of 35 out
of ~600 candidates, and the model was then asked to judge a pair that
never contained the right record. It answered DIFFERENT, correctly, and
the system recorded that as the model failing. The ranking below is the
fix; the full write-up is in docs/ENGINEERING_FAILURES_AND_FIXES.md.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from datetime import datetime

from app.domain.models import MerchantRecord, PolicyConfig, RazorpaySettlementRecord
from app.engine import normalize
from app.engine.semantic import CandidateComparison, RecordSide, SemanticVerifier

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


@dataclass(frozen=True)
class CandidateSignals:
    """Every signal the ranker used, kept alongside the score so a
    decision can be explained after the fact instead of being a bare
    number in a log."""

    amount_exact: bool
    amount_delta_minor: int
    amount_ratio: float
    shared_reference_core: bool
    days_apart: int
    text_similarity: float
    score: float

    def corroborating_count(self) -> int:
        """Independent signals that agree this is the same payment.
        Deliberately excludes text similarity: description wording is the
        weakest and most easily coincidental of the four."""
        return sum((self.amount_exact, self.shared_reference_core, self.days_apart <= 3))


class ReferenceIndex:
    """Built once per batch.

    Holds the exact-reference index, a day-bucketed index for windowed
    candidate search, and description token statistics for IDF weighting.

    The day bucketing matters at scale: the original implementation
    scanned the entire settlement population per record, making a batch
    O(records x population). At 50k records against a 48k population
    that is billions of comparisons; the throughput suite would not have
    completed. Bucketing makes the search proportional to the window,
    not the population.
    """

    def __init__(self, razorpay_records: list[RazorpaySettlementRecord]) -> None:
        self.all_records = razorpay_records
        self.by_reference: dict[str, list[RazorpaySettlementRecord]] = defaultdict(list)
        self.by_day: dict[int, list[RazorpaySettlementRecord]] = defaultdict(list)
        self.by_reference_core: dict[str, list[RazorpaySettlementRecord]] = defaultdict(list)
        self.by_amount: dict[int, list[RazorpaySettlementRecord]] = defaultdict(list)

        descriptions = []
        for r in razorpay_records:
            self.by_reference[normalize.normalize_reference(r.order_reference)].append(r)
            self.by_day[self._day_key(r.order_date)].append(r)
            self.by_amount[r.gross_amount_minor].append(r)
            for core in normalize.reference_cores(r.order_reference, r.description):
                self.by_reference_core[core].append(r)
            descriptions.append(r.description)

        self.total_documents = len(descriptions)
        self.document_frequency: Counter = normalize.token_document_frequencies(descriptions)

    @staticmethod
    def _day_key(dt: datetime) -> int:
        return dt.toordinal()

    def exact_candidates(self, merchant: MerchantRecord) -> list[RazorpaySettlementRecord]:
        key = normalize.normalize_reference(merchant.reference_id)
        if not key:
            return []
        return self.by_reference.get(key, [])

    def nearby_by_date(self, merchant: MerchantRecord, window_days: int, limit: int | None = None) -> list[RazorpaySettlementRecord]:
        """Records inside the date window, nearest day first.

        `limit` bounds the work for the one case that would otherwise
        stay quadratic: a merchant record with no amount and no
        identifier agreement anywhere in the population — i.e. a genuinely
        missing settlement. Scanning every record in a three-week window
        of a 50k-record population to confirm an absence is wasted work,
        so the scan stops at the nearest `limit` candidates. A real
        counterpart agrees on gross amount by definition and is found
        through the amount index regardless of this bound.
        """
        center = self._day_key(merchant.order_date)
        out: list[RazorpaySettlementRecord] = []
        for offset in range(window_days + 1):
            for day in ({center} if offset == 0 else {center - offset, center + offset}):
                bucket = self.by_day.get(day)
                if bucket:
                    out.extend(bucket)
                    if limit is not None and len(out) >= limit:
                        return out[:limit]
        return out

    def amount_candidates(self, merchant: MerchantRecord) -> list[RazorpaySettlementRecord]:
        """Exact gross-amount agreement, in O(1). A genuine counterpart
        agrees on gross amount by construction — fees and tax come off
        the net, not the gross — so this surfaces the true match even
        when the reference and the wording share nothing at all."""
        return self.by_amount.get(merchant.amount_minor, [])

    def core_candidates(self, merchant: MerchantRecord) -> list[RazorpaySettlementRecord]:
        """Records sharing a long digit run with the merchant's reference
        or description. Cheap, and it surfaces the true counterpart even
        when the wording diverges completely."""
        seen: dict[str, RazorpaySettlementRecord] = {}
        for core in normalize.reference_cores(merchant.reference_id, merchant.description):
            for r in self.by_reference_core.get(core, ()):
                seen[r.payment_id] = r
        return list(seen.values())

    def text_similarity(self, a: str, b: str) -> float:
        return normalize.weighted_jaccard(a, b, self.document_frequency, self.total_documents)


@dataclass
class FuzzyMatchOutcome:
    candidate: RazorpaySettlementRecord | None
    method: str  # "fuzzy_deterministic" | "semantic" | "semantic_error" | "none"
    ai_invoked: bool
    ai_confidence: float | None
    ai_backend: str | None
    verdict: str | None  # SAME / DIFFERENT / AMBIGUOUS / None
    detail: str
    ai_calls: int = 0


def _amount_plausible(merchant_amount: int, candidate_amount: int) -> bool:
    """Generous on purpose (0.5x-2x) — this only filters out wildly
    implausible candidates before ranking; the real amount check still
    happens exactly in policy.py's financial checks."""
    if merchant_amount <= 0:
        return True
    ratio = candidate_amount / merchant_amount
    return 0.5 <= ratio <= 2.0


def score_candidate(
    merchant: MerchantRecord,
    candidate: RazorpaySettlementRecord,
    index: ReferenceIndex,
    policy: PolicyConfig,
) -> CandidateSignals:
    """Composite evidence score in roughly [0, 1].

    The weights are ordered by how hard each signal is to produce by
    coincidence: an exact amount agreement and a shared identifier core
    are strong, date proximity is weak-but-real, and description wording
    is the tiebreaker rather than the driver.
    """
    amount_delta = candidate.gross_amount_minor - merchant.amount_minor
    amount_exact = abs(amount_delta) <= policy.amount_tolerance_minor
    ratio = (candidate.gross_amount_minor / merchant.amount_minor) if merchant.amount_minor else 0.0

    merchant_cores = normalize.reference_cores(merchant.reference_id, merchant.description)
    candidate_cores = normalize.reference_cores(candidate.order_reference, candidate.description)
    shared_core = bool(merchant_cores & candidate_cores)

    days_apart = normalize.days_between(merchant.order_date, candidate.order_date)
    text = index.text_similarity(merchant.description, candidate.description)

    # Near-miss amounts still carry information (a 0.2% difference is a
    # far better sign than a 60% one), so this decays rather than
    # switching off.
    if amount_exact:
        amount_component = 1.0
    elif merchant.amount_minor > 0:
        relative = abs(amount_delta) / merchant.amount_minor
        amount_component = max(0.0, 1.0 - min(relative, 1.0) * 2.0)
    else:
        amount_component = 0.0

    date_component = max(0.0, 1.0 - (days_apart / max(policy.candidate_search_window_days, 1)))

    score = (
        0.45 * amount_component
        + 0.25 * (1.0 if shared_core else 0.0)
        + 0.10 * date_component
        + 0.20 * text
    )

    return CandidateSignals(
        amount_exact=amount_exact,
        amount_delta_minor=amount_delta,
        amount_ratio=ratio,
        shared_reference_core=shared_core,
        days_apart=days_apart,
        text_similarity=text,
        score=score,
    )


def build_shortlist(
    merchant: MerchantRecord,
    index: ReferenceIndex,
    policy: PolicyConfig,
) -> list[tuple[RazorpaySettlementRecord, CandidateSignals]]:
    """Candidates worth considering at all, best evidence first.

    Pulls from two cheap sources — the date window and the shared
    reference core index — because a genuine counterpart can be outside
    the date window (a late-booked settlement) or inside it but textually
    unrecognisable.
    """
    # Cheap, high-precision sources first: a genuine counterpart almost
    # always agrees on gross amount or carries a shared identifier core,
    # and both are O(1) lookups. The date-window scan is the fallback for
    # everything else and is bounded, so batch cost stays close to linear
    # in the number of records rather than records x population.
    pool: dict[str, RazorpaySettlementRecord] = {r.payment_id: r for r in index.amount_candidates(merchant)}
    for r in index.core_candidates(merchant):
        pool.setdefault(r.payment_id, r)
    for r in index.nearby_by_date(merchant, policy.candidate_search_window_days,
                                  limit=policy.max_window_scan_candidates):
        pool.setdefault(r.payment_id, r)

    if not pool:
        return []

    plausible = [r for r in pool.values() if _amount_plausible(merchant.amount_minor, r.gross_amount_minor)]
    considered = plausible or list(pool.values())

    scored = [(r, score_candidate(merchant, r, index, policy)) for r in considered]
    scored.sort(key=lambda pair: pair[1].score, reverse=True)
    return scored[: policy.candidate_shortlist_size]


def resolve_fuzzy_or_semantic(
    merchant: MerchantRecord,
    index: ReferenceIndex,
    policy: PolicyConfig,
    semantic_verifier: SemanticVerifier,
) -> FuzzyMatchOutcome:
    """Called only when exact reference lookup found nothing."""
    shortlist = build_shortlist(merchant, index, policy)
    if not shortlist:
        return FuzzyMatchOutcome(None, "none", False, None, None, None,
                                 "No Razorpay record found within the search window.")

    best, best_signals = shortlist[0]

    # Tier 2: deterministic resolution on corroborating evidence. Two
    # independent signals agreeing (say, an exact amount and a shared
    # identifier core) is a stronger basis than any wording similarity,
    # and it costs nothing.
    if (
        best_signals.corroborating_count() >= 2
        and best_signals.score >= policy.deterministic_match_score
        and best_signals.text_similarity >= policy.deterministic_min_text_similarity
    ):
        runner_up = shortlist[1][1].score if len(shortlist) > 1 else 0.0
        if best_signals.score - runner_up >= policy.deterministic_match_margin:
            return FuzzyMatchOutcome(
                best, "fuzzy_deterministic", False, None, None, "SAME",
                f"Resolved deterministically: amount {'matches exactly' if best_signals.amount_exact else 'is close'}, "
                f"{'shared reference core, ' if best_signals.shared_reference_core else ''}"
                f"{best_signals.days_apart}d apart, evidence score {best_signals.score:.2f} "
                f"(runner-up {runner_up:.2f}) — no model call needed.",
            )

    if not policy.enable_semantic_matching:
        return FuzzyMatchOutcome(None, "none", False, None, None, None,
                                 "Deterministic matching could not resolve a candidate and the semantic "
                                 "verifier is disabled by policy.")

    # Tier 3: genuinely ambiguous. Walk the shortlist in evidence order
    # and ask the model about each pair until one is confirmed. Bounded
    # by policy.max_semantic_calls_per_record so an unresolvable record
    # can never fan out into unbounded API cost.
    ai_calls = 0
    last_verdict: str | None = None
    last_confidence: float | None = None
    last_backend: str | None = None
    last_detail = ""

    for candidate, signals in shortlist[: policy.max_semantic_calls_per_record]:
        if signals.text_similarity < policy.fuzzy_reference_jaccard_floor and not signals.amount_exact \
                and not signals.shared_reference_core:
            # Nothing about this pair justifies spending a model call.
            continue

        comparison = _build_comparison(merchant, candidate, signals)
        try:
            result = call_with_timeout(
                semantic_verifier.compare, comparison,
                timeout_seconds=SEMANTIC_CALL_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 — deliberately broad: any AI failure degrades the same way
            return FuzzyMatchOutcome(
                None, "semantic_error", True, None, None, None,
                f"AI provider error during ambiguous match resolution ({type(exc).__name__}: {exc}) — "
                "routed to human review as a safe fallback, not auto-reconciled.",
                ai_calls=ai_calls + 1,
            )

        ai_calls += 1
        last_verdict, last_confidence, last_backend = result.verdict, result.confidence, result.backend
        last_detail = f"[{result.backend}] {result.verdict} (confidence {result.confidence:.2f}): {result.rationale}"

        if result.verdict == "SAME":
            return FuzzyMatchOutcome(candidate, "semantic", True, result.confidence, result.backend,
                                     result.verdict, last_detail, ai_calls=ai_calls)
        if result.verdict == "AMBIGUOUS":
            # Stop here rather than shopping the shortlist for a yes.
            return FuzzyMatchOutcome(None, "semantic", True, result.confidence, result.backend,
                                     result.verdict, last_detail, ai_calls=ai_calls)

    if ai_calls == 0:
        return FuzzyMatchOutcome(None, "none", False, None, None, None,
                                 "No candidate with plausible corroborating evidence found.")

    return FuzzyMatchOutcome(None, "semantic", True, last_confidence, last_backend, last_verdict or "DIFFERENT",
                             last_detail or "All shortlisted candidates were rejected by the semantic verifier.",
                             ai_calls=ai_calls)


def _build_comparison(
    merchant: MerchantRecord, candidate: RazorpaySettlementRecord, signals: CandidateSignals
) -> CandidateComparison:
    return CandidateComparison(
        merchant=RecordSide(
            reference=merchant.reference_id or "",
            description=merchant.description,
            amount_minor=merchant.amount_minor,
            date=merchant.order_date,
        ),
        candidate=RecordSide(
            reference=candidate.order_reference,
            description=candidate.description,
            amount_minor=candidate.gross_amount_minor,
            date=candidate.order_date,
        ),
        amount_exact_match=signals.amount_exact,
        amount_delta_minor=signals.amount_delta_minor,
        days_apart=signals.days_apart,
        shared_reference_core=signals.shared_reference_core,
        text_similarity=round(signals.text_similarity, 3),
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
