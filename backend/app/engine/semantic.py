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
  the one part of the system where "97% detection rate" would actually
  mean something probabilistic, unlike the deterministic checks.
"""

from __future__ import annotations

import difflib
import json
import os
import re
from dataclasses import dataclass
from typing import Literal, Protocol

from app.engine import text_normalize

Verdict = Literal["EQUIVALENT", "NOT_EQUIVALENT", "AMBIGUOUS"]


@dataclass
class ProductAttrs:
    name: str
    category: str


@dataclass
class SemanticVerdictResult:
    verdict: Verdict
    confidence: float
    rationale: str
    backend: str  # "anthropic:claude-*" or "heuristic-fallback"


class SemanticVerifier(Protocol):
    def compare(
        self, declared: ProductAttrs, observed: ProductAttrs, user_constraint_text: str | None
    ) -> SemanticVerdictResult: ...


_SYSTEM_PROMPT = """You are a narrow product-equivalence checker used inside a payments \
integrity system. You are given exactly two structured product records — DECLARED (what \
the transaction claims it is buying) and OBSERVED (what the merchant's catalog \
independently reports for the product actually being charged) — plus, optionally, the \
user's original free-text constraint.

Decide only one thing: would a reasonable buyer consider OBSERVED the same commercial \
item as DECLARED (allowing for spelling/unit/phrasing variation), or a different item \
(different product line, different category, materially different value)?

Respond with STRICT JSON only, no prose outside the JSON object:
{"verdict": "EQUIVALENT" | "NOT_EQUIVALENT" | "AMBIGUOUS", "confidence": <0.0-1.0>, \
"rationale": "<one sentence citing the specific attributes that matched or mismatched>"}

Use AMBIGUOUS only when the record genuinely could go either way even for a careful human \
reviewer. Do not guess wildly — if uncertain, prefer AMBIGUOUS over a confident wrong answer.
"""


class AnthropicSemanticVerifier:
    def __init__(self, model: str = "claude-haiku-4-5-20251001") -> None:
        import anthropic  # local import: only required if this backend is selected

        self._client = anthropic.Anthropic()
        self._model = model

    def compare(
        self, declared: ProductAttrs, observed: ProductAttrs, user_constraint_text: str | None
    ) -> SemanticVerdictResult:
        user_payload = {
            "declared": {"name": declared.name, "category": declared.category},
            "observed": {"name": observed.name, "category": observed.category},
            "user_constraint_text": user_constraint_text,
        }
        message = self._client.messages.create(
            model=self._model,
            max_tokens=200,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps(user_payload)}],
        )
        text = "".join(block.text for block in message.content if block.type == "text")
        parsed = _parse_json_verdict(text)
        return SemanticVerdictResult(
            verdict=parsed["verdict"],
            confidence=float(parsed["confidence"]),
            rationale=parsed["rationale"],
            backend=f"anthropic:{self._model}",
        )


def _parse_json_verdict(text: str) -> dict:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"model did not return JSON: {text!r}")
    data = json.loads(match.group(0))
    if data.get("verdict") not in ("EQUIVALENT", "NOT_EQUIVALENT", "AMBIGUOUS"):
        raise ValueError(f"invalid verdict field: {data!r}")
    return data


class HeuristicSemanticVerifier:
    """Deterministic-ish fallback used only when ANTHROPIC_API_KEY is not
    set, so the system is fully runnable offline. This is NOT presented
    as the AI contribution — it is clearly labeled in its `backend` field,
    and the Decision Engine additionally never lets it auto-confirm
    equivalence outright (see checks.py) because it has no real semantic
    understanding, only lexical/token overlap.

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
            verdict = "NOT_EQUIVALENT"
            confidence = round(1 - token_jaccard, 2)
        else:
            verdict = "AMBIGUOUS"
            confidence = round(token_jaccard, 2)

        rationale = (
            f"heuristic: name_similarity={name_sim:.2f}, token_jaccard={token_jaccard:.2f}, "
            f"extra_tokens={extra_tokens}, one_directional_extension={is_one_directional_extension}, "
            f"same_category={same_category} (no LLM configured)"
        )
        return SemanticVerdictResult(
            verdict=verdict, confidence=confidence, rationale=rationale, backend="heuristic-fallback"
        )


def get_semantic_verifier() -> SemanticVerifier:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return AnthropicSemanticVerifier()
    return HeuristicSemanticVerifier()
