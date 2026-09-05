"""
Deterministic normalization — references, amounts, text tokens. Every
function here is pure and has no notion of "this looks fine to me"; it
either normalizes to an exact, comparable form or it doesn't, and the
matching engine (matching.py) decides what to do when it doesn't.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from datetime import datetime

_REF_STRIP = re.compile(r"[^A-Z0-9]")
_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")
_DIGIT_RUN = re.compile(r"\d{4,}")

# Digit runs shorter than this are too collision-prone to treat as an
# identifier core (a 3-digit run collides constantly across a real
# settlement population).
MIN_REFERENCE_CORE_DIGITS = 4


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


def reference_cores(*values: str | None) -> set[str]:
    """Digit runs long enough to plausibly be an identifier, pulled from
    a reference and/or a description.

    'ORD.200427.CHK' and 'RZP/200427/SETL' share the core '200427'. Real
    merchants and gateways routinely wrap the same underlying order
    number in different prefixes and separators, so this is a genuine
    signal — but only a corroborating one. A shared core is never enough
    on its own to declare a match here (see matching.py): an invoice
    counter on one side can legitimately collide with an order number on
    the other, which is exactly what the benchmark's
    `reference_core_collision` case is built to punish.
    """
    cores: set[str] = set()
    for value in values:
        if value:
            cores.update(_DIGIT_RUN.findall(value))
    return cores


def references_comparable(a_cores: set[str], b_cores: set[str]) -> bool:
    """Are these two identifier sets drawn from the same numbering system?

    A merchant invoice number and a bank UTR are both digit runs, and they
    will never agree — but their disagreement means nothing, because they
    are not the same kind of identifier. Treating that as contradiction
    would reject every genuine bank-statement match on principle.

    Width is the available proxy: a counter issued by one system produces
    identifiers of consistent length, so `INV-2057` (4 digits) and
    `UTR774120` (6 digits) are not comparable, while `ORD-2057` and
    `RZP/2058/S` are. Crude, but it fails in the safe direction — when the
    two are judged incomparable the pair simply loses its identifier
    evidence and has to be carried by other signals or by the model.
    """
    if not a_cores or not b_cores:
        return False
    widths_a = {len(c) for c in a_cores}
    widths_b = {len(c) for c in b_cores}
    return bool(widths_a & widths_b)


def token_document_frequencies(texts: list[str]) -> Counter:
    """How many of these texts each token appears in. Used to down-weight
    boilerplate — see `weighted_jaccard`."""
    df: Counter = Counter()
    for text in texts:
        df.update(token_set(text))
    return df


def inverse_document_frequency(token: str, document_frequency: Counter, total_documents: int) -> float:
    """Standard smoothed IDF. A token in every record carries ~no weight;
    a token in one record carries the most."""
    if total_documents <= 0:
        return 1.0
    return math.log((total_documents + 1) / (document_frequency.get(token, 0) + 1)) + 1.0


def weighted_jaccard(a: str, b: str, document_frequency: Counter, total_documents: int) -> float:
    """Jaccard over token sets, weighted by IDF.

    Plain Jaccard treats 'payment', 'order', 'settlement' and 'customer'
    as being worth exactly as much as the order number and the product
    name. In a settlement population where nearly every description is
    built from the same template, that lets shared boilerplate outrank a
    genuine match — which is precisely the failure this system shipped
    with (see docs/ENGINEERING_FAILURES_AND_FIXES.md). Weighting by IDF
    makes the distinctive tokens decide the ranking.
    """
    ta, tb = token_set(a), token_set(b)
    if not ta and not tb:
        return 1.0
    union = ta | tb
    if not union:
        return 1.0
    weights = {t: inverse_document_frequency(t, document_frequency, total_documents) for t in union}
    union_weight = sum(weights.values())
    if union_weight <= 0:
        return 0.0
    return sum(weights[t] for t in (ta & tb)) / union_weight
