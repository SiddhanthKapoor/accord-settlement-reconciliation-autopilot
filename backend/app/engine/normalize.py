"""
Deterministic normalization — references, amounts, text tokens. Every
function here is pure and has no notion of "this looks fine to me"; it
either normalizes to an exact, comparable form or it doesn't, and the
matching engine (matching.py) decides what to do when it doesn't.
"""

from __future__ import annotations

import re
from datetime import datetime

_REF_STRIP = re.compile(r"[^A-Z0-9]")
_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


def normalize_reference(ref: str | None) -> str:
    """Uppercase, strip everything but letters/digits. 'ORD-58291',
    'ord_58291', 'Ord58291 ' all normalize to 'ORD58291'. This alone
    resolves the large majority of real-world reference mismatches —
    the genuinely ambiguous cases are the ones that still don't match
    after this."""
    if not ref:
        return ""
    return _REF_STRIP.sub("", ref.upper())


def normalize_text(text: str) -> str:
    return _TOKEN_SPLIT.sub(" ", text.lower()).strip()


def token_set(text: str) -> set[str]:
    return set(normalize_text(text).split())


def jaccard(a: str, b: str) -> float:
    ta, tb = token_set(a), token_set(b)
    if not ta and not tb:
        return 1.0
    union = ta | tb
    if not union:
        return 1.0
    return len(ta & tb) / len(union)


def amounts_match(a: int, b: int, tolerance_minor: int) -> bool:
    return abs(a - b) <= tolerance_minor


def days_between(d1: datetime, d2: datetime) -> int:
    return abs((d2 - d1).days)
