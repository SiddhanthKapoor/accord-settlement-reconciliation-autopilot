"""
The one place in this system where an LLM is used — and it is used
narrowly, on purpose.

Everything else in engine/checks.py is deterministic: exact merchant ID
match, exact product ID match, numeric price/quantity comparison against
a declared tolerance, replay lookup, atomic budget reservation. None of
that needs a model.

What deterministic matching genuinely cannot resolve is fuzzy product
identity: "Amul Butter 500g" vs "Amul Butter 500 grams" is the same
product; "Amul Butter 500g" vs "Imported Gourmet Butter Hamper" is not,
even though both are technically "butter." This module is called ONLY
when checks.py finds a product_id mismatch that survives normalized
string matching — i.e. only on the genuinely ambiguous cases.

Constraints this module obeys, because the product boundary requires it:
- It never sees the agent's private reasoning/chain-of-thought. Its input
  is exactly two structured ProductRef-shaped records (declared vs.
  independently-fetched-observed) plus the user's original declared
  constraint text, if any.
- It never executes a money action itself. It returns a verdict + a
  bounded confidence; the Decision Engine (decision.py) is what turns
  that into ALLOW/BLOCK/REQUIRE_RECONFIRMATION, and does so
  conservatively (see decision.py policy table).
- Its accuracy is measured on a held-out labeled set (see
  backend/scenarios/semantic_eval.py) and reported honestly — this is
  the one part of the system where a detection-rate number would actually
  mean something probabilistic, unlike the deterministic checks.

Provider priority (see get_semantic_verifier): Gemini, if GEMINI_API_KEY
is set, is the primary classifier. Anthropic remains supported as an
alternate provider if ANTHROPIC_API_KEY is set instead. If neither is
configured, the deterministic heuristic fallback runs, and the system
stays fully functional offline — see HeuristicSemanticVerifier.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel

from app.engine import text_normalize

# EQUIVALENT: same commercial item, surface variation only (typo/unit/case).
# MATERIAL_CHANGE: a different item, tier, or category — not a wording issue.
# AMBIGUOUS: a careful human reviewer could reasonably go either way.
Verdict = Literal["EQUIVALENT", "MATERIAL_CHANGE", "AMBIGUOUS"]


@dataclass
class ProductAttrs:
    name: str
    category: str


@dataclass
class SemanticVerdictResult:
    verdict: Verdict
    confidence: float
    rationale: str
    backend: str  # e.g. "gemini:gemini-2.5-flash", "anthropic:claude-*", "heuristic-fallback"


class SemanticVerifier(Protocol):
    def compare(
        self, declared: ProductAttrs, observed: ProductAttrs, user_constraint_text: str | None
    ) -> SemanticVerdictResult: ...


_PROMPT_PREAMBLE = """You are a narrow product-equivalence checker used inside a payments \
integrity system. You are given exactly two structured product records — VERIFIED (what \
the transaction committed to buying) and OBSERVED (what the merchant's catalog \
independently reports for the product actually being charged) — plus, optionally, the \
user's original free-text constraint.

Decide only one thing: would a reasonable buyer consider OBSERVED the same commercial \
item as VERIFIED (allowing for spelling/unit/phrasing variation), a MATERIAL_CHANGE \
(different product line, different category, different tier/value), or genuinely AMBIGUOUS?

Use AMBIGUOUS only when the record genuinely could go either way even for a careful human \
reviewer. Do not guess wildly — if uncertain, prefer AMBIGUOUS over a confident wrong answer.
Keep the reason to one short sentence citing the specific attributes that matched or mismatched.
"""


class _VerdictSchema(BaseModel):
    relationship: Verdict
    confidence: float
    reason: str


def _retry_delay_seconds(exc: Exception, default: float) -> float:
    """Best-effort extraction of the server-suggested retry delay from a
    Gemini 429 error body; falls back to `default` if the shape doesn't
    match what's expected rather than raising a second error."""
    try:
        details = exc.details.get("error", {}).get("details", [])  # type: ignore[attr-defined]
        for d in details:
            if d.get("@type", "").endswith("RetryInfo"):
                return float(str(d.get("retryDelay", "")).rstrip("s") or default)
    except Exception:
        pass
    return default


class GeminiSemanticVerifier:
    """Primary semantic backend. Uses Google's official `google-genai` SDK
    with a Pydantic response_schema so the model's output is structurally
    validated by the SDK itself, not just regex-parsed hopefully-JSON."""

    def __init__(self, model: str | None = None) -> None:
        from google import genai  # local import: only required if this backend is selected

        self._client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        self._model = model or os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")

    def compare(
        self, declared: ProductAttrs, observed: ProductAttrs, user_constraint_text: str | None
    ) -> SemanticVerdictResult:
        from google.genai import types
        from google.genai import errors as genai_errors

        user_payload = {
            "verified": {"name": declared.name, "category": declared.category},
            "observed": {"name": observed.name, "category": observed.category},
            "user_constraint_text": user_constraint_text,
        }
        contents = f"{_PROMPT_PREAMBLE}\n\nInput:\n{json.dumps(user_payload)}"
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=_VerdictSchema,
            temperature=0.0,
        )

        # Free-tier rate limits are low (a handful of requests/minute) and
        # transient 429s are routine, not a system failure — one short
        # retry honoring the server's suggested delay is standard practice
        # here, not a workaround for a real error.
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                response = self._client.models.generate_content(
                    model=self._model, contents=contents, config=config
                )
                parsed: _VerdictSchema = response.parsed
                return SemanticVerdictResult(
                    verdict=parsed.relationship,
                    confidence=float(parsed.confidence),
                    rationale=parsed.reason,
                    backend=f"gemini:{self._model}",
                )
            except genai_errors.ClientError as exc:
                last_error = exc
                if exc.code == 429 and attempt == 0:
                    time.sleep(_retry_delay_seconds(exc, default=3.0))
                    continue
                raise
        raise last_error  # pragma: no cover — unreachable, satisfies type checkers


class AnthropicSemanticVerifier:
    """Alternate provider, supported for continuity if ANTHROPIC_API_KEY is
    set instead of GEMINI_API_KEY. Same bounded contract as the Gemini
    backend above."""

    def __init__(self, model: str = "claude-haiku-4-5-20251001") -> None:
        import anthropic  # local import: only required if this backend is selected

        self._client = anthropic.Anthropic()
        self._model = model

    def compare(
        self, declared: ProductAttrs, observed: ProductAttrs, user_constraint_text: str | None
    ) -> SemanticVerdictResult:
        user_payload = {
            "verified": {"name": declared.name, "category": declared.category},
            "observed": {"name": observed.name, "category": observed.category},
            "user_constraint_text": user_constraint_text,
        }
        system = (
            _PROMPT_PREAMBLE
            + '\nRespond with STRICT JSON only, no prose outside the object: '
            '{"relationship": "EQUIVALENT" | "MATERIAL_CHANGE" | "AMBIGUOUS", '
            '"confidence": <0.0-1.0>, "reason": "<one sentence>"}'
        )
        message = self._client.messages.create(
            model=self._model,
            max_tokens=200,
            system=system,
            messages=[{"role": "user", "content": json.dumps(user_payload)}],
        )
        text = "".join(block.text for block in message.content if block.type == "text")
        parsed = _parse_json_verdict(text)
        return SemanticVerdictResult(
            verdict=parsed["relationship"],
            confidence=float(parsed["confidence"]),
            rationale=parsed["reason"],
            backend=f"anthropic:{self._model}",
        )


def _parse_json_verdict(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"model did not return JSON: {text!r}")
    data = json.loads(match.group(0))
    if data.get("relationship") not in ("EQUIVALENT", "MATERIAL_CHANGE", "AMBIGUOUS"):
        raise ValueError(f"invalid relationship field: {data!r}")
    return data


class HeuristicSemanticVerifier:
    """Deterministic-ish fallback used only when no AI provider is
    configured, so the system is fully runnable offline. This is NOT
    presented as the AI contribution — it is clearly labeled in its
    `backend` field, and the Decision Engine additionally never lets it
    auto-confirm equivalence outright (see checks.py) because it has no
    real semantic understanding, only lexical/token overlap.

    Two signals, not one: plain sequence similarity is fooled by
    containment (a short name is a near-total substring of a longer
    "premium bundle" variant name, which inflates the ratio even though
    they are commercially different items at a different price point).

    Token-set overlap alone isn't enough either — it can't tell "one word
    swapped for another" (ambiguous: "Salted Potato Chips" vs "Classic
    Salted Chips") apart from "several words purely added on one side"
    (a bundle/premium upsell pattern: "Wireless Mouse" vs "Wireless Mouse
    Premium Gaming Bundle"), even though both can have the same token
    overlap size. The distinguishing signal is containment direction: a
    one-directional extension (the smaller name's tokens are a strict
    subset of the larger one's, with several tokens purely added) reads
    as a different, higher-tier product; a two-directional swap with
    comparable token counts is genuinely ambiguous instead."""

    def compare(
        self, declared: ProductAttrs, observed: ProductAttrs, user_constraint_text: str | None
    ) -> SemanticVerdictResult:
        name_sim = difflib.SequenceMatcher(
            None, text_normalize.normalize(declared.name), text_normalize.normalize(observed.name)
        ).ratio()
        token_jaccard = text_normalize.jaccard(declared.name, observed.name)
        declared_tokens = text_normalize.token_set(declared.name)
        observed_tokens = text_normalize.token_set(observed.name)
        extra_tokens = len(declared_tokens | observed_tokens) - len(declared_tokens & observed_tokens)
        is_one_directional_extension = (
            declared_tokens <= observed_tokens or observed_tokens <= declared_tokens
        ) and extra_tokens >= 2
        same_category = text_normalize.normalize(declared.category) == text_normalize.normalize(observed.category)

        if token_jaccard >= 0.8 and same_category:
            verdict: Verdict = "EQUIVALENT"
            confidence = round(token_jaccard, 2)
        elif is_one_directional_extension or token_jaccard < 0.4 or not same_category:
            verdict = "MATERIAL_CHANGE"
            confidence = round(1 - token_jaccard, 2)
        else:
            verdict = "AMBIGUOUS"
            confidence = round(token_jaccard, 2)

        rationale = (
            f"heuristic: name_similarity={name_sim:.2f}, token_jaccard={token_jaccard:.2f}, "
            f"extra_tokens={extra_tokens}, one_directional_extension={is_one_directional_extension}, "
            f"same_category={same_category} (no AI provider configured)"
        )
        return SemanticVerdictResult(
            verdict=verdict, confidence=confidence, rationale=rationale, backend="heuristic-fallback"
        )


def get_semantic_verifier() -> SemanticVerifier:
    if os.environ.get("GEMINI_API_KEY"):
        return GeminiSemanticVerifier()
    if os.environ.get("ANTHROPIC_API_KEY"):
        return AnthropicSemanticVerifier()
    return HeuristicSemanticVerifier()
