"""
These tests are the actual proof for T-33 (shared-budget races) and T-31
(replay), not just a UI demo of them. They hammer the same SQLite-backed
primitives with real OS threads, not simulated/sequential calls — if the
CAS logic in store.py were a naive read-then-write, this test would be
flaky and occasionally let two reservations through. It shouldn't.
"""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.domain.models import Constraints, TransactionIntent
from app.ledger import db, store


@pytest.fixture(autouse=True)
def clean_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db._local.__dict__.clear()
    db.init_db()
    yield
    db._local.__dict__.clear()


def _make_intent(max_amount_minor: int, single_use: bool = True) -> str:
    intent = TransactionIntent(
        intent_id="intent_test",
        constraints=Constraints(max_amount_minor=max_amount_minor, single_use=single_use),
    )
    store.create_intent(intent)
    return intent.intent_id


def test_reserve_budget_is_exclusive_under_concurrency():
    """20 threads race to reserve a single-use budget of ₹1,000 against
    20 different commitment_ids, each asking for the full amount. Exactly
    one must win — this is the server-side fix for AP2's T-33."""
    intent_id = _make_intent(max_amount_minor=100_000, single_use=True)
    commitment_ids = [f"commit_{i}" for i in range(20)]

    def attempt(commitment_id: str) -> bool:
        db._local.__dict__.clear()  # each thread needs its own sqlite connection
        return store.reserve_budget(intent_id, commitment_id, 100_000)

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(attempt, commitment_ids))

    assert sum(results) == 1, f"expected exactly 1 winner, got {sum(results)}"

    db._local.__dict__.clear()
    row = store.get_intent(intent_id)
    assert row["budget_remaining_minor"] == 0
    assert row["budget_reserved"] == 1


def test_reserve_budget_respects_remaining_amount_not_just_single_use_flag():
    """Non-single-use budget: two commitments each request 60% of a
    ₹1,000 budget concurrently. Only one can fit — this proves the CAS
    is checking the actual remaining amount, not merely a boolean flag."""
    intent_id = _make_intent(max_amount_minor=100_000, single_use=False)

    def attempt(commitment_id: str) -> bool:
        db._local.__dict__.clear()
        return store.reserve_budget(intent_id, commitment_id, 60_000)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ["commit_a", "commit_b"]))

    assert sorted(results) == [False, True]


def test_consume_commitment_is_exclusive_under_concurrency():
    """The final T-31 guard: 20 threads race to consume the SAME
    commitment_id (simulating 20 concurrent /execute calls against one
    already-ALLOWed commitment, e.g. a replayed request racing the
    original). Exactly one may proceed to call Razorpay."""
    commitment_id = "commit_shared"

    def attempt(_: int) -> bool:
        db._local.__dict__.clear()
        return store.consume_commitment(commitment_id, "txn_shared")

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(attempt, range(20)))

    assert sum(results) == 1, f"expected exactly 1 winner, got {sum(results)}"


def test_replay_after_consumption_is_detected():
    commitment_id = "commit_x"
    assert store.is_commitment_consumed(commitment_id) is False
    assert store.consume_commitment(commitment_id, "txn_x") is True
    assert store.is_commitment_consumed(commitment_id) is True
    # A second, independent attempt to consume the same commitment (e.g. a
    # replayed artifact presented again later) must fail.
    assert store.consume_commitment(commitment_id, "txn_x_replayed") is False
