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

from app.domain.models import (
    CheckResult, CheckStatus, PolicyConfig, ReconciliationOutcome, ReconciliationRecord,
    ReconciliationResult, RazorpaySettlementRecord,
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
) -> list[ReconciliationResult]:
    policy = policy or PolicyConfig()
    semantic_verifier = semantic_verifier or get_semantic_verifier()
    index = matching.ReferenceIndex(razorpay_records)

    results: list[ReconciliationResult] = []
    total = len(records)
    for i, record in enumerate(records):
        started = time.perf_counter()
        try:
            candidates = index.exact_candidates(record.merchant)
            result = policy_engine.reconcile(
                record.merchant, candidates, index, policy, semantic_verifier, record.record_id
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
    return results


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
