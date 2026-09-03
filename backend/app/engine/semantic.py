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
import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel

from app.engine import normalize

Verdict = Literal["SAME", "DIFFERENT", "AMBIGUOUS"]


@dataclass
class MatchCandidateText:
    description: str
    amount_minor: int
    date: datetime


@dataclass
class SemanticVerdictResult:
    verdict: Verdict
    confidence: float
    rationale: str
    backend: str


class SemanticVerifier(Protocol):
    def compare(self, merchant: MatchCandidateText, candidate: MatchCandidateText) -> SemanticVerdictResult: ...


_PROMPT_PREAMBLE = """You are a narrow reconciliation-matching classifier used inside a \
finance-operations system. You are given two structured transaction descriptions — MERCHANT \
(the merchant's own order record) and CANDIDATE (a Razorpay settlement record whose reference \
did not exactly match, but was found nearby in time with some textual overlap) — plus each \
side's amount and date.

Decide only one thing: do these two records plausibly describe the SAME underlying payment, \
are they clearly DIFFERENT payments, or is it genuinely AMBIGUOUS even for a careful human \
reviewer? A materially different amount or date is strong evidence of DIFFERENT even if the \
text is similar. Do not guess wildly — prefer AMBIGUOUS over a confident wrong answer.
Keep the reason to one short sentence.
"""


class _VerdictSchema(BaseModel):
    relationship: Verdict
    confidence: float
    reason: str


def _retry_delay_seconds(exc: Exception, default: float) -> float:
    try:
        details = exc.details.get("error", {}).get("details", [])  # type: ignore[attr-defined]
        for d in details:
            if d.get("@type", "").endswith("RetryInfo"):
                return float(str(d.get("retryDelay", "")).rstrip("s") or default)
    except Exception:
        pass
    return default


class GeminiSemanticVerifier:
    def __init__(self, model: str | None = None) -> None:
        from google import genai

        self._client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        self._model = model or os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")

    def compare(self, merchant: MatchCandidateText, candidate: MatchCandidateText) -> SemanticVerdictResult:
        from google.genai import errors as genai_errors
        from google.genai import types

        payload = {
            "merchant": {"description": merchant.description, "amount_minor": merchant.amount_minor, "date": merchant.date.isoformat()},
            "candidate": {"description": candidate.description, "amount_minor": candidate.amount_minor, "date": candidate.date.isoformat()},
        }
        contents = f"{_PROMPT_PREAMBLE}\n\nInput:\n{json.dumps(payload)}"
        config = types.GenerateContentConfig(
            response_mime_type="application/json", response_schema=_VerdictSchema, temperature=0.0
        )

        last_error: Exception | None = None
        for attempt in range(2):
            try:
                response = self._client.models.generate_content(model=self._model, contents=contents, config=config)
                parsed: _VerdictSchema = response.parsed
                return SemanticVerdictResult(
                    verdict=parsed.relationship, confidence=float(parsed.confidence),
                    rationale=parsed.reason, backend=f"gemini:{self._model}",
                )
            except genai_errors.ClientError as exc:
                last_error = exc
                if exc.code == 429 and attempt == 0:
                    time.sleep(_retry_delay_seconds(exc, default=3.0))
                    continue
                raise
        raise last_error  # pragma: no cover


class HeuristicSemanticVerifier:
    """Offline fallback: pure token-overlap + amount/date corroboration.
    Clearly labeled, never trusted to confidently say SAME on its own —
    see policy.py, which treats a heuristic-backend SAME the same way it
    treats a low-confidence AI verdict."""

    def compare(self, merchant: MatchCandidateText, candidate: MatchCandidateText) -> SemanticVerdictResult:
        text_sim = normalize.jaccard(merchant.description, candidate.description)
        amount_close = normalize.amounts_match(merchant.amount_minor, candidate.amount_minor, tolerance_minor=max(2, int(merchant.amount_minor * 0.02)))
        days_apart = abs((candidate.date - merchant.date).days)

        if text_sim >= 0.6 and amount_close and days_apart <= 21:
            verdict: Verdict = "SAME"
            confidence = round(min(0.75, text_sim), 2)  # capped: heuristic is never confident enough alone
        elif text_sim < 0.15 or not amount_close:
            verdict = "DIFFERENT"
            confidence = round(1 - text_sim, 2)
        else:
            verdict = "AMBIGUOUS"
            confidence = round(text_sim, 2)

        rationale = f"heuristic: text_jaccard={text_sim:.2f}, amount_close={amount_close}, days_apart={days_apart} (no AI provider configured)"
        return SemanticVerdictResult(verdict=verdict, confidence=confidence, rationale=rationale, backend="heuristic-fallback")


def get_semantic_verifier() -> SemanticVerifier:
    if os.environ.get("GEMINI_API_KEY"):
        return GeminiSemanticVerifier()
    return HeuristicSemanticVerifier()
