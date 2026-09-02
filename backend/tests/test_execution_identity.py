"""
Regression tests for a real bug found during manual browser testing:
the frontend generated a fresh client_request_id for /execute instead of
reusing the one returned by /verify, so /execute always failed with
"no verified payment request found for this client_request_id" even
after a genuine ALLOW.

The bug was entirely in the frontend (backend/scenarios/run_scenarios.py
already reused the id correctly, which is why the backend test suite
never caught it). These tests pin the API-level contract the frontend
must follow: /execute must be called with the EXACT client_request_id a
prior /verify call used for the SAME commitment. That is not an
accident of the implementation — it is the identity link between "what
Interlock approved" and "what gets executed," and is part of the
security model (see docs/DECISION_REPORT.md, T-31).

Uses FastAPI's TestClient against the real app (routes.py unmodified),
with the merchant catalog and Razorpay client mocked so this suite is
hermetic — it proves the identity contract, not the live integrations
already covered by backend/scenarios/run_scenarios.py.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

from app.domain.models import ProductRef
from app.integrations import catalog_client, razorpay_client
from app.ledger import db

MOUSE = ProductRef(
    merchant_id="merchant_electronics_01", product_id="mouse_001", name="Wireless Mouse",
    category="electronics", price_minor=149900, currency="INR", available=True,
)


@pytest.fixture(autouse=True)
def clean_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.db")
    db._local.__dict__.clear()
    yield
    db._local.__dict__.clear()


@pytest.fixture(autouse=True)
def fake_catalog(monkeypatch):
    monkeypatch.setattr(catalog_client, "fetch_ground_truth", lambda merchant_id, product_id: MOUSE)


@pytest.fixture(autouse=True)
def fake_razorpay(monkeypatch):
    calls = []

    def fake_execute(commitment, transaction_id):
        calls.append(transaction_id)
        return {"id": "plink_test123", "short_url": "https://rzp.io/test123", "status": "created"}

    monkeypatch.setattr(razorpay_client, "execute_payment_link", fake_execute)
    return calls  # tests can assert on this to confirm execution actually reached Razorpay


@pytest.fixture
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _setup_commitment(client, *, max_amount_minor=200_000, quantity=1):
    """Real request/commitment lifecycle: intent -> evidence -> commitment."""
    intent = client.post("/intents", json={
        "constraints": {"max_amount_minor": max_amount_minor, "max_quantity": quantity}
    }).json()
    evidence = client.post(f"/intents/{intent['intent_id']}/evidence", json={
        "merchant_id": "merchant_electronics_01", "product_id": "mouse_001", "stage": "SELECTED",
    }).json()
    commit = client.post(f"/intents/{intent['intent_id']}/commitments", json={
        "evidence_id": evidence["evidence_id"], "quantity": quantity,
    }).json()
    return intent["intent_id"], commit["commitment"]["commitment_id"]


def _verify_payload(client_request_id: str, **overrides) -> dict:
    payload = {
        "client_request_id": client_request_id,
        "merchant_id": "merchant_electronics_01",
        "product_id": "mouse_001",
        "product_name": "Wireless Mouse",
        "category": "electronics",
        "quantity": 1,
        "price_minor": 149900,
    }
    payload.update(overrides)
    return payload


def test_execute_succeeds_when_reusing_verify_client_request_id(client, fake_razorpay):
    """The fix: /execute called with the SAME id /verify returned for ALLOW
    must succeed and must genuinely invoke the configured execution path."""
    intent_id, commitment_id = _setup_commitment(client)
    req_id = _new_id("req")

    verify_resp = client.post(
        f"/intents/{intent_id}/commitments/{commitment_id}/verify", json=_verify_payload(req_id)
    )
    assert verify_resp.status_code == 200
    assert verify_resp.json()["decision"]["outcome"] == "ALLOW"

    exec_resp = client.post(
        f"/intents/{intent_id}/commitments/{commitment_id}/execute", json={"client_request_id": req_id}
    )
    assert exec_resp.status_code == 200, exec_resp.text
    assert exec_resp.json()["status"] == "executed"
    assert fake_razorpay == [commitment_id], "execute() must actually call the Razorpay client, not fake success"


def test_execute_fails_with_different_client_request_id_pins_original_bug(client):
    """This is exactly the bug from manual browser testing: verify with
    one id, then execute with a DIFFERENT, freshly generated id (what the
    old scenarios.js did). Must fail with the exact reported error, not
    silently succeed and not silently retry under a different identity."""
    intent_id, commitment_id = _setup_commitment(client)
    verify_req_id = _new_id("req")
    different_req_id = _new_id("req")  # simulates newRequestId() called again, the bug

    verify_resp = client.post(
        f"/intents/{intent_id}/commitments/{commitment_id}/verify", json=_verify_payload(verify_req_id)
    )
    assert verify_resp.json()["decision"]["outcome"] == "ALLOW"

    exec_resp = client.post(
        f"/intents/{intent_id}/commitments/{commitment_id}/execute", json={"client_request_id": different_req_id}
    )
    assert exec_resp.status_code == 400
    assert "no verified payment request found" in exec_resp.json()["detail"]


def test_execute_rejects_unknown_client_request_id(client):
    """An id that was never used in any /verify call at all cannot execute."""
    intent_id, commitment_id = _setup_commitment(client)
    client.post(f"/intents/{intent_id}/commitments/{commitment_id}/verify", json=_verify_payload(_new_id("req")))

    exec_resp = client.post(
        f"/intents/{intent_id}/commitments/{commitment_id}/execute",
        json={"client_request_id": "totally-made-up-id-nobody-verified"},
    )
    assert exec_resp.status_code == 400
    assert "no verified payment request found" in exec_resp.json()["detail"]


def test_verified_request_executes_exactly_once(client, fake_razorpay):
    """Re-using the same (id, commitment) pair for a second /execute call
    must be rejected — not silently retried, not double-executed."""
    intent_id, commitment_id = _setup_commitment(client)
    req_id = _new_id("req")
    client.post(f"/intents/{intent_id}/commitments/{commitment_id}/verify", json=_verify_payload(req_id))

    first = client.post(f"/intents/{intent_id}/commitments/{commitment_id}/execute", json={"client_request_id": req_id})
    assert first.status_code == 200

    second = client.post(f"/intents/{intent_id}/commitments/{commitment_id}/execute", json={"client_request_id": req_id})
    assert second.status_code == 409
    assert len(fake_razorpay) == 1, "Razorpay must only be called once for this commitment"


def test_replay_cannot_execute_through_the_new_fix(client):
    """After a real execution, re-verifying the SAME commitment (a fresh
    client_request_id, as a genuine replay presentation would use) must
    return BLOCK — and that new id must not be executable, since its
    stored decision is BLOCK, not ALLOW. Proves the client_request_id
    fix doesn't accidentally open a replay path."""
    intent_id, commitment_id = _setup_commitment(client)
    first_req_id = _new_id("req")
    client.post(f"/intents/{intent_id}/commitments/{commitment_id}/verify", json=_verify_payload(first_req_id))
    exec_resp = client.post(
        f"/intents/{intent_id}/commitments/{commitment_id}/execute", json={"client_request_id": first_req_id}
    )
    assert exec_resp.status_code == 200

    replay_req_id = _new_id("req")
    replay_verify = client.post(
        f"/intents/{intent_id}/commitments/{commitment_id}/verify", json=_verify_payload(replay_req_id)
    )
    assert replay_verify.json()["decision"]["outcome"] == "BLOCK"

    replay_exec = client.post(
        f"/intents/{intent_id}/commitments/{commitment_id}/execute", json={"client_request_id": replay_req_id}
    )
    assert replay_exec.status_code == 409
    assert "not ALLOW" in replay_exec.json()["detail"]


def test_cannot_execute_using_another_commitments_verified_id(client):
    """A client_request_id verified against commitment A must not be
    usable to execute commitment B, even though both are otherwise
    legitimate, separately-ALLOWed transactions — no cross-session ID
    confusion."""
    intent_a, commitment_a = _setup_commitment(client)
    intent_b, commitment_b = _setup_commitment(client)

    shared_req_id = _new_id("req")
    verify_a = client.post(
        f"/intents/{intent_a}/commitments/{commitment_a}/verify", json=_verify_payload(shared_req_id)
    )
    assert verify_a.json()["decision"]["outcome"] == "ALLOW"

    # Attempt to execute commitment B using the id that was actually
    # verified against commitment A.
    cross_exec = client.post(
        f"/intents/{intent_b}/commitments/{commitment_b}/execute", json={"client_request_id": shared_req_id}
    )
    assert cross_exec.status_code == 400
    assert "no verified payment request found" in cross_exec.json()["detail"]
