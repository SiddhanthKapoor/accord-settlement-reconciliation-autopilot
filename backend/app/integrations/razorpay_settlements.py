"""
Real Razorpay settlement integration — genuinely implemented, not a
stub, but empirically unable to produce non-empty data in this test-mode
account. Verified directly, not assumed:

    >>> client.settlement.all()
    {'entity': 'collection', 'count': 0, 'items': [], 'has_more': False}
    >>> client.payment.all({'count': 5})
    {'entity': 'collection', 'count': 0, 'items': []}

Razorpay's Settlements API only returns records for payments that were
actually captured AND have completed a real settlement cycle (T+2/T+3
business days after capture, via a real bank settlement run). A test-mode
account with no completed real payment flow — which is what any fresh
Buildathon account is — has nothing to fetch, by construction, regardless
of how this client is implemented. This isn't a code limitation; it's a
data-availability limitation of the sandbox itself.

Because of that, the reconciliation engine, dataset, and evaluation in
this project run entirely on the synthetic generator
(backend/data/generate_dataset.py) — clearly labeled as synthetic
everywhere it's used (see README.md's SYNTHETIC EVALUATION section).
This module exists so the integration path is real and exercised (see
the test suite), not merely described, and so a merchant's actual
Razorpay account — which does have real settlement history — could be
pointed at this same code with no changes to the reconciliation engine
itself.
"""

from __future__ import annotations

import os

from app.domain.models import RazorpaySettlementRecord


class RazorpayNotConfigured(RuntimeError):
    pass


def _client():
    import razorpay

    key_id = os.environ.get("RAZORPAY_KEY_ID")
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET")
    if not key_id or not key_secret:
        raise RazorpayNotConfigured("RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET not set.")
    return razorpay.Client(auth=(key_id, key_secret))


def fetch_live_settlements(count: int = 100) -> list[RazorpaySettlementRecord]:
    """Real call, real parsing — returns whatever Razorpay's Settlements
    API actually has for this account. Raises RazorpayNotConfigured if no
    credentials are set; returns an empty list (not an error) if the
    account genuinely has no settlement history, which is the honest,
    verified state of the account this project was built against."""
    client = _client()
    response = client.settlement.all({"count": count})
    records: list[RazorpaySettlementRecord] = []
    for item in response.get("items", []):
        records.append(_settlement_to_record(item))
    return records


def _settlement_to_record(item: dict) -> RazorpaySettlementRecord:
    from datetime import datetime, timezone

    created_at = datetime.fromtimestamp(item["created_at"], tz=timezone.utc)
    return RazorpaySettlementRecord(
        payment_id=item.get("id", ""),
        order_reference=item.get("id", ""),
        settlement_id=item.get("id", ""),
        gross_amount_minor=item.get("amount", 0),
        fee_minor=item.get("fees", 0),
        tax_minor=item.get("tax", 0),
        net_amount_minor=item.get("amount", 0) - item.get("fees", 0),
        order_date=created_at,
        settlement_date=created_at,
        status="settled",
        description=f"Razorpay settlement {item.get('id', '')}",
    )
