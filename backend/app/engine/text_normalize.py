"""
Shared, deterministic text normalization used by the fast-path exact/near
match in checks.py AND the heuristic fallback in semantic.py.

This exists specifically so a case like "Amul Butter 500g" vs "Amul
Butter 500 grams" is resolved here — deterministically, no model call —
rather than being escalated to the semantic verifier. The semantic
verifier should only ever see cases that survive this normalization
still looking different, i.e. genuinely ambiguous ones.
"""

from __future__ import annotations

import re

_UNIT_PATTERNS = [
    (re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:grams?|gms?)\b"), r"\1 g"),
    (re.compile(r"\b(\d+(?:\.\d+)?)\s*g\b"), r"\1 g"),
    (re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:kilograms?|kgs?)\b"), r"\1 kg"),
    (re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:millilitres?|milliliters?|mls?)\b"), r"\1 ml"),
    (re.compile(r"\b(\d+(?:\.\d+)?)\s*(?:litres?|liters?)\b"), r"\1 l"),
    (re.compile(r"\b(\d+(?:\.\d+)?)\s*l\b"), r"\1 l"),
]


def normalize(text: str) -> str:
    s = text.lower()
    s = re.sub(r"[^a-z0-9.\s]+", " ", s)
    for pattern, repl in _UNIT_PATTERNS:
        s = pattern.sub(repl, s)
    return re.sub(r"\s+", " ", s).strip()


def token_set(text: str) -> set[str]:
    return set(normalize(text).split())


def jaccard(a: str, b: str) -> float:
    ta, tb = token_set(a), token_set(b)
    if not ta and not tb:
        return 1.0
    union = ta | tb
    if not union:
        return 1.0
    return len(ta & tb) / len(union)
