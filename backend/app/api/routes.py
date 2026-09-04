from __future__ import annotations

import asyncio
import json
import threading
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

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


def _load_split(name: str, limit: int | None = None) -> tuple[list[ReconciliationRecord], list[str]]:
    """Load a split, returning the valid records and any rows that were
    rejected.

    A malformed row is skipped and reported rather than allowed to abort
    the whole batch: one bad line in a merchant export should not stop
    the other several thousand from being reconciled, and it must not be
    silently dropped either.
    """
    records: list[ReconciliationRecord] = []
    rejected: list[str] = []
    with (DATA_DIR / f"{name}.jsonl").open() as f:
        for line_number, line in enumerate(f, start=1):
            # `is not None`, not truthiness: limit=0 means an empty batch,
            # and treating it as "no limit" silently ran the entire
            # dataset instead of nothing.
            if limit is not None and len(records) >= limit:
                break
            try:
                row = json.loads(line)
                merchant = MerchantRecord.model_validate(row["merchant"])
                gt = GroundTruth(case=row["ground_truth_case"],
                                 expected_outcome=ReconciliationOutcome(row["ground_truth_outcome"]))
                records.append(ReconciliationRecord(record_id=row["record_id"], merchant=merchant, ground_truth=gt))
            except Exception as exc:  # noqa: BLE001 — any malformed row degrades the same way
                rejected.append(f"line {line_number}: {type(exc).__name__}: {exc}")
    return records, rejected


@router.get("/health")
def health():
    return {"status": "ok", "service": "reconciliation-autopilot"}


# ---------------------------------------------------------------------------
# Batch demo — real backend processing, real SSE progress
# ---------------------------------------------------------------------------

class RunBatchRequest(BaseModel):
    dataset: str = "holdout"  # "holdout" | "dev"
    limit: int | None = Field(default=500, ge=0, description="Records to process; 0 means none, omit for all.")


@router.post("/batch/run")
def run_batch(body: RunBatchRequest):
    if body.dataset not in ("holdout", "dev"):
        raise HTTPException(400, "dataset must be 'holdout' or 'dev'")

    records, rejected = _load_split(body.dataset, limit=body.limit)
    pool = _load_pool()
    batch_id = f"batch_{uuid.uuid4().hex[:10]}"
    label = f"{body.dataset} batch ({len(records)} records)"

    store.create_batch(batch_id, label, body.dataset, len(records))
    audit.append_event(
        transaction_id=batch_id, event_type="BATCH_STARTED", prior_state=None, new_state="RUNNING",
        payload={"batch_id": batch_id, "dataset": body.dataset, "total": len(records),
                 "rejected_rows": len(rejected)},
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
    return {"batch_id": batch_id, "total_records": len(records), "label": label,
            "rejected_rows": rejected}


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
def get_record(record_id: str, batch_id: str | None = None):
    record = store.get_record(record_id, batch_id)
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
# Data provenance — what is real and what is generated
# ---------------------------------------------------------------------------

@router.get("/data-sources")
def data_sources():
    """Which settlement source is in use and what each one can supply.
    Surfaced in the console so a viewer is never left to assume the
    numbers came from a live Razorpay account."""
    from app.integrations.settlement_source import describe_sources

    return describe_sources()


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

@router.get("/audit/log")
def audit_log(limit: int = 200, since: int = 0):
    """Historical audit events.

    History is a query, not a stream. The live stream deliberately starts
    at the current head so a newly-attached client is not sent the whole
    ledger, which means anything wanting existing events — the audit
    view on load — has to ask for them here and then tail the stream from
    the sequence this returns.
    """
    events = audit.get_events_since(since, limit=limit)
    for event in events:
        event["payload"] = json.loads(event.pop("payload_json"))
    return {"events": events, "head_seq": audit.head_seq()}


@router.get("/audit/verify")
def verify_audit_chain():
    return audit.verify_chain()


def resume_point(since: int | None, last_event_id: str | None, head: int) -> int:
    """Where an SSE client should resume from.

    Precedence: an explicit `?since=` wins, then the `Last-Event-ID`
    header a browser resends automatically after a dropped connection,
    then the current head. Falling back to the head rather than zero is
    deliberate — a client attaching mid-batch wants what happens next,
    not a replay of the entire ledger.

    Pulled out of the endpoint so it can be tested without opening a
    stream that never ends.
    """
    if since is not None:
        return max(since, 0)
    if last_event_id and last_event_id.strip().isdigit():
        return int(last_event_id.strip())
    return head


@router.get("/audit/stream")
async def stream_audit(request: Request, since: int | None = None):
    """Server-sent stream of audit events.

    Resumption is explicit: every frame carries its sequence number as
    the SSE event id, and a reconnecting client resumes from either the
    standard `Last-Event-ID` header (which browsers send automatically)
    or an explicit `?since=`. Without one, the stream starts at the
    current head rather than replaying the whole ledger — a client
    attaching mid-batch wants what happens next, and replaying fifty
    thousand historical events to every new tab is how a demo falls over
    in front of an audience.
    """
    last_seq = resume_point(since, request.headers.get("last-event-id"), audit.head_seq())

    async def event_gen():
        nonlocal last_seq
        idle_polls = 0
        while True:
            if await request.is_disconnected():
                break
            rows = audit.get_events_since(last_seq)
            for row in rows:
                last_seq = row["seq"]
                event = dict(row)
                event["payload"] = json.loads(event.pop("payload_json"))
                yield f"id: {last_seq}\ndata: {json.dumps(event, default=str)}\n\n"
            if rows:
                idle_polls = 0
            else:
                idle_polls += 1
                # A comment frame every ~15s keeps proxies and load
                # balancers from culling an idle connection, and lets the
                # client notice a dead link instead of waiting forever.
                if idle_polls >= 150:
                    idle_polls = 0
                    yield ": keepalive\n\n"
            await asyncio.sleep(0.1)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Dev/demo only
# ---------------------------------------------------------------------------

@router.post("/admin/reset")
def admin_reset():
    reset_db()
    return {"status": "reset"}
