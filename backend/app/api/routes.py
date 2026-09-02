from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.domain.canonical import commitment_content_hash
from app.domain.models import (
    CatalogEvidence,
    CheckStatus,
    Commitment,
    Constraints,
    DecisionOutcome,
    PaymentRequest,
    ProductRef,
    TransactionIntent,
)
from app.engine.checks import run_integrity_checks
from app.engine.decision import decide
from app.integrations import catalog_client, razorpay_client
from app.ledger import audit, store

router = APIRouter()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


# ---------------------------------------------------------------------------
# Step A — declare intent / constraints
# ---------------------------------------------------------------------------

class CreateIntentRequest(BaseModel):
    constraints: Constraints
    declared_natural_language: str | None = None


@router.post("/intents")
def create_intent(body: CreateIntentRequest):
    intent = TransactionIntent(
        intent_id=new_id("intent"),
        constraints=body.constraints,
        declared_natural_language=body.declared_natural_language,
    )
    store.create_intent(intent)
    audit.append_event(
        transaction_id=intent.intent_id,
        event_type="INTENT_DECLARED",
        prior_state=None,
        new_state="DECLARED",
        payload=json.loads(intent.model_dump_json()),
    )
    return intent


@router.get("/intents/{intent_id}")
def get_intent(intent_id: str):
    row = store.get_intent(intent_id)
    if not row:
        raise HTTPException(404, "intent not found")
    return {
        "intent_id": row["intent_id"],
        "constraints": json.loads(row["constraints_json"]),
        "budget_remaining_minor": row["budget_remaining_minor"],
        "budget_reserved": bool(row["budget_reserved"]),
        "budget_reserved_by": row["budget_reserved_by"],
        "created_at": row["created_at"],
    }


# ---------------------------------------------------------------------------
# Step B — record catalog evidence (independently fetched, not agent-claimed)
# ---------------------------------------------------------------------------

class RecordEvidenceRequest(BaseModel):
    merchant_id: str
    product_id: str
    stage: str = "DISCOVERED"  # DISCOVERED | SELECTED


@router.post("/intents/{intent_id}/evidence")
def record_evidence(intent_id: str, body: RecordEvidenceRequest):
    intent_row = store.get_intent(intent_id)
    if not intent_row:
        raise HTTPException(404, "intent not found")

    try:
        product: ProductRef = catalog_client.fetch_ground_truth(body.merchant_id, body.product_id)
    except catalog_client.ProductNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except catalog_client.CatalogUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc

    evidence = CatalogEvidence(
        evidence_id=new_id("evidence"),
        intent_id=intent_id,
        product=product,
        source=f"catalog_service:/merchants/{body.merchant_id}/products/{body.product_id}",
    )
    store.save_evidence(evidence)
    audit.append_event(
        transaction_id=intent_id,
        event_type="CATALOG_EVIDENCE_RECORDED",
        prior_state=None,
        new_state=body.stage,
        evidence_ref=evidence.evidence_id,
        payload=json.loads(evidence.model_dump_json()),
    )
    return evidence


# ---------------------------------------------------------------------------
# Step C — create a commitment (canonical, hashed, reserves budget)
# ---------------------------------------------------------------------------

class CreateCommitmentRequest(BaseModel):
    evidence_id: str
    quantity: int


@router.post("/intents/{intent_id}/commitments")
def create_commitment(intent_id: str, body: CreateCommitmentRequest):
    intent_row = store.get_intent(intent_id)
    if not intent_row:
        raise HTTPException(404, "intent not found")
    evidence_row = store.get_evidence(body.evidence_id)
    if not evidence_row or evidence_row["intent_id"] != intent_id:
        raise HTTPException(404, "evidence not found for this intent")

    product = ProductRef.model_validate_json(evidence_row["product_json"])
    content_hash = commitment_content_hash(
        merchant_id=product.merchant_id,
        product_id=product.product_id,
        category=product.category,
        quantity=body.quantity,
        price_minor=product.price_minor,
        currency=product.currency,
    )
    commitment = Commitment(
        commitment_id=new_id("commit"),
        intent_id=intent_id,
        merchant_id=product.merchant_id,
        product_id=product.product_id,
        product_name=product.name,
        category=product.category,
        quantity=body.quantity,
        price_minor=product.price_minor,
        currency=product.currency,
        evidence_id=body.evidence_id,
        content_hash=content_hash,
    )

    total_minor = product.price_minor * body.quantity
    reserved = store.reserve_budget(intent_id, commitment.commitment_id, total_minor)
    state = "CHECKOUT_READY" if reserved else "BLOCKED"
    store.save_commitment(commitment, state=state)

    audit.append_event(
        transaction_id=commitment.commitment_id,
        event_type="COMMITMENT_CREATED" if reserved else "BUDGET_RESERVATION_FAILED",
        prior_state="SELECTED",
        new_state=state,
        evidence_ref=body.evidence_id,
        payload={
            **json.loads(commitment.model_dump_json()),
            "budget_reserved": reserved,
            "reason": None if reserved else (
                "Budget already reserved by another commitment under this intent, or insufficient "
                "remaining budget — this is the shared-budget race guard (AP2 threat T-33)."
            ),
        },
    )
    return {"commitment": commitment, "budget_reserved": reserved}


# ---------------------------------------------------------------------------
# Step D/E — payment request (observed final state) -> integrity verification
# ---------------------------------------------------------------------------

class VerifyPaymentRequest(BaseModel):
    client_request_id: str
    merchant_id: str
    product_id: str
    product_name: str
    category: str
    quantity: int
    price_minor: int
    currency: str = "INR"


@router.post("/intents/{intent_id}/commitments/{commitment_id}/verify")
def verify(intent_id: str, commitment_id: str, body: VerifyPaymentRequest):
    intent_row = store.get_intent(intent_id)
    commitment_row = store.get_commitment(commitment_id)
    if not intent_row or not commitment_row or commitment_row["intent_id"] != intent_id:
        raise HTTPException(404, "intent/commitment not found")

    existing = store.get_payment_request(body.client_request_id)
    if existing and existing["commitment_id"] == commitment_id:
        return {"decision": json.loads(existing["decision_json"]), "idempotent_replay_of_request": True}

    constraints = store.get_constraints(intent_id)
    commitment = Commitment(
        commitment_id=commitment_row["commitment_id"],
        intent_id=commitment_row["intent_id"],
        merchant_id=commitment_row["merchant_id"],
        product_id=commitment_row["product_id"],
        product_name=commitment_row["product_name"],
        category=commitment_row["category"],
        quantity=commitment_row["quantity"],
        price_minor=commitment_row["price_minor"],
        currency=commitment_row["currency"],
        evidence_id=commitment_row["evidence_id"],
        created_at=datetime.fromisoformat(commitment_row["created_at"]),
        version=commitment_row["version"],
        content_hash=commitment_row["content_hash"],
    )
    payment_request = PaymentRequest(
        transaction_id=commitment_id,
        commitment_id=commitment_id,
        merchant_id=body.merchant_id,
        product_id=body.product_id,
        product_name=body.product_name,
        category=body.category,
        quantity=body.quantity,
        price_minor=body.price_minor,
        currency=body.currency,
        client_request_id=body.client_request_id,
    )

    audit.append_event(
        transaction_id=commitment_id,
        event_type="PAYMENT_REQUEST_RECEIVED",
        prior_state=commitment_row["state"],
        new_state="PAYMENT_REQUESTED",
        payload=json.loads(payment_request.model_dump_json()),
    )

    checks = run_integrity_checks(intent_row, constraints, commitment, payment_request)
    decision = decide(commitment_id, checks)

    for check in checks:
        audit.append_event(
            transaction_id=commitment_id,
            event_type="CHECK_EXECUTED",
            prior_state=None,
            new_state=None,
            payload=json.loads(check.model_dump_json()),
        )

    new_state = {
        DecisionOutcome.ALLOW: "ALLOWED",
        DecisionOutcome.BLOCK: "BLOCKED",
        DecisionOutcome.REQUIRE_RECONFIRMATION: "REQUIRES_RECONFIRMATION",
    }[decision.outcome]
    store.update_commitment_state(commitment_id, new_state)

    if decision.outcome == DecisionOutcome.BLOCK and commitment_row["state"] not in ("BLOCKED", "EXECUTED"):
        total_minor = commitment_row["price_minor"] * commitment_row["quantity"]
        store.release_budget(intent_id, total_minor)

    audit.append_event(
        transaction_id=commitment_id,
        event_type="DECISION",
        prior_state="PAYMENT_REQUESTED",
        new_state=new_state,
        payload=json.loads(decision.model_dump_json()),
    )

    store.save_payment_request(body.client_request_id, commitment_id, commitment_id, decision.model_dump_json())

    return {"decision": decision, "idempotent_replay_of_request": False}


# ---------------------------------------------------------------------------
# Execute — the only path that can call Razorpay, and only after ALLOW
# ---------------------------------------------------------------------------

class ExecuteRequest(BaseModel):
    client_request_id: str


@router.post("/intents/{intent_id}/commitments/{commitment_id}/execute")
def execute(intent_id: str, commitment_id: str, body: ExecuteRequest):
    commitment_row = store.get_commitment(commitment_id)
    if not commitment_row:
        raise HTTPException(404, "commitment not found")

    prior_request = store.get_payment_request(body.client_request_id)
    if not prior_request or prior_request["commitment_id"] != commitment_id:
        raise HTTPException(400, "no verified payment request found for this client_request_id")
    decision = json.loads(prior_request["decision_json"])
    if decision["outcome"] != DecisionOutcome.ALLOW.value:
        raise HTTPException(409, f"cannot execute: last decision was {decision['outcome']}, not ALLOW")

    consumed = store.consume_commitment(commitment_id, commitment_id)
    if not consumed:
        audit.append_event(
            transaction_id=commitment_id,
            event_type="EXECUTION_BLOCKED_RACE",
            prior_state="ALLOWED",
            new_state="BLOCKED",
            payload={"reason": "Lost a race to a concurrent /execute call for the same commitment (T-31)."},
        )
        raise HTTPException(409, "commitment already consumed by a concurrent execution — replay/race rejected")

    commitment = Commitment(
        commitment_id=commitment_row["commitment_id"],
        intent_id=commitment_row["intent_id"],
        merchant_id=commitment_row["merchant_id"],
        product_id=commitment_row["product_id"],
        product_name=commitment_row["product_name"],
        category=commitment_row["category"],
        quantity=commitment_row["quantity"],
        price_minor=commitment_row["price_minor"],
        currency=commitment_row["currency"],
        evidence_id=commitment_row["evidence_id"],
        created_at=datetime.fromisoformat(commitment_row["created_at"]),
        version=commitment_row["version"],
        content_hash=commitment_row["content_hash"],
    )

    try:
        response = razorpay_client.execute_payment_link(commitment, commitment_id)
    except razorpay_client.RazorpayNotConfigured as exc:
        # Missing credentials are an environment fact, not a reason to
        # pretend this commitment is still spendable: it has genuinely
        # been consumed (the atomic guard above already fired), so a
        # second attempt against it is a real replay and must still be
        # rejected. We record a clearly-labeled SIMULATED execution
        # instead of a fabricated Razorpay response — the system
        # demonstrates the full lifecycle including replay-after-
        # execution without requiring external credentials, but never
        # claims a payment happened when it didn't.
        store.save_execution(
            commitment_id, order_id=None, payment_link_id=None, payment_link_url=None,
            status="simulated", raw_response={"simulated": True, "reason": str(exc)},
        )
        store.update_commitment_state(commitment_id, "EXECUTED")
        audit.append_event(
            transaction_id=commitment_id,
            event_type="PAYMENT_EXECUTION_SIMULATED",
            prior_state="ALLOWED",
            new_state="EXECUTED",
            payload={"simulated": True, "reason": str(exc)},
        )
        return {"status": "simulated", "simulated": True, "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001 — surface real Razorpay errors verbatim, do not swallow
        # An actual API failure (bad credentials, network error, Razorpay
        # outage) is a transient/environment failure, not a security
        # event — roll back the consumption so a legitimate retry isn't
        # permanently blocked by our own replay guard.
        store.unconsume_commitment(commitment_id)
        audit.append_event(
            transaction_id=commitment_id,
            event_type="EXECUTION_FAILED",
            prior_state="ALLOWED",
            new_state="FAILED",
            payload={"error": str(exc)},
        )
        store.update_commitment_state(commitment_id, "FAILED")
        raise HTTPException(502, f"razorpay execution failed: {exc}") from exc

    store.save_execution(
        commitment_id,
        order_id=response.get("order_id"),
        payment_link_id=response.get("id"),
        payment_link_url=response.get("short_url"),
        status=response.get("status", "created"),
        raw_response=response,
    )
    store.update_commitment_state(commitment_id, "EXECUTED")
    audit.append_event(
        transaction_id=commitment_id,
        event_type="PAYMENT_EXECUTED",
        prior_state="ALLOWED",
        new_state="EXECUTED",
        payload={"payment_link_url": response.get("short_url"), "payment_link_id": response.get("id")},
    )
    return {"status": "executed", "simulated": False, "razorpay": response}


# ---------------------------------------------------------------------------
# Live session stats — Overview screen. Real counts from the ledger only;
# this is demo/session data (see README), never fabricated figures.
# ---------------------------------------------------------------------------

@router.get("/stats")
def get_stats():
    import os

    counts = store.get_state_counts()
    chain = audit.verify_chain()
    return {
        "counts": {
            "total": sum(counts.values()),
            "allowed": counts.get("ALLOWED", 0) + counts.get("EXECUTED", 0),
            "blocked": counts.get("BLOCKED", 0),
            "requires_reconfirmation": counts.get("REQUIRES_RECONFIRMATION", 0),
            "executed": counts.get("EXECUTED", 0),
        },
        "chain": {"intact": chain["intact"], "total_events": chain["total_events"]},
        "semantic_provider": (
            "gemini" if os.environ.get("GEMINI_API_KEY")
            else "anthropic" if os.environ.get("ANTHROPIC_API_KEY")
            else "heuristic-fallback"
        ),
        "razorpay_configured": bool(os.environ.get("RAZORPAY_KEY_ID") and os.environ.get("RAZORPAY_KEY_SECRET")),
    }


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

@router.get("/transactions/{transaction_id}/audit")
def get_audit(transaction_id: str):
    return audit.get_trail(transaction_id)


@router.get("/audit/verify")
def verify_audit_chain():
    return audit.verify_chain()


@router.get("/audit/stream")
async def stream_audit(request: Request):
    async def event_gen():
        last_seq = 0
        while True:
            # Without this check, a client that never sends a clean close
            # (e.g. a backgrounded browser tab) keeps this generator
            # looping forever, which blocks uvicorn's graceful shutdown
            # (and therefore --reload) waiting for the connection to end.
            if await request.is_disconnected():
                break
            rows = audit.get_full_log()
            new_rows = [r for r in rows if r["seq"] > last_seq]
            for row in new_rows:
                last_seq = row["seq"]
                # The DB column is payload_json (a JSON-encoded string, so
                # it round-trips through SQLite cleanly). Consumers of
                # this stream expect a real object at `payload`, not a
                # string they must know to re-parse — decode it here once,
                # at the one place that serializes rows onto the wire.
                event = dict(row)
                event["payload"] = json.loads(event.pop("payload_json"))
                yield f"data: {json.dumps(event, default=str)}\n\n"
            # 100ms: fast enough that ScenariosView's live per-check
            # rendering (see CHECK_EXECUTED handling client-side) reads as
            # a real-time feed rather than a batched refresh, while a
            # verify() request runs concurrently in its own worker thread.
            await asyncio.sleep(0.1)

    return StreamingResponse(event_gen(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Dev/eval only
# ---------------------------------------------------------------------------

@router.post("/admin/reset")
def admin_reset():
    from app.ledger.db import reset_db

    reset_db()
    return {"status": "reset"}
