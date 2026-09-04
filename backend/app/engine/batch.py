"""
Shared batch-processing core — used identically by evaluate.py (terminal,
against the held-out set) and the API's live batch endpoint (UI demo
mode, with SSE progress). One code path means the numbers the UI shows
and the numbers evaluate.py prints can never silently diverge.

Candidates are resolved here, once, from a single ReferenceIndex built
over the batch's full Razorpay-side population — not pre-attached to
each record. That's what makes the fuzzy/semantic fallback path
possible: a record with zero exact matches still needs visibility into
every other Razorpay record in the batch to find a plausible nearby one.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from datetime import datetime

from app.domain.models import (
    CheckResult, CheckStatus, ExceptionType, MatchClassification, PolicyConfig, ReconciliationOutcome,
    ReconciliationRecord, ReconciliationResult, RazorpaySettlementRecord, Severity,
)
from app.engine import matching, policy as policy_engine
from app.engine.semantic import SemanticVerifier, get_semantic_verifier


def process_batch(
    records: list[ReconciliationRecord],
    razorpay_records: list[RazorpaySettlementRecord],
    *,
    policy: Optional[PolicyConfig] = None,
    semantic_verifier: Optional[SemanticVerifier] = None,
    on_record: Optional[Callable[[int, int, ReconciliationRecord, ReconciliationResult], None]] = None,
    on_revision: Optional[Callable[[int, ReconciliationRecord, ReconciliationResult], None]] = None,
    as_of: Optional[datetime] = None,
) -> list[ReconciliationResult]:
    """Reconcile a batch.

    `as_of` is the observation point used to tell a settlement that is
    missing from one that is not due yet. When not supplied it is taken
    from the population's latest settlement date, which is the most recent
    moment the data can attest to; passing `None` with an empty population
    simply disables the distinction rather than guessing at it.
    """
    policy = policy or PolicyConfig()
    semantic_verifier = semantic_verifier or get_semantic_verifier()
    index = matching.ReferenceIndex(razorpay_records)
    if as_of is None and razorpay_records:
        as_of = max(r.settlement_date for r in razorpay_records)

    results: list[ReconciliationResult] = []
    total = len(records)
    for i, record in enumerate(records):
        started = time.perf_counter()
        try:
            candidates = index.exact_candidates(record.merchant)
            result = policy_engine.reconcile(
                record.merchant, candidates, index, policy, semantic_verifier, record.record_id,
                as_of=as_of,
            )
        except Exception as exc:  # noqa: BLE001 — one bad record must never take down a 1000+ record batch
            result = ReconciliationResult(
                record_id=record.record_id,
                outcome=ReconciliationOutcome.HUMAN_REVIEW,
                reason=f"Unexpected processing error ({type(exc).__name__}: {exc}) — routed to human review, not silently reconciled.",
                checks=[CheckResult(name="processing", status=CheckStatus.FAIL, detail=str(exc))],
                candidate_count=0,
                policy_threshold=policy.ai_confidence_threshold,
                latency_ms=(time.perf_counter() - started) * 1000,
            )
        results.append(result)
        if on_record:
            on_record(i, total, record, result)

    # One settlement, one merchant record. This can only be checked once
    # every record has been decided, so it is a second pass rather than
    # part of the loop; revised records are re-emitted so anything that
    # persisted the first decision corrects it.
    resolved, conflicts = resolve_claims(results)
    if conflicts and on_revision:
        for i, (before, after) in enumerate(zip(results, resolved)):
            if before.outcome is not after.outcome:
                on_revision(i, records[i], after)
    return resolved


# How strong the basis for a claim was. Used only to settle conflicts,
# and only when one claimant is strictly stronger than every other — a tie
# means the system cannot tell, and saying so is the correct answer.
_EVIDENCE_TIER: dict[MatchClassification, int] = {
    MatchClassification.EXACT_REFERENCE: 4,
    MatchClassification.DISAMBIGUATED_BY_AMOUNT: 3,
    MatchClassification.CORROBORATED: 2,
    MatchClassification.SEMANTIC_CONFIRMED: 1,
}


def resolve_claims(
    results: list[ReconciliationResult],
) -> tuple[list[ReconciliationResult], dict[str, list[str]]]:
    """Enforce one settlement, one merchant record.

    Each record is decided in isolation, which is right per record and
    blind across them: two orders can each match the same payment and each
    look perfectly reconciled. That is double-counted revenue, and no
    per-record check can see it.

    Conflicts are settled deterministically and only when the evidence
    actually settles them — a claim resting on an exact reference match
    beats one resting on a semantic verdict. When the strongest tier is
    tied, every claimant is demoted to HUMAN_REVIEW rather than picking by
    position in the batch. Deciding by order would be arbitrary, and
    arbitrary is indistinguishable from wrong when someone audits it later.

    Global assignment (bipartite matching) was considered and rejected: it
    would make one record's outcome depend on every other record in the
    batch, which is not explainable to the operator who has to act on it.

    Note this is about `payment_id`, not `settlement_id`. Many payments
    legitimately settle in one batch and therefore share a settlement_id;
    two merchant orders claiming one *payment* is always a conflict in
    this data model.
    """
    claims: dict[str, list[int]] = {}
    for i, result in enumerate(results):
        if result.matched_payment_id and result.outcome is not ReconciliationOutcome.EXCEPTION:
            claims.setdefault(result.matched_payment_id, []).append(i)

    conflicts = {pid: idxs for pid, idxs in claims.items() if len(idxs) > 1}
    if not conflicts:
        return results, {}

    updated = list(results)
    reported: dict[str, list[str]] = {}

    for payment_id, idxs in conflicts.items():
        reported[payment_id] = [results[i].record_id for i in idxs]
        tiers = [_EVIDENCE_TIER.get(results[i].classification, 0) for i in idxs]
        best = max(tiers)
        winners = [i for i, tier in zip(idxs, tiers) if tier == best]
        keep = winners[0] if len(winners) == 1 else None

        for i in idxs:
            if i == keep:
                continue
            original = updated[i]
            others = [results[j].record_id for j in idxs if j != i]
            updated[i] = original.model_copy(update={
                "outcome": ReconciliationOutcome.HUMAN_REVIEW,
                "exception_type": ExceptionType.DUPLICATE_CLAIM,
                "severity": Severity.HIGH,
                "reason": (
                    f"Settlement {payment_id} was also matched to {', '.join(others)}. "
                    + ("A stronger claim exists, so this one is not accepted automatically."
                       if keep is not None else
                       "No claim is better supported than the others, so none is accepted automatically.")
                ),
                "explanation": (
                    f"This settlement was claimed by {len(idxs)} different orders. Reconciling more than "
                    "one of them would count the same money twice, so the claim is held for review."
                ),
                "recommended_action": (
                    f"Confirm which order settlement {payment_id} belongs to, or whether the provider "
                    "aggregated several payments into it."
                ),
                "checks": list(original.checks) + [CheckResult(
                    name="claim_uniqueness",
                    status=CheckStatus.WARN,
                    expected="1 merchant record per settlement",
                    observed=f"{len(idxs)} records claim {payment_id}",
                    detail=f"Competing claims: {', '.join(reported[payment_id])}.",
                )],
            })

    return updated, reported


def detect_duplicate_claims(results: list[ReconciliationResult]) -> dict[str, list[str]]:
    """Settlement records claimed by more than one merchant record.

    Every decision in a batch is made for one merchant record in
    isolation, which is correct per record and blind across them: two
    different orders can each match the same payment, and each looks
    perfectly reconciled on its own. That is double-counted revenue, and
    it is invisible to any per-record check — it only exists at the batch
    level, so it is detected here rather than pretended away.

    Returns {payment_id: [record_id, ...]} for every settlement claimed
    more than once. An empty dict means the batch's matching is
    one-to-one.
    """
    claims: dict[str, list[str]] = {}
    for result in results:
        if result.matched_payment_id:
            claims.setdefault(result.matched_payment_id, []).append(result.record_id)
    return {payment_id: ids for payment_id, ids in claims.items() if len(ids) > 1}
