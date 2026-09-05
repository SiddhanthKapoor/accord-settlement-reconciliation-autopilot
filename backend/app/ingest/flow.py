"""
The workspace plan: what was uploaded, where each file sits in the money
flow, and which files are proposed to relate to which.

What this is honest about, because the alternative is a diagram that
lies. The engine reconciles two pooled sides — everything classified as
ledger against everything classified as settlement. It does not walk a
record from an order to a payment to a payout to a bank line, hop by
hop. So this module produces a *plan and provenance view*: it says which
uploaded file occupies each stage of the flow, which stages have nothing
behind them, and which pairs of files are proposed to be related, and it
leaves per-record trace to the investigator.

Two consequences follow, and both are deliberate:

*An absent stage is shown as absent.* A workspace with orders and a bank
statement and nothing in between is a workspace with two visible stages
and three gaps, and drawing five connected boxes would tell the operator
they have coverage they do not have.

*No percentage is invented.* Where a run has actually executed, the
per-stage counts here are read back from the provenance stamped on
matched records — real files, real rows. Where it has not, the counts
are absent rather than estimated.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from app.ingest.classify import FLOW_STAGES, STAGE_LABELS, STAGE_UNASSIGNED, SourceType

# Cross-product of sources between adjacent stages is bounded: fifty
# files could otherwise propose hundreds of pairs, which is noise rather
# than a plan.
MAX_RELATIONSHIPS = 60

ENGINE_NOTE = (
    "This map is a plan and a provenance view, not a chained matcher. The engine reconciles the "
    "ledger side (orders, accounting) against the settlement side (gateway payouts, bank statements) "
    "as two pooled populations; it does not match stage-by-stage between arbitrary pairs. Per-record "
    "trace and breakpoints come from the investigator."
)


def _stage_of(source: dict) -> str:
    """Where an uploaded source sits in the flow.

    A user-confirmed source type wins over the classifier's stage: if
    someone has said this file is a bank statement, it belongs at the
    bank stage even if it was first read as a gateway payout.
    """
    classification = (source.get("detection") or {}).get("classification") or {}
    declared = source.get("source_type")
    stage = classification.get("stage")
    if stage and classification.get("detected_source_type") == declared:
        return stage
    try:
        kind = SourceType(declared)
    except (ValueError, TypeError):
        return STAGE_UNASSIGNED
    if kind is SourceType.ORDERS:
        return "ORDERS"
    if kind is SourceType.BANK_STATEMENT:
        return "BANK"
    if kind is SourceType.ACCOUNTING:
        return "ACCOUNTING"
    if kind is SourceType.PAYMENT_GATEWAY:
        return stage if stage in ("PAYMENT_GATEWAY", "SETTLEMENT") else "SETTLEMENT"
    return STAGE_UNASSIGNED


def _source_view(source: dict) -> dict:
    classification = (source.get("detection") or {}).get("classification") or {}
    detection = source.get("detection") or {}
    return {
        "source_id": source["source_id"],
        "filename": source["filename"],
        "source_type": source["source_type"],
        "role": source["role"],
        "row_count": source.get("row_count", 0),
        "accepted_count": source.get("accepted_count", 0),
        "rejected_count": source.get("rejected_count", 0),
        "provider": classification.get("provider"),
        "confidence": classification.get("detection_confidence"),
        "needs_confirmation": bool(classification.get("needs_confirmation"))
        and not detection.get("role_confirmed"),
        "role_confirmed": bool(detection.get("role_confirmed")),
        "date_range": classification.get("date_range"),
        "amount_range": classification.get("amount_range"),
        "currency": classification.get("currency"),
        "unmapped_required": detection.get("unmapped_required") or [],
        "duplicate_of": detection.get("duplicate_of"),
        "format": detection.get("format", "csv"),
    }


def _identifier_sample(source: dict) -> set[str]:
    sample = (source.get("detection") or {}).get("identifier_sample") or []
    return {str(v).strip().upper() for v in sample if str(v).strip()}


def _relationship_key(from_id: str, to_id: str) -> str:
    return f"{from_id}->{to_id}"


def propose_relationships(bound: dict[str, list[dict]], sources_by_id: dict[str, dict]) -> tuple[list[dict], bool]:
    """Pairs of files that plausibly describe the same money, one stage apart.

    Adjacency skips absent stages, because orders followed directly by a
    bank statement is a real workspace and pretending the gap makes the
    two unrelated would leave a plan with no relationships at all.

    The evidence offered is deliberately weak-but-true: overlapping
    identifier values observed in the sampled rows of both files. Where
    the samples do not overlap, that is reported as "not observed in the
    sample", never as "unrelated".
    """
    present = [stage for stage in FLOW_STAGES if bound.get(stage)]
    proposals: list[dict] = []
    truncated = False

    for left, right in zip(present, present[1:]):
        for upstream in bound[left]:
            for downstream in bound[right]:
                if len(proposals) >= MAX_RELATIONSHIPS:
                    truncated = True
                    break
                a = _identifier_sample(sources_by_id[upstream["source_id"]])
                b = _identifier_sample(sources_by_id[downstream["source_id"]])
                shared = a & b
                if shared:
                    basis = (f"{len(shared)} identifier value(s) appear in both files' sampled rows "
                             f"(e.g. {', '.join(sorted(shared)[:3])})")
                    strength = "OBSERVED_SHARED_IDENTIFIERS"
                elif a and b:
                    basis = ("adjacent stages; no shared identifier was observed in the sampled rows of "
                             "either file — that is a sample, not proof they are unrelated")
                    strength = "ADJACENCY_ONLY"
                else:
                    basis = "adjacent stages; neither file exposed identifier values to compare"
                    strength = "ADJACENCY_ONLY"
                proposals.append({
                    "relationship_id": _relationship_key(upstream["source_id"], downstream["source_id"]),
                    "from_stage": left,
                    "to_stage": right,
                    "from_source_id": upstream["source_id"],
                    "from_filename": upstream["filename"],
                    "to_source_id": downstream["source_id"],
                    "to_filename": downstream["filename"],
                    "label": f"{upstream['filename']} → {downstream['filename']}",
                    "basis": basis,
                    "strength": strength,
                    "shared_identifier_count": len(shared),
                    "status": "PROPOSED",
                })
            if truncated:
                break
        if truncated:
            break
    return proposals, truncated


def stage_coverage(records: list[dict], sources_by_id: dict[str, dict]) -> dict:
    """Real per-stage counts, read back from record provenance.

    Only what the stored records actually say: which file each ledger
    record came from, and which file the settlement it matched came
    from. Nothing is inferred for records that have no provenance
    stamped — they are counted as unknown rather than assigned.
    """
    stage_by_source = {sid: _stage_of(src) for sid, src in sources_by_id.items()}
    sourced: dict[str, int] = {stage: 0 for stage in FLOW_STAGES}
    settled: dict[str, int] = {stage: 0 for stage in FLOW_STAGES}
    unknown = 0
    for row in records:
        raw = row.get("provenance_json")
        if not raw:
            unknown += 1
            continue
        try:
            provenance = json.loads(raw)
        except (TypeError, ValueError):
            unknown += 1
            continue
        ledger = provenance.get("ledger") or {}
        stage = stage_by_source.get(ledger.get("source_id"))
        if stage in sourced:
            sourced[stage] += 1
        settlement = provenance.get("settlement") or {}
        stage = stage_by_source.get(settlement.get("source_id"))
        if stage in settled:
            settled[stage] += 1
    return {
        "available": bool(records),
        "total_records": len(records),
        "records_without_provenance": unknown,
        "records_sourced_at_stage": sourced,
        "records_settled_at_stage": settled,
        "note": "Counts come from provenance stamped on stored records. No stage percentage is estimated.",
    }


def build_plan(
    run: dict,
    sources: list[dict],
    *,
    saved_plan: dict | None = None,
    records: list[dict] | None = None,
) -> dict:
    """The workspace as a money-flow map, with the gaps left visible."""
    saved_plan = saved_plan or {}
    saved_relationships = saved_plan.get("relationships") or {}
    saved_sources = saved_plan.get("sources") or {}

    views = [_source_view(s) for s in sources]
    sources_by_id = {s["source_id"]: s for s in sources}
    for view in views:
        note = (saved_sources.get(view["source_id"]) or {}).get("note")
        if note:
            view["note"] = note

    bound: dict[str, list[dict]] = {stage: [] for stage in FLOW_STAGES}
    unassigned: list[dict] = []
    for view in views:
        stage = _stage_of(sources_by_id[view["source_id"]])
        view["stage"] = stage
        if stage in bound:
            bound[stage].append(view)
        else:
            unassigned.append(view)

    coverage = stage_coverage(records or [], sources_by_id)

    stages = []
    for stage in FLOW_STAGES:
        files = bound[stage]
        stages.append({
            "stage": stage,
            "label": STAGE_LABELS[stage],
            "present": bool(files),
            "sources": files,
            "file_count": len(files),
            "record_count": sum(f["row_count"] for f in files),
            "absent_reason": None if files else "no uploaded file covers this stage",
            "records_sourced_here": coverage["records_sourced_at_stage"][stage] if coverage["available"] else None,
            "records_settled_here": coverage["records_settled_at_stage"][stage] if coverage["available"] else None,
        })

    relationships, truncated = propose_relationships(bound, sources_by_id)
    for relationship in relationships:
        saved = saved_relationships.get(relationship["relationship_id"])
        if saved:
            relationship["status"] = "CONFIRMED" if saved.get("confirmed") else "REJECTED"
            if saved.get("note"):
                relationship["note"] = saved["note"]

    type_counts: dict[str, int] = {}
    role_counts: dict[str, int] = {}
    for view in views:
        type_counts[view["source_type"]] = type_counts.get(view["source_type"], 0) + 1
        role_counts[view["role"]] = role_counts.get(view["role"], 0) + 1

    blocking: list[dict] = []
    for view in views:
        if view["unmapped_required"]:
            blocking.append({
                "source_id": view["source_id"], "filename": view["filename"], "kind": "UNMAPPED_REQUIRED",
                "detail": f"required column(s) not mapped: {', '.join(view['unmapped_required'])}",
            })
        if view["needs_confirmation"]:
            blocking.append({
                "source_id": view["source_id"], "filename": view["filename"], "kind": "UNCONFIRMED_ROLE",
                "detail": (f"detected as {view['source_type']} with confidence "
                           f"{view['confidence']} — confirm the role before running"),
            })
    if views and not role_counts.get("LEDGER"):
        blocking.append({"source_id": None, "filename": None, "kind": "NO_LEDGER_SIDE",
                         "detail": "nothing on the ledger side — add an orders or accounting source"})
    if views and not role_counts.get("SETTLEMENT"):
        blocking.append({"source_id": None, "filename": None, "kind": "NO_SETTLEMENT_SIDE",
                         "detail": "nothing on the settlement side — add a gateway or bank source"})

    duplicates = [
        {"source_id": v["source_id"], "filename": v["filename"], "duplicate_of": v["duplicate_of"]}
        for v in views if v.get("duplicate_of")
    ]

    return {
        "run_id": run.get("batch_id"),
        "label": run.get("label"),
        "status": run.get("status"),
        "file_count": len(views),
        "total_records": sum(v["row_count"] for v in views),
        "source_type_counts": type_counts,
        "role_counts": role_counts,
        "stages": stages,
        "stages_present": [s["stage"] for s in stages if s["present"]],
        "stages_absent": [s["stage"] for s in stages if not s["present"]],
        "unassigned_sources": unassigned,
        "relationships": relationships,
        "relationships_truncated": truncated,
        "duplicates": duplicates,
        "blocking": blocking,
        "can_execute": not blocking and bool(views),
        "coverage": coverage,
        "engine_note": ENGINE_NOTE,
        "confirmed": bool(saved_plan.get("confirmed")),
        "updated_at": saved_plan.get("updated_at"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
