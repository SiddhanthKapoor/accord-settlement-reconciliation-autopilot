"""
The one place in this system where an LLM is used — narrowly, on purpose,
same principle as the rest of this codebase's history: deterministic
matching (exact reference normalization, then token-overlap fuzzy
matching in matching.py) resolves the large majority of cases with no
model call at all. This module is only reached when a merchant record's
reference doesn't match anything deterministically, but there is at
least one Razorpay record nearby (within the policy's date window) whose
free-text description has *some* plausible textual overlap — not zero,
not enough to call it a confident match either.

Hard constraints, because they're part of the product's integrity
guarantee, not just this module's docstring:
- It never sees the merchant's or Razorpay's full record — only the two
  free-text descriptions, their amounts, and their dates. No hidden
  reasoning, nothing it wasn't structurally given.
- It never reconciles a record by itself. It returns a verdict + a
  bounded confidence; policy.py is what decides RECONCILED / EXCEPTION /
  HUMAN_REVIEW, and a low-confidence match can never become RECONCILED
  regardless of what the model says — see PolicyConfig.ai_confidence_threshold
  and policy.py's enforcement of it.
- Its accuracy is measured on a held-out labeled set exactly like the
  rest of this system's decisions (see evaluate.py) — not asserted.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel

from app.engine import normalize
from app.engine.providers import (
    FallbackChain,
    GeminiProvider,
    GroqProvider,
    ProviderError,
    ProviderErrorKind,
    build_chain,
    retry_delay_seconds,
)

Verdict = Literal["SAME", "DIFFERENT", "AMBIGUOUS"]


@dataclass
class RecordSide:
    reference: str
    description: str
    amount_minor: int
    date: datetime


@dataclass
class CandidateComparison:
    """Everything the model is given, and nothing else.

    Deliberately includes the deterministic signals the engine already
    computed. Recomputing "do these amounts agree" inside a language
    model would be both wasteful and less reliable than the exact integer
    comparison that produced `amount_exact_match` — so the model is told
    the answer and asked to spend its judgment on the part that actually
    needs interpretation: whether two pieces of free text and two
    differently-formatted references denote the same payment.
    """

    merchant: RecordSide
    candidate: RecordSide
    amount_exact_match: bool
    amount_delta_minor: int
    days_apart: int
    shared_reference_core: bool
    text_similarity: float


@dataclass
class SemanticVerdictResult:
    verdict: Verdict
    confidence: float
    rationale: str
    backend: str


class SemanticVerifier(Protocol):
    def compare(self, comparison: CandidateComparison) -> SemanticVerdictResult: ...


_PROMPT_PREAMBLE = """You are a narrow reconciliation-matching classifier inside a finance \
operations system. Deterministic matching has already narrowed the field; you are being asked \
about one merchant order record and one Razorpay settlement record that it could not settle \
on its own.

You are given both sides (reference, description, amount in paise, date) and the deterministic \
signals already computed for the pair. Trust those signals — they are exact comparisons, not \
estimates. Do not re-derive them.

Decide one thing only: do these two records describe the SAME underlying payment, are they \
clearly DIFFERENT payments, or is it genuinely AMBIGUOUS for a careful human reviewer?

How to weigh the evidence:
- Descriptions routinely differ in wording for the same payment: abbreviations, aliases, a \
trading name instead of a legal entity, reordered words, gateway routing noise, or an \
initialised customer name. Different wording is NOT evidence of a different payment.
- An exact amount match plus close dates is strong corroboration of SAME.
- A shared reference core is corroboration, but identifiers scoped differently on each side \
(an invoice counter versus an order number) can collide. Weigh it with the rest.
- Genuinely different payments are the ones where the underlying subject differs: a different \
customer, a different product or service, or a different order in a sequence — especially when \
amounts and dates are close enough that only the subject distinguishes them.
- Prefer AMBIGUOUS over a confident wrong answer in either direction.

Set confidence to your actual certainty in the verdict. Keep the reason to one short sentence.
"""


class _VerdictSchema(BaseModel):
    relationship: Verdict
    confidence: float
    reason: str


#: Kept as a module-level name because the RetryInfo parsing it pins is
#: the part that silently rots when the SDK's error shape changes. The
#: implementation (and the cap that stops a 60s retryDelay from stalling a
#: batch) lives in providers.py, where the retry actually happens.
def _retry_delay_seconds(exc: Exception, default: float) -> float:
    return retry_delay_seconds(exc, default, cap=float("inf"))


#: The wire contract for a verdict. Same three fields the pydantic
#: `_VerdictSchema` describes, expressed as JSON Schema because that is
#: what both providers' structured-output modes take.
VERDICT_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "relationship": {"type": "string", "enum": ["SAME", "DIFFERENT", "AMBIGUOUS"]},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["relationship", "confidence", "reason"],
    "additionalProperties": False,
}


def build_comparison_payload(comparison: CandidateComparison) -> dict:
    """Exactly what the model is shown — no more, and unchanged by the
    provider that happens to serve the call."""
    return {
        "merchant": {
            "reference": comparison.merchant.reference,
            "description": comparison.merchant.description,
            "amount_minor": comparison.merchant.amount_minor,
            "date": comparison.merchant.date.isoformat(),
        },
        "candidate": {
            "reference": comparison.candidate.reference,
            "description": comparison.candidate.description,
            "amount_minor": comparison.candidate.amount_minor,
            "date": comparison.candidate.date.isoformat(),
        },
        "deterministic_signals": {
            "amount_exact_match": comparison.amount_exact_match,
            "amount_delta_minor": comparison.amount_delta_minor,
            "days_apart": comparison.days_apart,
            "shared_reference_core": comparison.shared_reference_core,
            "weighted_text_similarity": comparison.text_similarity,
        },
    }


class ChainSemanticVerifier:
    """The semantic verifier, sitting on a provider chain rather than one SDK.

    Two things this deliberately does not do:

    It does not swallow a chain failure. If no provider can answer, the
    ProviderError propagates — matching.resolve_fuzzy_or_semantic catches
    it, classifies the record PROVIDER_ERROR and routes it to
    HUMAN_REVIEW. Manufacturing an AMBIGUOUS verdict here would look
    tidier and would be a lie about what the system knows.

    It does not report a generic backend. `backend` names the provider and
    model that actually produced the verdict, so a run served by the
    fallback is visibly a run served by the fallback — in the UI, in the
    audit trail, and in the evaluation report.
    """

    def __init__(self, chain: FallbackChain) -> None:
        self._chain = chain
        self._models = {p.name: p.model for p in chain.providers}

    @property
    def chain(self) -> FallbackChain:
        return self._chain

    @property
    def status(self) -> str:
        return self._chain.status

    def compare(self, comparison: CandidateComparison) -> SemanticVerdictResult:
        payload = build_comparison_payload(comparison)
        result, provider_name = self._chain.complete_json(
            system=_PROMPT_PREAMBLE,
            user=f"Input:\n{json.dumps(payload)}",
            schema=VERDICT_JSON_SCHEMA,
            timeout_s=30.0,
        )
        model = self._models.get(provider_name)
        backend = f"{provider_name}:{model}" if model else provider_name
        return self._to_result(result, provider_name, backend)

    @staticmethod
    def _to_result(payload: dict, provider_name: str, backend: str) -> SemanticVerdictResult:
        """Validate before trusting. Structured output is enforced provider-side,
        but a verdict that drives money movement is not the place to assume
        that held."""
        try:
            parsed = _VerdictSchema(
                relationship=payload["relationship"],
                confidence=payload["confidence"],
                reason=payload.get("reason", ""),
            )
        except Exception as exc:  # noqa: BLE001 — KeyError, ValidationError, TypeError all mean the same thing here
            raise ProviderError(
                ProviderErrorKind.PROVIDER_ERROR, provider_name,
                f"verdict did not match the expected schema ({type(exc).__name__}: {exc}); "
                f"keys present: {sorted(payload)}",
            ) from exc
        return SemanticVerdictResult(
            verdict=parsed.relationship,
            confidence=float(parsed.confidence),
            rationale=parsed.reason,
            backend=backend,
        )


class GeminiSemanticVerifier(ChainSemanticVerifier):
    """Gemini-only verifier — a one-provider chain.

    Retained under its original name because the benchmark scripts
    (benchmark_matching.py, benchmark_settlement_presence.py) compare
    backends head-to-head and need to pin one provider rather than get
    whichever the chain fell through to.
    """

    def __init__(self, model: str | None = None) -> None:
        super().__init__(FallbackChain([GeminiProvider(model=model)]))


class GroqSemanticVerifier(ChainSemanticVerifier):
    """Groq-only verifier — the mirror of the above, for the same reason."""

    def __init__(self, model: str | None = None) -> None:
        super().__init__(FallbackChain([GroqProvider(model=model)]))


class HeuristicSemanticVerifier:
    """Offline fallback: pure token-overlap + amount/date corroboration.
    Clearly labeled, never trusted to confidently say SAME on its own —
    see policy.py, which treats a heuristic-backend SAME the same way it
    treats a low-confidence AI verdict."""

    def compare(self, comparison: CandidateComparison) -> SemanticVerdictResult:
        text_sim = normalize.jaccard(comparison.merchant.description, comparison.candidate.description)
        amount_close = comparison.amount_exact_match or abs(comparison.amount_delta_minor) <= max(
            2, int(comparison.merchant.amount_minor * 0.02)
        )
        days_apart = comparison.days_apart

        if text_sim >= 0.6 and amount_close and days_apart <= 21:
            verdict: Verdict = "SAME"
            confidence = round(min(0.75, text_sim), 2)  # capped: heuristic is never confident enough alone
        elif comparison.shared_reference_core and amount_close:
            verdict = "SAME"
            confidence = 0.7
        elif text_sim < 0.15 or not amount_close:
            verdict = "DIFFERENT"
            confidence = round(1 - text_sim, 2)
        else:
            verdict = "AMBIGUOUS"
            confidence = round(text_sim, 2)

        rationale = (
            f"heuristic: text_jaccard={text_sim:.2f}, amount_close={amount_close}, "
            f"shared_core={comparison.shared_reference_core}, days_apart={days_apart} (no AI provider configured)"
        )
        return SemanticVerdictResult(verdict=verdict, confidence=confidence, rationale=rationale, backend="heuristic-fallback")


#: Set to 1/true to force the offline heuristic even with keys present.
#: The evaluation harness needs a deterministic, network-free mode — a
#: number produced by a run that silently reached the network is not
#: reproducible, and an accuracy figure that cannot be reproduced is not
#: a measurement.
AI_DISABLED_ENV = "ACCORD_AI_DISABLED"


def ai_disabled() -> bool:
    return os.environ.get(AI_DISABLED_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def get_semantic_verifier() -> SemanticVerifier:
    """The verifier this deployment can actually back.

    Any configured key gives the chain; no key gives the labeled offline
    heuristic. There is no in-between state where the system pretends to
    have a model it cannot call.
    """
    if ai_disabled():
        return HeuristicSemanticVerifier()
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GROQ_API_KEY"):
        chain = build_chain()
        if chain.providers:
            return ChainSemanticVerifier(chain)
    return HeuristicSemanticVerifier()
