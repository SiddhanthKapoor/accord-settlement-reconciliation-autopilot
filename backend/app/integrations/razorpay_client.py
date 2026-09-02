"""
Real Razorpay test-mode integration.

This is called from exactly one place: the /execute endpoint, and only
after a Decision with outcome ALLOW has been persisted. A BLOCKed or
REQUIRE_RECONFIRMATION transaction never reaches this module — that
boundary is enforced in app/api/routes.py, not here, but this module
still refuses to run without real credentials rather than pretending
to succeed, because faking a payment integration in a security-adjacent
demo is worse than admitting it isn't configured.

Uses Razorpay's official Python SDK against test-mode keys
(RAZORPAY_KEY_ID starting with 'rzp_test_'). Test-mode keys are available
immediately on signup with no KYC (confirmed against Razorpay's own
account-creation docs) — this is a real integration, not a stub, once
those two env vars are set.
"""

from __future__ import annotations

import os

import razorpay

from app.domain.models import Commitment


class RazorpayNotConfigured(RuntimeError):
    pass


def _client() -> razorpay.Client:
    key_id = os.environ.get("RAZORPAY_KEY_ID")
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET")
    if not key_id or not key_secret:
        raise RazorpayNotConfigured(
            "RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET not set. Get free test-mode "
            "keys from the Razorpay dashboard (no KYC required for test mode) "
            "and put them in backend/.env — see README."
        )
    client = razorpay.Client(auth=(key_id, key_secret))
    return client


def execute_payment_link(commitment: Commitment, transaction_id: str) -> dict:
    """Creates a real Razorpay test-mode Payment Link for an ALLOWed
    commitment. Returns the raw Razorpay response (short_url is what you'd
    show/open in a demo video)."""
    client = _client()
    payload = {
        "amount": commitment.price_minor * commitment.quantity,
        "currency": commitment.currency,
        "description": f"{commitment.product_name} x{commitment.quantity} ({commitment.merchant_id})",
        "reference_id": transaction_id,
        "notes": {
            "interlock_transaction_id": transaction_id,
            "interlock_commitment_id": commitment.commitment_id,
            "interlock_content_hash": commitment.content_hash,
        },
    }
    return client.payment_link.create(payload)  # type: ignore[no-any-return]
