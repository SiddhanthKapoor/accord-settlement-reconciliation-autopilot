"""
Reconciliation runs over uploaded data.

The flow a finance operator actually performs: create a run, upload the
files they have, confirm what each column means, execute, inspect. The
dataset-driven `/batch/run` path stays exactly as it was — it is how the
frozen evaluations reproduce — and both write into the same records,
review queue and audit tables, so an uploaded run is inspected with the
same screens and the same evidence as an evaluation run.

Column mapping is confirmed rather than assumed. Detection reports a
confidence per column and refuses to proceed while a required field is
unresolved. Silently mis-reading an amount column is the one failure this
product cannot afford.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from app.domain.models import PolicyConfig, ReconciliationRecord
from app.engine.batch import process_batch
from app.engine.matching import ReferenceIndex
from app.engine.semantic import get_semantic_verifier
from app.ingest.mapper import combine, map_rows
from app.ingest.schema import CANONICAL_FIELDS, SourceType, detect_schema, parse_csv
from app.ledger import audit, store

router = APIRouter()

# A single upload is held in memory to parse it, so it is bounded. Well
# above a realistic month of transactions, well below anything that
# threatens the process.
MAX_UPLOAD_BYTES = 32 * 1024 * 1024
PREVIEW_ROWS = 200


class CreateRunRequest(BaseModel):
    label: str | None = None


class MappingRequest(BaseModel):
    mapping: dict[str, str]
    source_type: str | None = None
    amount_scale: str | None = None


class ExecuteRequest(BaseModel):
    label: str | None = None


@router.post("/runs")
def create_run(body: CreateRunRequest):
    """An empty run, waiting for sources."""
    batch_id = f"run_{uuid.uuid4().hex[:10]}"
    label = body.label or f"Reconciliation {datetime.now(timezone.utc).strftime('%d %b %H:%M')}"
    store.create_batch(batch_id, label, "upload", 0)
    audit.append_event(
        transaction_id=batch_id, event_type="RUN_CREATED", prior_state=None, new_state="DRAFT",
        payload={"batch_id": batch_id, "label": label},
    )
    return {"run_id": batch_id, "label": label, "status": "DRAFT", "sources": []}


@router.get("/runs")
def list_runs(limit: int = 20):
    return {"runs": store.list_batches(limit=limit)}


@router.get("/runs/{run_id}")
def get_run(run_id: str):
    batch = store.get_batch(run_id)
    if not batch:
        raise HTTPException(404, "run not found")
    batch["outcome_counts"] = store.batch_outcome_counts(run_id)
    batch["sources"] = store.list_sources(run_id)
    return batch


@router.post("/runs/{run_id}/sources")
async def upload_source(
    run_id: str,
    file: UploadFile = File(...),
    source_type: str = Form("OTHER"),
):
    """Take a CSV, detect its schema, and report what needs confirming."""
    if not store.get_batch(run_id):
        raise HTTPException(404, "run not found")
    try:
        kind = SourceType(source_type)
    except ValueError:
        raise HTTPException(400, f"unknown source type '{source_type}'")

    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"file exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)}MB")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")

    columns, rows = parse_csv(text)
    if not columns:
        raise HTTPException(400, "no columns found — is this a CSV?")
    if not rows:
        raise HTTPException(400, "file has headers but no rows")

    detected = detect_schema(columns, rows)
    source_id = f"src_{uuid.uuid4().hex[:10]}"
    detection = {
        "columns": detected.columns,
        "row_count": detected.row_count,
        "amount_scale": detected.amount_scale,
        "unmapped_required": detected.unmapped_required,
        "debit_column": detected.debit_column,
        "credit_column": detected.credit_column,
        "sample_rows": detected.sample_rows,
        "guesses": [
            {"column": g.column, "canonical": g.canonical, "confidence": g.confidence,
             "reason": g.reason, "samples": g.samples}
            for g in detected.guesses
        ],
    }
    store.save_source(source_id, run_id, file.filename or "upload.csv", kind.value, kind.role,
                      detected.row_count, detected.mapping, detection, text)
    audit.append_event(
        transaction_id=run_id, event_type="SOURCE_UPLOADED", prior_state="DRAFT", new_state="DRAFT",
        payload={"batch_id": run_id, "source_id": source_id, "filename": file.filename,
                 "source_type": kind.value, "role": kind.role, "rows": detected.row_count,
                 "unmapped_required": detected.unmapped_required},
    )
    return {
        "source_id": source_id, "filename": file.filename, "source_type": kind.value,
        "role": kind.role, "canonical_fields": list(CANONICAL_FIELDS),
        "mapping": detected.mapping, "detection": detection,
        "needs_user_input": detected.needs_user_input,
    }


@router.put("/runs/{run_id}/sources/{source_id}/mapping")
def update_mapping(run_id: str, source_id: str, body: MappingRequest):
    """Correct what detection guessed.

    Re-parsed against the stored file so the preview reflects the user's
    mapping rather than the original guess.
    """
    source = store.get_source(source_id)
    if not source or source["batch_id"] != run_id:
        raise HTTPException(404, "source not found in this run")

    unknown = [f for f in body.mapping if f not in CANONICAL_FIELDS]
    if unknown:
        raise HTTPException(400, f"unknown canonical field(s): {', '.join(unknown)}")

    columns = source["detection"]["columns"]
    bad = [c for c in body.mapping.values() if c and c not in columns]
    if bad:
        raise HTTPException(400, f"column(s) not present in this file: {', '.join(bad)}")

    kind = SourceType(body.source_type) if body.source_type else SourceType(source["source_type"])
    mapping = {k: v for k, v in body.mapping.items() if v}
    store.update_source_mapping(source_id, mapping, kind.value, kind.role)

    detection = source["detection"]
    if body.amount_scale in ("major", "minor"):
        detection["amount_scale"] = body.amount_scale
    detection["unmapped_required"] = [f for f in ("amount", "date") if f not in mapping]
    store.save_source(source_id, run_id, source["filename"], kind.value, kind.role,
                      source["row_count"], mapping, detection, source["raw_csv"])
    return {"source_id": source_id, "mapping": mapping, "source_type": kind.value,
            "role": kind.role, "unmapped_required": detection["unmapped_required"]}


@router.delete("/runs/{run_id}/sources/{source_id}")
def remove_source(run_id: str, source_id: str):
    source = store.get_source(source_id)
    if not source or source["batch_id"] != run_id:
        raise HTTPException(404, "source not found in this run")
    store.delete_source(source_id)
    return {"source_id": source_id, "removed": True}


@router.post("/runs/{run_id}/execute")
def execute_run(run_id: str, body: ExecuteRequest):
    """Map every source, then reconcile.

    Refuses rather than guesses when a run cannot produce two sides: a
    reconciliation with nothing to reconcile against would return a page
    of exceptions that say more about the upload than about the books.
    """
    batch = store.get_batch(run_id)
    if not batch:
        raise HTTPException(404, "run not found")
    sources = store.list_sources(run_id, include_raw=True)
    if not sources:
        raise HTTPException(400, "upload at least one source before running")

    blocked = [s["filename"] for s in sources if s["detection"].get("unmapped_required")]
    if blocked:
        raise HTTPException(400, f"map the required columns first for: {', '.join(blocked)}")

    mapped = []
    for source in sources:
        _, rows = parse_csv(source["raw_csv"])
        detection = source["detection"]
        result = map_rows(
            rows, source["mapping"], SourceType(source["source_type"]), source["source_id"],
            detection.get("amount_scale", "major"),
            detection.get("debit_column"), detection.get("credit_column"),
        )
        store.record_source_outcome(source["source_id"], result.accepted_count, len(result.rejected))
        mapped.append(result)

    ledger, settlements, rejected = combine(mapped)
    if not ledger:
        raise HTTPException(400, "no ledger-side records — add an orders or accounting source")
    if not settlements:
        raise HTTPException(400, "no settlement-side records — add a gateway or bank source")

    records = [ReconciliationRecord(record_id=r.order_id, merchant=r) for r in ledger]
    label = body.label or batch["label"]
    store.set_batch_total(run_id, len(records), label)
    audit.append_event(
        transaction_id=run_id, event_type="BATCH_STARTED", prior_state="DRAFT", new_state="RUNNING",
        payload={"batch_id": run_id, "dataset": "upload", "total": len(records),
                 "ledger_records": len(ledger), "settlement_records": len(settlements),
                 "rejected_rows": len(rejected), "sources": len(sources)},
    )

    def _run():
        index = ReferenceIndex(settlements)
        policy = PolicyConfig()
        verifier = get_semantic_verifier()

        def on_record(i, total, record, result):
            store.save_record(run_id, i, record, result, index.exact_candidates(record.merchant))
            store.increment_batch_progress(run_id)
            audit.append_event(
                transaction_id=record.record_id, event_type="RECORD_DECIDED",
                prior_state=None, new_state=result.outcome.value,
                payload={"batch_id": run_id, "seq": i, "total": total, "record_id": record.record_id,
                         "outcome": result.outcome.value, "reason": result.reason,
                         "ai_invoked": result.ai_invoked},
            )

        def on_revision(i, record, result):
            store.save_record(run_id, i, record, result, index.exact_candidates(record.merchant))
            audit.append_event(
                transaction_id=record.record_id, event_type="RECORD_REVISED",
                prior_state="RECONCILED", new_state=result.outcome.value,
                payload={"batch_id": run_id, "record_id": record.record_id,
                         "outcome": result.outcome.value, "reason": result.reason},
            )

        process_batch(records, settlements, policy=policy, semantic_verifier=verifier,
                      on_record=on_record, on_revision=on_revision)
        store.mark_batch_complete(run_id)
        audit.append_event(
            transaction_id=run_id, event_type="BATCH_COMPLETED", prior_state="RUNNING",
            new_state="COMPLETED", payload={"batch_id": run_id, "total": len(records)},
        )

    threading.Thread(target=_run, daemon=True).start()
    return {
        "run_id": run_id, "total_records": len(records), "label": label,
        "ledger_records": len(ledger), "settlement_records": len(settlements),
        "rejected_rows": rejected[:50], "rejected_count": len(rejected),
    }


@router.get("/runs/{run_id}/export")
def export_run(run_id: str, outcome: str | None = None):
    """Results as CSV, including the evidence behind each decision.

    Exported for a spreadsheet, which is where reconciliation output
    actually goes. The rejected-candidate reasoning travels with the row
    so a reviewer working offline still has the evidence.
    """
    import csv
    import io

    from fastapi.responses import StreamingResponse

    if not store.get_batch(run_id):
        raise HTTPException(404, "run not found")
    rows = store.list_records(run_id, outcome=outcome, limit=100_000)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        "record_id", "outcome", "exception_type", "severity", "matched_payment_id",
        "amount_minor", "currency", "reference", "description", "explanation",
        "recommended_action", "ai_invoked", "review_state", "rejected_candidates",
    ])
    for row in rows:
        merchant = json.loads(row["merchant_json"])
        considered = json.loads(row.get("considered_json") or "[]")
        rejected = "; ".join(
            f"{c['payment_id']} ({c['admissibility_reason']})"
            for c in considered if not c.get("admissible")
        )
        writer.writerow([
            row["record_id"], row["outcome"], row.get("exception_type") or "",
            row.get("severity") or "", row.get("matched_payment_id") or "",
            merchant.get("amount_minor"), merchant.get("currency"),
            merchant.get("reference_id") or "", merchant.get("description") or "",
            row.get("explanation") or "", row.get("recommended_action") or "",
            "yes" if row.get("ai_invoked") else "no", row.get("review_state") or "OPEN",
            rejected,
        ])

    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{run_id}-reconciliation.csv"'},
    )
