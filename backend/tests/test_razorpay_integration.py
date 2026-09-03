"""
Tests the real integration code path itself (parsing, error handling)
without depending on network access or a live account — the empirical
finding that this test account's settlement.all() returns zero items is
documented in razorpay_settlements.py's module docstring and the README,
not re-asserted here as if it were a unit test invariant.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.integrations import razorpay_settlements


def test_not_configured_raises_clear_error(monkeypatch):
    monkeypatch.delenv("RAZORPAY_KEY_ID", raising=False)
    monkeypatch.delenv("RAZORPAY_KEY_SECRET", raising=False)
    with pytest.raises(razorpay_settlements.RazorpayNotConfigured):
        razorpay_settlements.fetch_live_settlements()


def test_empty_account_returns_empty_list_not_error(monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_fake")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "fake_secret")

    class FakeSettlementResource:
        def all(self, params):
            return {"entity": "collection", "count": 0, "items": [], "has_more": False}

    class FakeClient:
        def __init__(self, auth):
            self.settlement = FakeSettlementResource()

    monkeypatch.setattr("razorpay.Client", FakeClient)
    result = razorpay_settlements.fetch_live_settlements()
    assert result == []


def test_parses_a_real_shaped_settlement_item(monkeypatch):
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_fake")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "fake_secret")

    sample_item = {"id": "setl_ABC123", "amount": 100000, "fees": 2000, "tax": 360, "created_at": 1780000000}

    class FakeSettlementResource:
        def all(self, params):
            return {"entity": "collection", "count": 1, "items": [sample_item], "has_more": False}

    class FakeClient:
        def __init__(self, auth):
            self.settlement = FakeSettlementResource()

    monkeypatch.setattr("razorpay.Client", FakeClient)
    result = razorpay_settlements.fetch_live_settlements()
    assert len(result) == 1
    assert result[0].payment_id == "setl_ABC123"
    assert result[0].gross_amount_minor == 100000
    assert result[0].net_amount_minor == 98000
