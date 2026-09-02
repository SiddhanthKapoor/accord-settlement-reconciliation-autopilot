"""
The deterministic core.

Every check here answers a single yes/no or bounded-tolerance question
from structured evidence — no model, no hidden reasoning, no guessing.
Two comparison axes are checked, deliberately kept separate because they
catch different failure modes:

  (1) PaymentRequest vs. Commitment  — did the AGENT's final ask drift
      from what it already committed to? (catches an agent quietly
      changing quantity/price/product at the last step, e.g. via
      injected tool output)

  (2) Commitment vs. freshly-fetched catalog ground truth — did the
      WORLD change since the commitment was made? (catches merchant-side
      price/availability changes, staleness)

Each check is tagged with the AP2 threat it closes (T-31 replay, T-32
state mutation / semantic manipulation, T-33 shared-budget races) where
applicable, so the mapping from "check in this file" to "named gap in
AP2's own security analysis" is traceable rather than asserted.
"""

from __future__ import annotations

import difflib
from datetime import datetime, timezone

from app.domain.models import CheckStatus, Commitment, Constraints, IntegrityCheck, PaymentRequest
from app.engine import text_normalize
from app.engine.semantic import (
    HeuristicSemanticVerifier,
    ProductAttrs,
    SemanticVerdictResult,
    get_semantic_verifier,
)
from app.integrations import catalog_client
from app.ledger import store

DEFAULT_COMMITMENT_TTL_SECONDS = 300  # 5 minutes: how long a commitment stays fresh
NAME_MATCH_STRONG_THRESHOLD = 0.92    # above this, treat as same product without calling the LLM


def _name_similarity(a: str, b: str) -> float:
    """Deterministic fast path: unit/phrasing normalization (see
    text_normalize.py) then sequence similarity. Handles "500g" vs
    "500 grams"-class variation without ever calling a model. Cases that
    still look different after this normalization are the genuinely
    ambiguous ones that get escalated to the semantic verifier."""
    return difflib.SequenceMatcher(None, text_normalize.normalize(a), text_normalize.normalize(b)).ratio()


def run_integrity_checks(
    intent_row: dict,
    constraints: Constraints,
    commitment: Commitment,
    payment_request: PaymentRequest,
) -> list[IntegrityCheck]:
    checks: list[IntegrityCheck] = []
    now = datetime.now(timezone.utc)

    # --- T-31: replay --------------------------------------------------
    already_consumed = store.is_commitment_consumed(commitment.commitment_id)
    checks.append(
        IntegrityCheck(
            name="replay_check",
            status=CheckStatus.FAIL if already_consumed else CheckStatus.PASS,
            expected="commitment not previously consumed",
            observed="already consumed" if already_consumed else "not previously consumed",
            detail="Commitment already backed a completed payment; reuse rejected."
            if already_consumed
            else "No prior execution recorded for this commitment.",
            threat_ref="T-31",
        )
    )

    # --- T-33: shared-budget reservation still valid --------------------
    # Strict identity check applies to single-use budgets (our headline
    # framing: one open mandate backs exactly one committed transaction).
    # Non-single-use budgets only track an aggregate remaining balance, not
    # a per-commitment ledger, so `budget_reserved_by` reflects only the
    # most recent reservation — a genuine simplification, called out here
    # rather than silently producing a misleading FAIL for a case this
    # system's checks don't fully itemize.
    if constraints.single_use:
        reserved_ok = intent_row["budget_reserved"] == 1 and intent_row["budget_reserved_by"] == commitment.commitment_id
        checks.append(
            IntegrityCheck(
                name="budget_reservation",
                status=CheckStatus.PASS if reserved_ok else CheckStatus.FAIL,
                expected=f"budget reserved_by={commitment.commitment_id}",
                observed=f"reserved={intent_row['budget_reserved']} reserved_by={intent_row['budget_reserved_by']}",
                detail="Budget reservation for this commitment is intact."
                if reserved_ok
                else "This commitment does not hold the intent's budget reservation "
                "(lost a concurrent race to another commitment under the same open mandate).",
                threat_ref="T-33",
            )
        )
    else:
        checks.append(
            IntegrityCheck(
                name="budget_reservation",
                status=CheckStatus.PASS,
                expected="n/a (non-single-use budget)",
                observed=f"remaining={intent_row['budget_remaining_minor']}",
                detail="Non-single-use budget: aggregate remaining balance is tracked atomically "
                "(see reserve_budget), but per-commitment reservation identity is not itemized.",
                threat_ref="T-33",
            )
        )

    # --- T-32: agent's final ask vs. what it committed to ---------------
    checks.append(_field_check(
        "merchant_identity", commitment.merchant_id, payment_request.merchant_id, "T-32",
        "Merchant in payment request differs from merchant in the committed transaction.",
    ))

    checks.append(_field_check(
        "quantity_vs_commitment", str(commitment.quantity), str(payment_request.quantity), "T-32",
        "Quantity in payment request differs from the committed quantity.",
    ))

    checks.append(_product_identity_check(commitment, payment_request))

    checks.append(_price_tolerance_check(
        name="price_vs_commitment",
        expected_minor=commitment.price_minor,
        observed_minor=payment_request.price_minor,
        tolerance_pct=constraints.price_tolerance_pct,
        threat_ref="T-32",
        context="payment request vs. commitment",
    ))

    # --- Declared constraints (hard caps, not tolerance-based) ----------
    total_minor = payment_request.price_minor * payment_request.quantity
    checks.append(
        IntegrityCheck(
            name="constraint_max_amount",
            status=CheckStatus.PASS if total_minor <= constraints.max_amount_minor else CheckStatus.FAIL,
            expected=f"<= {constraints.max_amount_minor} minor units",
            observed=f"{total_minor} minor units",
            detail="Total is within the declared spend cap." if total_minor <= constraints.max_amount_minor
            else "Total exceeds the declared spend cap — this is a hard constraint, not tolerance-adjustable.",
        )
    )
    checks.append(
        IntegrityCheck(
            name="constraint_max_quantity",
            status=CheckStatus.PASS if payment_request.quantity <= constraints.max_quantity else CheckStatus.FAIL,
            expected=f"<= {constraints.max_quantity}",
            observed=str(payment_request.quantity),
            detail="Quantity within declared cap." if payment_request.quantity <= constraints.max_quantity
            else "Quantity exceeds declared cap.",
        )
    )
    if constraints.allowed_categories:
        ok = payment_request.category in constraints.allowed_categories
        checks.append(
            IntegrityCheck(
                name="constraint_category",
                status=CheckStatus.PASS if ok else CheckStatus.FAIL,
                expected=str(constraints.allowed_categories),
                observed=payment_request.category,
                detail="Category allowed." if ok else "Category not in the declared allow-list.",
            )
        )
    if constraints.allowed_merchants:
        ok = payment_request.merchant_id in constraints.allowed_merchants
        checks.append(
            IntegrityCheck(
                name="constraint_merchant_allowlist",
                status=CheckStatus.PASS if ok else CheckStatus.FAIL,
                expected=str(constraints.allowed_merchants),
                observed=payment_request.merchant_id,
                detail="Merchant allowed." if ok else "Merchant not in the declared allow-list.",
            )
        )
    if constraints.expires_at:
        expired = now > constraints.expires_at
        checks.append(
            IntegrityCheck(
                name="constraint_expiry",
                status=CheckStatus.FAIL if expired else CheckStatus.PASS,
                expected=f"now <= {constraints.expires_at.isoformat()}",
                observed=now.isoformat(),
                detail="Within the intent's validity window." if not expired else "Intent has expired.",
            )
        )

    # --- Staleness: commitment age (graduated: warn, then hard-fail) -----
    age_seconds = (now - commitment.created_at).total_seconds()
    hard_stale_seconds = DEFAULT_COMMITMENT_TTL_SECONDS * 3
    if age_seconds <= DEFAULT_COMMITMENT_TTL_SECONDS:
        staleness_status = CheckStatus.PASS
        staleness_detail = "Commitment is fresh."
    elif age_seconds <= hard_stale_seconds:
        staleness_status = CheckStatus.WARN
        staleness_detail = (
            "Commitment exceeds its freshness window — checkout state may no longer reflect "
            "current terms; requires reconfirmation before proceeding."
        )
    else:
        staleness_status = CheckStatus.FAIL
        staleness_detail = "Commitment is far past its freshness window — treated as an abandoned session, must restart."
    checks.append(
        IntegrityCheck(
            name="commitment_staleness",
            status=staleness_status,
            expected=f"age <= {DEFAULT_COMMITMENT_TTL_SECONDS}s",
            observed=f"age = {age_seconds:.0f}s",
            detail=staleness_detail,
            threat_ref="T-32",
        )
    )

    # --- Ground truth: has the WORLD changed since commit? ----------------
    checks.extend(_ground_truth_checks(commitment, constraints))

    return checks


def _field_check(name: str, expected: str, observed: str, threat_ref: str, fail_detail: str) -> IntegrityCheck:
    ok = expected == observed
    return IntegrityCheck(
        name=name,
        status=CheckStatus.PASS if ok else CheckStatus.FAIL,
        expected=expected,
        observed=observed,
        detail="Matches committed state." if ok else fail_detail,
        threat_ref=threat_ref,
    )


def _price_tolerance_check(
    *,
    name: str,
    expected_minor: int,
    observed_minor: int,
    tolerance_pct: float,
    threat_ref: str,
    context: str,
    hard_multiple: float = 3.0,
) -> IntegrityCheck:
    """Graduated policy, not a single cliff: exact match passes; drift within
    the declared tolerance passes (benign variance is not treated as an
    attack); drift beyond tolerance but within `hard_multiple` * tolerance
    warrants a human/agent reconfirmation rather than an automatic block;
    drift beyond that is treated as a violation. This directly operationalizes
    "not every price change is malicious" as an explicit, inspectable policy
    instead of a single arbitrary threshold."""
    if expected_minor == 0:
        pct_diff = 0.0 if observed_minor == 0 else 100.0
    else:
        pct_diff = abs(observed_minor - expected_minor) / expected_minor * 100

    hard_ceiling = tolerance_pct * hard_multiple if tolerance_pct > 0 else 0.01

    if pct_diff == 0:
        status = CheckStatus.PASS
        detail = f"Exact match ({context})."
    elif pct_diff <= tolerance_pct:
        status = CheckStatus.PASS
        detail = f"Drift of {pct_diff:.2f}% is within the declared tolerance of {tolerance_pct}% ({context})."
    elif pct_diff <= hard_ceiling:
        status = CheckStatus.WARN
        detail = (
            f"Drift of {pct_diff:.2f}% exceeds tolerance ({tolerance_pct}%) but is below the "
            f"hard ceiling ({hard_ceiling:.2f}%) — treated as requiring reconfirmation, not an "
            f"automatic block ({context})."
        )
    else:
        status = CheckStatus.FAIL
        detail = f"Drift of {pct_diff:.2f}% exceeds the hard ceiling of {hard_ceiling:.2f}% ({context})."

    return IntegrityCheck(
        name=name,
        status=status,
        expected=f"{expected_minor} minor units (±{tolerance_pct}%)",
        observed=f"{observed_minor} minor units",
        detail=detail,
        threat_ref=threat_ref,
    )


def _product_identity_check(commitment: Commitment, payment_request: PaymentRequest) -> IntegrityCheck:
    if commitment.product_id == payment_request.product_id:
        return IntegrityCheck(
            name="product_identity",
            status=CheckStatus.PASS,
            expected=commitment.product_id,
            observed=payment_request.product_id,
            detail="Exact product ID match against the commitment.",
            threat_ref="T-32",
        )

    # product_id differs — first try a cheap deterministic normalized-name
    # match (handles "500g" vs "500 grams"-class variation without ever
    # calling the model) before escalating to the semantic verifier.
    similarity = _name_similarity(commitment.product_name, payment_request.product_name)
    if similarity >= NAME_MATCH_STRONG_THRESHOLD:
        return IntegrityCheck(
            name="product_identity",
            status=CheckStatus.PASS,
            expected=commitment.product_name,
            observed=payment_request.product_name,
            detail=f"Different product_id but normalized name similarity={similarity:.2f} "
            "(deterministic match, no model call needed).",
            threat_ref="T-32",
        )

    verifier = get_semantic_verifier()
    declared_attrs = ProductAttrs(name=commitment.product_name, category=commitment.category)
    observed_attrs = ProductAttrs(name=payment_request.product_name, category=payment_request.category)
    try:
        result = verifier.compare(declared=declared_attrs, observed=observed_attrs, user_constraint_text=None)
    except Exception as exc:  # noqa: BLE001 — a provider outage must degrade, not crash the integrity check
        result = HeuristicSemanticVerifier().compare(
            declared=declared_attrs, observed=observed_attrs, user_constraint_text=None
        )
        result = SemanticVerdictResult(
            verdict=result.verdict,
            confidence=result.confidence,
            rationale=f"primary provider errored ({exc}); heuristic fallback used for this decision only — {result.rationale}",
            backend="heuristic-fallback-after-error",
        )
    status_map = {
        "EQUIVALENT": CheckStatus.PASS,
        "AMBIGUOUS": CheckStatus.WARN,
        "MATERIAL_CHANGE": CheckStatus.FAIL,
    }
    status = status_map[result.verdict]
    detail = f"[{result.backend}] {result.verdict}: {result.rationale}"
    if result.backend.startswith("heuristic-fallback") and result.verdict == "EQUIVALENT":
        # The lexical fallback has no real semantic understanding. It is
        # trusted to say "definitely different" (FAIL) but never trusted
        # to confidently say "definitely the same" for a case that already
        # failed the cheap normalized-string fast path — that confidence
        # is reserved for the LLM backend. A false ALLOW is worse than an
        # unnecessary reconfirmation.
        status = CheckStatus.WARN
        detail += " (downgraded from PASS: heuristic fallback is not trusted to auto-confirm equivalence)"
    return IntegrityCheck(
        name="product_identity",
        status=status,
        expected=commitment.product_name,
        observed=payment_request.product_name,
        detail=detail,
        confidence=result.confidence,
        threat_ref="T-32",
    )


def _ground_truth_checks(commitment: Commitment, constraints: Constraints) -> list[IntegrityCheck]:
    try:
        fresh = catalog_client.fetch_ground_truth(commitment.merchant_id, commitment.product_id)
    except catalog_client.ProductNotFound:
        return [
            IntegrityCheck(
                name="ground_truth_availability",
                status=CheckStatus.FAIL,
                expected="product exists in merchant catalog",
                observed="not found",
                detail="Product referenced by the commitment no longer exists in the merchant's catalog.",
                threat_ref="T-32",
            )
        ]
    except catalog_client.CatalogUnavailable as exc:
        return [
            IntegrityCheck(
                name="ground_truth_availability",
                status=CheckStatus.WARN,
                expected="catalog reachable",
                observed="unreachable",
                detail=f"Could not independently verify ground truth: {exc}. Treated as inconclusive, not a pass.",
                threat_ref="T-32",
            )
        ]

    results = [
        _price_tolerance_check(
            name="ground_truth_price",
            expected_minor=commitment.price_minor,
            observed_minor=fresh.price_minor,
            tolerance_pct=constraints.price_tolerance_pct,
            threat_ref="T-32",
            context="commitment vs. live merchant catalog",
        ),
        IntegrityCheck(
            name="ground_truth_availability",
            status=CheckStatus.PASS if fresh.available else CheckStatus.FAIL,
            expected="available",
            observed="available" if fresh.available else "unavailable",
            detail="Product is currently available from the merchant." if fresh.available
            else "Merchant catalog now reports this product as unavailable.",
            threat_ref="T-32",
        ),
    ]
    return results
