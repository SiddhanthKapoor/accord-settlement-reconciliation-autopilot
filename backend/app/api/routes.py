from __future__ import annotations

import asyncio
import json
import threading
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.domain.models import GroundTruth, MerchantRecord, PolicyConfig, ReconciliationOutcome, ReconciliationRecord, RazorpaySettlementRecord
from app.engine.batch import process_batch
from app.engine.matching import ReferenceIndex
from app.engine.semantic import get_semantic_verifier
from app.ledger import audit, store
from app.ledger.db import reset_db

router = APIRouter()

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "datasets"
EVAL_REPORTS_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "eval_reports"

_pool_cache: list[RazorpaySettlementRecord] | None = None


def _load_pool() -> list[RazorpaySettlementRecord]:
    global _pool_cache
    if _pool_cache is None:
        records = []
        with (DATA_DIR / "razorpay_pool.jsonl").open() as f:
            for line in f:
                records.append(RazorpaySettlementRecord.model_validate_json(line))
        _pool_cache = records
    return _pool_cache


def _load_split(name: str, limit: int | None = None) -> list[ReconciliationRecord]:
    records = []
    with (DATA_DIR / f"{name}.jsonl").open() as f:
        for line in f:
            row = json.loads(line)
            merchant = MerchantRecord.model_validate(row["merchant"])
            gt = GroundTruth(case=row["ground_truth_case"], expected_outcome=ReconciliationOutcome(row["ground_truth_outcome"]))
            records.append(ReconciliationRecord(record_id=row["record_id"], merchant=merchant, ground_truth=gt))
            if limit and len(records) >= limit:
                break
    return records


@router.get("/health")
def health():
    return {"status": "ok", "service": "reconciliation-autopilot"}


# ---------------------------------------------------------------------------
# Batch demo — real backend processing, real SSE progress
# ---------------------------------------------------------------------------

class RunBatchRequest(BaseModel):
    dataset: str = "holdout"  # "holdout" | "dev"
    limit: int | None = 500


@router.post("/batch/run")
def run_batch(body: RunBatchRequest):
    if body.dataset not in ("holdout", "dev"):
        raise HTTPException(400, "dataset must be 'holdout' or 'dev'")

    records = _load_split(body.dataset, limit=body.limit)
    pool = _load_pool()
    batch_id = f"batch_{uuid.uuid4().hex[:10]}"
    label = f"{body.dataset} batch ({len(records)} records)"

    store.create_batch(batch_id, label, body.dataset, len(records))
    audit.append_event(
        transaction_id=batch_id, event_type="BATCH_STARTED", prior_state=None, new_state="RUNNING",
        payload={"batch_id": batch_id, "dataset": body.dataset, "total": len(records)},
    )

    def _run():
        index = ReferenceIndex(pool)
        policy = PolicyConfig()
        semantic_verifier = get_semantic_verifier()

        def on_record(i, total, record, result):
            candidates = index.exact_candidates(record.merchant)
            store.save_record(batch_id, i, record, result, candidates)
            store.increment_batch_progress(batch_id)
            audit.append_event(
                transaction_id=record.record_id, event_type="RECORD_DECIDED",
                prior_state=None, new_state=result.outcome.value,
                payload={
                    "batch_id": batch_id, "seq": i, "total": total, "record_id": record.record_id,
                    "outcome": result.outcome.value, "reason": result.reason, "ai_invoked": result.ai_invoked,
                },
            )

        process_batch(records, pool, policy=policy, semantic_verifier=semantic_verifier, on_record=on_record)
        store.mark_batch_complete(batch_id)
        audit.append_event(
            transaction_id=batch_id, event_type="BATCH_COMPLETED", prior_state="RUNNING", new_state="COMPLETED",
            payload={"batch_id": batch_id, "total": len(records)},
        )

    threading.Thread(target=_run, daemon=True).start()
    return {"batch_id": batch_id, "total_records": len(records), "label": label}


@router.get("/batch/latest")
def get_latest_batch():
    batch = store.get_latest_batch()
    if not batch:
        return {"batch": None}
    batch["outcome_counts"] = store.batch_outcome_counts(batch["batch_id"])
    return {"batch": batch}


@router.get("/batch/{batch_id}")
def get_batch(batch_id: str):
    batch = store.get_batch(batch_id)
    if not batch:
        raise HTTPException(404, "batch not found")
    batch["outcome_counts"] = store.batch_outcome_counts(batch_id)
    return batch


@router.get("/batch/{batch_id}/records")
def list_batch_records(batch_id: str, outcome: str | None = None, limit: int = 200, offset: int = 0):
    return store.list_records(batch_id, outcome=outcome, limit=limit, offset=offset)


@router.get("/records/{record_id}")
def get_record(record_id: str):
    record = store.get_record(record_id)
    if not record:
        raise HTTPException(404, "record not found")
    record["merchant"] = json.loads(record.pop("merchant_json"))
    record["candidates"] = json.loads(record.pop("candidates_json"))
    record["checks"] = json.loads(record.pop("checks_json"))
    trail = audit.get_trail(record_id)
    for event in trail:
        event["payload"] = json.loads(event.pop("payload_json"))
    record["audit_trail"] = trail
    return record


# ---------------------------------------------------------------------------
# Evaluation report — read only what evaluate.py actually wrote
# ---------------------------------------------------------------------------

@router.get("/evaluation/latest")
def latest_evaluation(dataset: str = "holdout"):
    path = EVAL_REPORTS_DIR / f"latest_{dataset}.json"
    if not path.exists():
        raise HTTPException(404, f"no evaluation report found for '{dataset}' — run `python evaluate.py --dataset {dataset}` first")
    return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

@router.get("/audit/verify")
def verify_audit_chain():
    return audit.verify_chain()


@router.get("/audit/stream")
async def stream_audit(request: Request):
    async def event_gen():
        last_seq = 0
        while True:
            if await request.is_disconnected():
                break
            rows = audit.get_full_log()
            new_rows = [r for r in rows if r["seq"] > last_seq]
            for row in new_rows:
                last_seq = row["seq"]
                event = dict(row)
                event["payload"] = json.loads(event.pop("payload_json"))
                yield f"data: {json.dumps(event, default=str)}\n\n"
            await asyncio.sleep(0.1)

    return StreamingResponse(event_gen(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Dev/demo only
# ---------------------------------------------------------------------------

@router.post("/admin/reset")
def admin_reset():
    reset_db()
    return {"status": "reset"}
