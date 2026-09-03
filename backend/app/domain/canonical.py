"""
Canonicalization + hashing — generic, domain-independent. Used by the
audit ledger's hash chain (app/ledger/audit.py) and by anything that
needs a stable content hash of a structured record (e.g. a dataset
version fingerprint).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(obj: Any) -> bytes:
    """Deterministic JSON encoding: sorted keys, fixed separators, no
    whitespace, ASCII-safe. Any two equal Python structures always
    produce byte-identical output."""
    return json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_hash(fields: dict) -> str:
    return sha256_hex(canonical_json(fields))


GENESIS_HASH = "0" * 64


def chain_hash(prev_hash: str, event_payload: dict) -> str:
    body = canonical_json({"prev_hash": prev_hash, "event": event_payload})
    return sha256_hex(body)
