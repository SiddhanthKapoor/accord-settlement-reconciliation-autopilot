"""
Canonicalization + hashing.

Two different hashes matter and must not be confused:

1. content_hash(commitment)  — hash of the *commercial* fields only
   (merchant, product, quantity, price, currency). Used to detect whether
   two commitments represent the same deal. Deliberately excludes
   commitment_id/created_at/version so that re-hashing the same commercial
   terms always produces the same digest, and any drift in terms changes it.

2. chain_hash(prev_hash, event) — the audit-log hash chain. Includes
   everything (including timestamps and IDs) because its job is tamper
   evidence of the *log*, not equivalence of *content*.

Money is compared as integers. This module never round-trips through
float for anything that participates in a hash or an equality check.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _canonical_json(obj: Any) -> bytes:
    """Deterministic JSON encoding: sorted keys, fixed separators, no
    whitespace, ASCII-safe. Any two equal Python structures always
    produce byte-identical output."""
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,  # datetimes -> isoformat string, deterministically
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def commitment_content_hash(
    *,
    merchant_id: str,
    product_id: str,
    category: str,
    quantity: int,
    price_minor: int,
    currency: str,
) -> str:
    """Hash of exactly the fields that define 'what is being bought,
    from whom, for how much' — nothing else. This is the value compared
    across state transitions to detect drift, and is what a replay
    attempt reuses unchanged."""
    payload = {
        "merchant_id": merchant_id,
        "product_id": product_id,
        "category": category,
        "quantity": quantity,
        "price_minor": price_minor,
        "currency": currency,
    }
    return sha256_hex(_canonical_json(payload))


GENESIS_HASH = "0" * 64


def chain_hash(prev_hash: str, event_payload: dict) -> str:
    body = _canonical_json({"prev_hash": prev_hash, "event": event_payload})
    return sha256_hex(body)
