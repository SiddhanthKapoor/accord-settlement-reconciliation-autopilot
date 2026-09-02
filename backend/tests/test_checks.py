"""
Unit tests for the deterministic checks that don't require the live
catalog/backend processes — canonicalization, price-tolerance banding,
and staleness banding (tested with a synthetic past timestamp, not a
slow real sleep()).
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.domain.canonical import commitment_content_hash
from app.domain.models import CheckStatus, Commitment, Constraints, PaymentRequest
from app.engine.checks import DEFAULT_COMMITMENT_TTL_SECONDS, _price_tolerance_check
from app.ledger import db


@pytest.fixture(autouse=True)
def clean_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db._local.__dict__.clear()
    db.init_db()
    yield
    db._local.__dict__.clear()


def test_content_hash_is_stable_for_identical_terms():
    a = commitment_content_hash(
        merchant_id="m1", product_id="p1", category="electronics", quantity=1, price_minor=100, currency="INR"
    )
    b = commitment_content_hash(
        merchant_id="m1", product_id="p1", category="electronics", quantity=1, price_minor=100, currency="INR"
    )
    assert a == b


def test_content_hash_changes_when_any_commercial_field_changes():
    base = commitment_content_hash(
        merchant_id="m1", product_id="p1", category="electronics", quantity=1, price_minor=100, currency="INR"
    )
    variants = [
        dict(merchant_id="m2", product_id="p1", category="electronics", quantity=1, price_minor=100, currency="INR"),
        dict(merchant_id="m1", product_id="p2", category="electronics", quantity=1, price_minor=100, currency="INR"),
        dict(merchant_id="m1", product_id="p1", category="electronics", quantity=2, price_minor=100, currency="INR"),
        dict(merchant_id="m1", product_id="p1", category="electronics", quantity=1, price_minor=101, currency="INR"),
    ]
    for v in variants:
        assert commitment_content_hash(**v) != base


def test_price_tolerance_graduated_bands():
    exact = _price_tolerance_check(
        name="x", expected_minor=1000, observed_minor=1000, tolerance_pct=2.0, threat_ref="T-32", context="t"
    )
    assert exact.status == CheckStatus.PASS

    within_tolerance = _price_tolerance_check(
        name="x", expected_minor=1000, observed_minor=1015, tolerance_pct=2.0, threat_ref="T-32", context="t"
    )  # 1.5% drift, tolerance is 2%
    assert within_tolerance.status == CheckStatus.PASS

    beyond_tolerance_below_ceiling = _price_tolerance_check(
        name="x", expected_minor=1000, observed_minor=1040, tolerance_pct=2.0, threat_ref="T-32", context="t"
    )  # 4% drift: beyond 2% tolerance, below 6% hard ceiling (2% * 3)
    assert beyond_tolerance_below_ceiling.status == CheckStatus.WARN

    beyond_hard_ceiling = _price_tolerance_check(
        name="x", expected_minor=1000, observed_minor=2000, tolerance_pct=2.0, threat_ref="T-32", context="t"
    )  # 100% drift
    assert beyond_hard_ceiling.status == CheckStatus.FAIL


def _make_commitment(created_at: datetime, **overrides) -> Commitment:
    defaults = dict(
        commitment_id="commit_1", intent_id="intent_1", merchant_id="m1", product_id="p1",
        product_name="Widget", category="electronics", quantity=1, price_minor=1000, currency="INR",
        evidence_id="ev_1", created_at=created_at, version=1, content_hash="deadbeef",
    )
    defaults.update(overrides)
    return Commitment(**defaults)


def test_staleness_fresh_commitment_passes():
    now = datetime.now(timezone.utc)
    commitment = _make_commitment(created_at=now)
    payment_request = PaymentRequest(
        transaction_id="commit_1", commitment_id="commit_1", merchant_id="m1", product_id="p1",
        product_name="Widget", category="electronics", quantity=1, price_minor=1000,
        client_request_id="r1",
    )
    intent_row = {"budget_reserved": 1, "budget_reserved_by": "commit_1"}
    constraints = Constraints(max_amount_minor=10_000, single_use=True)

    # ground-truth checks will fail to reach a live catalog service in this
    # unit test — that's fine, we only assert on the staleness check here.
    checks = _run_checks_ignoring_ground_truth(intent_row, constraints, commitment, payment_request)
    staleness = next(c for c in checks if c.name == "commitment_staleness")
    assert staleness.status == CheckStatus.PASS


def test_staleness_warns_then_fails_as_age_increases():
    now = datetime.now(timezone.utc)

    warn_case = _make_commitment(created_at=now - timedelta(seconds=DEFAULT_COMMITMENT_TTL_SECONDS * 2))
    fail_case = _make_commitment(created_at=now - timedelta(seconds=DEFAULT_COMMITMENT_TTL_SECONDS * 4))

    payment_request = PaymentRequest(
        transaction_id="commit_1", commitment_id="commit_1", merchant_id="m1", product_id="p1",
        product_name="Widget", category="electronics", quantity=1, price_minor=1000,
        client_request_id="r1",
    )
    intent_row = {"budget_reserved": 1, "budget_reserved_by": "commit_1"}
    constraints = Constraints(max_amount_minor=10_000, single_use=True)

    warn_checks = _run_checks_ignoring_ground_truth(intent_row, constraints, warn_case, payment_request)
    fail_checks = _run_checks_ignoring_ground_truth(intent_row, constraints, fail_case, payment_request)

    assert next(c for c in warn_checks if c.name == "commitment_staleness").status == CheckStatus.WARN
    assert next(c for c in fail_checks if c.name == "commitment_staleness").status == CheckStatus.FAIL


def _run_checks_ignoring_ground_truth(intent_row, constraints, commitment, payment_request):
    """The ground-truth checks require the live catalog_service process,
    which isn't running in a unit test. We monkeypatch that one function
    to avoid a network dependency while still exercising every other
    check for real."""
    import app.engine.checks as checks_module
    from app.integrations import catalog_client

    original = catalog_client.fetch_ground_truth
    catalog_client.fetch_ground_truth = lambda *a, **k: (_ for _ in ()).throw(
        catalog_client.CatalogUnavailable("not running in this unit test")
    )
    try:
        return checks_module.run_integrity_checks(intent_row, constraints, commitment, payment_request)
    finally:
        catalog_client.fetch_ground_truth = original
