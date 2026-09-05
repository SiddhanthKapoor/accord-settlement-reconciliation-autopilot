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

import hashlib
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
from app.ingest import flow
from app.ingest.classify import classify_source
from app.ingest.mapper import combine, combine_provenance, map_rows
from app.ingest.reader import UnreadableFile, read_table
from app.ingest.schema import CANONICAL_FIELDS, SourceType, detect_schema, parse_csv
from app.ledger import audit, store

router = APIRouter()

# A single upload is held in memory to parse it, so it is bounded. Well
# above a realistic month of transactions, well below anything that
# threatens the process.
MAX_UPLOAD_BYTES = 32 * 1024 * 1024

# One workspace's sources all live in memory during a run, so the
# per-file cap is not enough on its own — fifty files each just under the
# per-file limit is a different problem from one oversized file.
MAX_WORKSPACE_BYTES = 128 * 1024 * 1024
MAX_SOURCES_PER_RUN = 50

# Identifier values kept per source so the plan can propose relationships
# on observed overlap rather than on filenames. Sampled, and labelled as
# sampled everywhere it is used.
IDENTIFIER_SAMPLE_SIZE = 200
IDENTIFIER_SAMPLE_ROWS = 2_000

PREVIEW_ROWS = 200

# `source_type` may be omitted entirely or sent as this sentinel; either
# means "work it out from the file".
AUTO = "AUTO"


class CreateRunRequest(BaseModel):
    label: str | None = None


class MappingRequest(BaseModel):
    mapping: dict[str, str]
    source_type: str | None = None
    amount_scale: str | None = None


class ExecuteRequest(BaseModel):
    label: str | None = None


class PlanSourceUpdate(BaseModel):
    """A user's answer to "what is this file, and which side is it on?"."""

    source_id: str
    source_type: str | None = None
    role: str | None = None
    confirmed: bool = True
    note: str | None = None


class PlanRelationshipUpdate(BaseModel):
    from_source_id: str
    to_source_id: str
    confirmed: bool = True
    note: str | None = None


class PlanRequest(BaseModel):
    sources: list[PlanSourceUpdate] = []
    relationships: list[PlanRelationshipUpdate] = []
    confirmed: bool | None = None


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


class _FileRejected(Exception):
    """One file in a multi-file upload could not be ingested."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _identifier_sample(rows: list[dict], mapping: dict[str, str]) -> list[str]:
    """A bounded sample of this file's cross-system identifiers.

    Used only to propose relationships between files on *observed*
    overlap. It is a sample, so its absence proves nothing and every
    place it is reported says so.
    """
    columns = [mapping.get(c) for c in ("reference", "transaction_id")]
    columns = [c for c in columns if c]
    if not columns:
        return []
    seen: list[str] = []
    unique: set[str] = set()
    for row in rows[:IDENTIFIER_SAMPLE_ROWS]:
        for column in columns:
            value = str(row.get(column, "") or "").strip().upper()
            if value and value not in unique:
                unique.add(value)
                seen.append(value)
                if len(seen) >= IDENTIFIER_SAMPLE_SIZE:
                    return seen
    return seen


def _ingest_file(run_id: str, filename: str, raw: bytes, requested_type: str | None) -> dict:
    """One uploaded file: read it, classify it, store it, report it.

    Raises `_FileRejected` rather than an HTTPException so a bad file in
    a batch of twelve fails on its own line instead of taking the other
    eleven with it. A single-file upload turns the same failure back into
    the HTTP error it always was.
    """
    if len(raw) > MAX_UPLOAD_BYTES:
        raise _FileRejected(413, f"file exceeds {MAX_UPLOAD_BYTES // (1024 * 1024)}MB")

    try:
        read = read_table(filename, raw)
    except UnreadableFile as exc:
        raise _FileRejected(400, str(exc)) from exc

    if not read.columns:
        raise _FileRejected(400, "no columns found — is this a CSV?")
    if not read.rows:
        raise _FileRejected(400, "file has headers but no rows")

    detected = detect_schema(read.columns, read.rows)
    classification = classify_source(filename, read.columns, read.rows, detected)

    # An explicitly declared type is a statement by the person uploading,
    # so it stands and needs no confirmation. AUTO means the classifier
    # decides — and then a low-confidence answer must be confirmed before
    # it can be reconciled on.
    explicit = bool(requested_type) and requested_type.upper() != AUTO
    if explicit:
        kind = SourceType(requested_type)                   # validated by the caller
        role_confirmed = True
    else:
        kind = classification.source_type
        role_confirmed = not classification.needs_confirmation

    content_hash = hashlib.sha256(raw).hexdigest()
    duplicate = store.find_source_by_hash(run_id, content_hash)

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
        "classification": classification.to_dict(),
        "identifier_sample": _identifier_sample(read.rows, detected.mapping),
        "role_confirmed": role_confirmed,
        "role_declared_by": "user" if explicit else "detection",
        "format": read.fmt,
        "header_row": read.header_row or 1,
        "sheet_name": read.sheet_name,
        "truncated": read.truncated,
        "read_notes": read.notes,
        "content_hash": content_hash,
        "duplicate_of": duplicate["source_id"] if duplicate else None,
    }

    store.save_source(source_id, run_id, filename, kind.value, kind.role,
                      detected.row_count, detected.mapping, detection, read.csv_text,
                      content_hash=content_hash)
    audit.append_event(
        transaction_id=run_id, event_type="SOURCE_UPLOADED", prior_state="DRAFT", new_state="DRAFT",
        payload={"batch_id": run_id, "source_id": source_id, "filename": filename,
                 "source_type": kind.value, "role": kind.role, "rows": detected.row_count,
                 "unmapped_required": detected.unmapped_required,
                 "detected_source_type": classification.source_type.value,
                 "detection_confidence": round(classification.confidence, 3),
                 "provider": classification.provider,
                 "role_declared_by": detection["role_declared_by"],
                 "duplicate_of": detection["duplicate_of"],
                 "content_hash": content_hash},
    )

    result = {
        "ok": True,
        "source_id": source_id,
        "filename": filename,
        "source_type": kind.value,
        "role": kind.role,
        "canonical_fields": list(CANONICAL_FIELDS),
        "mapping": detected.mapping,
        "detection": detection,
        "needs_user_input": detected.needs_user_input,
        "role_confirmed": role_confirmed,
        "duplicate_of": detection["duplicate_of"],
        "duplicate_of_filename": duplicate["filename"] if duplicate else None,
        "format": read.fmt,
        "truncated": read.truncated,
        "read_notes": read.notes,
    }
    result.update(classification.to_dict())
    # The declared type wins over the detected one in the top-level
    # `source_type`; both are reported so a user can see they differ.
    result["source_type"] = kind.value
    result["role"] = kind.role
    return result


@router.post("/runs/{run_id}/sources")
async def upload_sources(
    run_id: str,
    files: list[UploadFile] = File(default=[]),
    file: list[UploadFile] = File(default=[]),
    source_type: str | None = Form(default=None),
    source_types: list[str] = Form(default=[]),
):
    """Take any number of files, work out what each one is, and report it.

    Both field names are supported: `files` (one or many) and `file`
    (what the single-file callers already send). A list of one is the
    same thing as one, so a single-file upload still gets its result at
    the top level of the response as well as inside `sources`.

    `source_type` is optional. Omitted — or sent as "AUTO" — means the
    file is classified from its contents, and a low-confidence answer
    comes back with `needs_confirmation`, which blocks execution until a
    person confirms it. `source_types` may carry one declared type per
    file, in upload order.
    """
    if not store.get_batch(run_id):
        raise HTTPException(404, "run not found")

    uploads = list(files) + list(file)
    if not uploads:
        raise HTTPException(400, "send at least one file, as 'files' (one or many) or 'file'")

    declared: list[str | None] = list(source_types) if source_types else []
    if len(declared) not in (0, len(uploads)):
        raise HTTPException(400, f"{len(declared)} source_types for {len(uploads)} files — send one per file, or none")

    def _validate(value: str | None) -> str | None:
        if value is None or value.upper() == AUTO:
            return None
        try:
            SourceType(value)
        except ValueError:
            raise HTTPException(400, f"unknown source type '{value}'") from None
        return value

    fallback = _validate(source_type)
    per_file = [_validate(v) for v in declared] if declared else [fallback] * len(uploads)

    existing = store.count_sources(run_id)
    if existing + len(uploads) > MAX_SOURCES_PER_RUN:
        raise HTTPException(
            400,
            f"this workspace holds {existing} file(s) and {len(uploads)} more were sent, over the "
            f"limit of {MAX_SOURCES_PER_RUN}. Remove sources, or split the reconciliation into "
            f"separate runs.",
        )

    used = store.workspace_bytes(run_id)
    results: list[dict] = []
    errors: list[dict] = []

    for index, upload in enumerate(uploads):
        filename = upload.filename or f"upload-{index + 1}.csv"
        try:
            raw = await upload.read()
            if used + len(raw) > MAX_WORKSPACE_BYTES:
                raise _FileRejected(
                    413,
                    f"this workspace would exceed its total size limit of "
                    f"{MAX_WORKSPACE_BYTES // (1024 * 1024)}MB",
                )
            result = _ingest_file(run_id, filename, raw, per_file[index])
            used += len(raw)
            results.append(result)
        except _FileRejected as exc:
            errors.append({"ok": False, "filename": filename, "status": exc.status, "error": exc.detail})

    # A single file that failed is the error it always was; a batch where
    # everything failed is reported the same way rather than as an empty
    # success.
    if errors and not results:
        raise HTTPException(errors[0]["status"], errors[0]["error"])

    response = {
        "run_id": run_id,
        "count": len(results),
        "sources": results,
        "errors": errors,
        "file_count": store.count_sources(run_id),
        "max_files": MAX_SOURCES_PER_RUN,
    }
    if len(uploads) == 1 and results:
        # Backwards compatibility: the single-file shape is preserved for
        # existing callers, which read source_id/mapping/detection at the
        # top level.
        response = {**results[0], **response}
    return response


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
    if body.source_type:
        # Stating the type here is the confirmation the classifier asked
        # for, so the run is no longer blocked on it.
        detection["role_confirmed"] = True
        detection["role_declared_by"] = "user"
    store.save_source(source_id, run_id, source["filename"], kind.value, kind.role,
                      source["row_count"], mapping, detection, source["raw_csv"],
                      content_hash=source.get("content_hash"))
    return {"source_id": source_id, "mapping": mapping, "source_type": kind.value,
            "role": kind.role, "unmapped_required": detection["unmapped_required"],
            "role_confirmed": bool(detection.get("role_confirmed"))}


@router.delete("/runs/{run_id}/sources/{source_id}")
def remove_source(run_id: str, source_id: str):
    source = store.get_source(source_id)
    if not source or source["batch_id"] != run_id:
        raise HTTPException(404, "source not found in this run")
    store.delete_source(source_id)
    return {"source_id": source_id, "removed": True}


@router.get("/runs/{run_id}/plan")
def get_plan(run_id: str):
    """The workspace as a money-flow map, gaps included.

    Read the module docstring in `app/ingest/flow.py` before wiring this
    into a diagram: the stages are a plan and a provenance view over a
    two-sided engine, not evidence of hop-by-hop matching.
    """
    batch = store.get_batch(run_id)
    if not batch:
        raise HTTPException(404, "run not found")
    sources = store.list_sources(run_id)
    records = store.list_record_provenance(run_id)
    return flow.build_plan(batch, sources, saved_plan=store.get_run_plan(run_id), records=records)


@router.put("/runs/{run_id}/plan")
def update_plan(run_id: str, body: PlanRequest):
    """Confirm or correct the plan: what each file is, and which pairs relate.

    Confirming a source's role is the gate the classifier defers to — a
    file it was not sure about cannot be reconciled on until this has
    been answered.
    """
    batch = store.get_batch(run_id)
    if not batch:
        raise HTTPException(404, "run not found")

    by_id = {s["source_id"]: s for s in store.list_sources(run_id, include_raw=True)}
    unknown = [u.source_id for u in body.sources if u.source_id not in by_id]
    unknown += [
        sid for r in body.relationships for sid in (r.from_source_id, r.to_source_id)
        if sid not in by_id
    ]
    if unknown:
        raise HTTPException(404, f"source(s) not in this run: {', '.join(sorted(set(unknown)))}")

    saved = store.get_run_plan(run_id)
    saved_sources = dict(saved.get("sources") or {})
    saved_relationships = dict(saved.get("relationships") or {})

    for update in body.sources:
        source = by_id[update.source_id]
        detection = source["detection"]
        kind = SourceType(source["source_type"])
        if update.source_type:
            try:
                kind = SourceType(update.source_type)
            except ValueError:
                raise HTTPException(400, f"unknown source type '{update.source_type}'") from None
        role = update.role or kind.role
        if role not in ("LEDGER", "SETTLEMENT"):
            raise HTTPException(400, f"role must be LEDGER or SETTLEMENT, got '{role}'")
        detection["role_confirmed"] = bool(update.confirmed)
        detection["role_declared_by"] = "user" if update.confirmed else "detection"
        store.save_source(source["source_id"], run_id, source["filename"], kind.value, role,
                          source["row_count"], source["mapping"], detection, source["raw_csv"],
                          content_hash=source.get("content_hash"))
        saved_sources[update.source_id] = {
            "source_type": kind.value, "role": role, "confirmed": bool(update.confirmed),
            "note": update.note,
        }

    for relationship in body.relationships:
        key = f"{relationship.from_source_id}->{relationship.to_source_id}"
        saved_relationships[key] = {"confirmed": relationship.confirmed, "note": relationship.note}

    plan_state = {
        "sources": saved_sources,
        "relationships": saved_relationships,
        "confirmed": bool(body.confirmed) if body.confirmed is not None else bool(saved.get("confirmed")),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    store.save_run_plan(run_id, plan_state)
    audit.append_event(
        transaction_id=run_id, event_type="PLAN_UPDATED", prior_state="DRAFT", new_state="DRAFT",
        payload={"batch_id": run_id,
                 "sources_confirmed": [u.source_id for u in body.sources if u.confirmed],
                 "relationships": list(saved_relationships.keys()),
                 "confirmed": plan_state["confirmed"]},
    )

    sources = store.list_sources(run_id)
    records = store.list_record_provenance(run_id)
    return flow.build_plan(store.get_batch(run_id), sources, saved_plan=plan_state, records=records)


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

    # A role that was inferred rather than stated, and inferred weakly,
    # is not a basis for reconciling money. Putting an order book on the
    # settlement side would compare the ledger against itself and return
    # a page of clean matches, so the guess has to be confirmed first.
    unconfirmed = [
        s["filename"] for s in sources
        if (s["detection"].get("classification") or {}).get("needs_confirmation")
        and not s["detection"].get("role_confirmed")
    ]
    if unconfirmed:
        raise HTTPException(
            400,
            "confirm what these file(s) are before running — detection was not confident enough: "
            + ", ".join(unconfirmed),
        )

    mapped = []
    for source in sources:
        _, rows = parse_csv(source["raw_csv"])
        detection = source["detection"]
        result = map_rows(
            rows, source["mapping"], SourceType(source["source_type"]), source["source_id"],
            detection.get("amount_scale", "major"),
            detection.get("debit_column"), detection.get("credit_column"),
            filename=source["filename"],
            header_offset=int(detection.get("header_row") or 1),
        )
        store.record_source_outcome(source["source_id"], result.accepted_count, len(result.rejected))
        mapped.append(result)

    ledger, settlements, rejected = combine(mapped)
    ledger_provenance, settlement_provenance = combine_provenance(mapped)
    if not ledger:
        raise HTTPException(400, "no ledger-side records — add an orders or accounting source")
    if not settlements:
        raise HTTPException(400, "no settlement-side records — add a gateway or bank source")

    records = [ReconciliationRecord(record_id=r.order_id, merchant=r) for r in ledger]

    # Provenance is index-aligned with `ledger` (see combine_provenance),
    # so a record's origin is its position, not a lookup by id — two
    # sources can legitimately carry the same order id, and keying by it
    # would attribute one file's row to the other file.
    settlement_origin = {
        record.payment_id: origin
        for record, origin in zip(settlements, settlement_provenance)
    }
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

        def _provenance(i: int, result) -> dict:
            origin = {"ledger": ledger_provenance[i] if i < len(ledger_provenance) else None}
            if result.matched_payment_id:
                origin["settlement"] = settlement_origin.get(result.matched_payment_id)
            return origin

        def on_record(i, total, record, result):
            store.save_record(run_id, i, record, result, index.exact_candidates(record.merchant),
                              provenance=_provenance(i, result))
            store.increment_batch_progress(run_id)
            audit.append_event(
                transaction_id=record.record_id, event_type="RECORD_DECIDED",
                prior_state=None, new_state=result.outcome.value,
                payload={"batch_id": run_id, "seq": i, "total": total, "record_id": record.record_id,
                         "outcome": result.outcome.value, "reason": result.reason,
                         "ai_invoked": result.ai_invoked},
            )

        def on_revision(i, record, result):
            store.save_record(run_id, i, record, result, index.exact_candidates(record.merchant),
                              provenance=_provenance(i, result))
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
        # Appended, not inserted: existing consumers read by position.
        "ledger_source_file", "ledger_source_row",
        "settlement_source_file", "settlement_source_row",
    ])
    for row in rows:
        merchant = json.loads(row["merchant_json"])
        considered = json.loads(row.get("considered_json") or "[]")
        rejected = "; ".join(
            f"{c['payment_id']} ({c['admissibility_reason']})"
            for c in considered if not c.get("admissible")
        )
        provenance = json.loads(row.get("provenance_json") or "{}") or {}
        ledger_origin = provenance.get("ledger") or {}
        settlement_origin = provenance.get("settlement") or {}
        writer.writerow([
            row["record_id"], row["outcome"], row.get("exception_type") or "",
            row.get("severity") or "", row.get("matched_payment_id") or "",
            merchant.get("amount_minor"), merchant.get("currency"),
            merchant.get("reference_id") or "", merchant.get("description") or "",
            row.get("explanation") or "", row.get("recommended_action") or "",
            "yes" if row.get("ai_invoked") else "no", row.get("review_state") or "OPEN",
            rejected,
            ledger_origin.get("filename") or "", ledger_origin.get("file_row") or "",
            settlement_origin.get("filename") or "", settlement_origin.get("file_row") or "",
        ])

    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{run_id}-reconciliation.csv"'},
    )
