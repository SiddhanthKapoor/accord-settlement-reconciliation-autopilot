"""
Investigation endpoints.

Two shapes, both read-only:

    POST /records/{record_id}/investigate?batch_id=...   one record, in full
    GET  /batch/{batch_id}/breakpoints                   the whole run, counted

The frontend reaches these through its `/api` proxy, which strips the
prefix — the same convention every other router in this app is registered
under, so `/api/records/x/investigate` in the browser arrives here as
`/records/x/investigate`.

The router's only real job beyond routing is assembling the evidence the
engine is allowed to see: the stored decision, its siblings in the same
run, which kinds of file the run contained, and the run's settlement-side
population. That population is rebuilt here rather than in the engine so
`app.engine.investigate` stays free of both the API and the ingest layer,
and so a test can hand it an exact world instead of a database.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.domain.models import PolicyConfig, RazorpaySettlementRecord
from app.engine.investigate import (
    InvestigationContext,
    Investigator,
    breakpoint_summary,
    load_context,
)
from app.ledger import store

router = APIRouter()

#: Above this many settlement-side rows the population is not rebuilt for
#: an investigation. Rebuilding means re-parsing the uploaded files, which
#: is fine for a month of transactions and wasteful for a stress-test
#: batch; the investigation degrades to the evidence already stored rather
#: than making an interactive request wait on a re-parse.
MAX_REBUILD_ROWS = 25_000

# One rebuilt population per run, keyed by a fingerprint of its sources so
# an upload or a mapping change invalidates it. Deliberately tiny: this is
# a request-path convenience, not a cache layer.
_population_cache: dict[str, tuple[tuple, list[RazorpaySettlementRecord]]] = {}
_POPULATION_CACHE_LIMIT = 4

# The provider chain is built at most once per process. A chain that
# cannot be built is remembered as None, which the investigator treats as
# a degraded-but-correct outcome rather than an error.
_chain_state: dict = {"built": False, "chain": None}


def _chain():
    if not _chain_state["built"]:
        _chain_state["built"] = True
        try:
            from app.engine.providers import build_chain  # owned by the provider layer

            _chain_state["chain"] = build_chain()
        except Exception:  # noqa: BLE001 — a missing or broken provider layer must never 500 a read
            _chain_state["chain"] = None
    return _chain_state["chain"]


def _uploaded_settlements(batch_id: str, sources: list[dict]) -> list[RazorpaySettlementRecord]:
    """Rebuild the settlement side of an uploaded run from its own files."""
    from app.ingest.mapper import map_rows
    from app.ingest.schema import SourceType, parse_csv

    settlement_sources = [s for s in sources if s.get("role") == "SETTLEMENT"]
    if not settlement_sources:
        return []
    if sum(int(s.get("row_count") or 0) for s in settlement_sources) > MAX_REBUILD_ROWS:
        return []

    fingerprint = tuple(
        (s.get("source_id"), s.get("row_count"), s.get("source_type")) for s in settlement_sources
    )
    cached = _population_cache.get(batch_id)
    if cached and cached[0] == fingerprint:
        return cached[1]

    records: list[RazorpaySettlementRecord] = []
    for source in store.list_sources(batch_id, include_raw=True):
        if source.get("role") != "SETTLEMENT":
            continue
        try:
            _, rows = parse_csv(source["raw_csv"])
            detection = source.get("detection") or {}
            mapped = map_rows(
                rows,
                source.get("mapping") or {},
                SourceType(source["source_type"]),
                source["source_id"],
                detection.get("amount_scale", "major"),
                detection.get("debit_column"),
                detection.get("credit_column"),
            )
            records.extend(mapped.settlement_records)
        except Exception:  # noqa: BLE001 — a source that will not re-map costs evidence, not the request
            continue

    if len(_population_cache) >= _POPULATION_CACHE_LIMIT:
        _population_cache.clear()
    _population_cache[batch_id] = (fingerprint, records)
    return records


def _dataset_settlements() -> list[RazorpaySettlementRecord]:
    """The settlement pool a dataset-driven batch was reconciled against."""
    try:
        from app.api.routes import _load_pool

        return _load_pool()
    except Exception:  # noqa: BLE001 — the pool is evidence, not a precondition
        return []


def _attach_population(context: InvestigationContext) -> InvestigationContext:
    batch_id = context.record.get("batch_id") or (context.batch or {}).get("batch_id")
    if context.sources:
        context.settlements = _uploaded_settlements(str(batch_id), context.sources)
    else:
        context.settlements = _dataset_settlements()
    context.settlement_population_complete = bool(context.settlements)
    return context


@router.post("/records/{record_id}/investigate")
def investigate_record(record_id: str, batch_id: str | None = None, use_ai: bool = True):
    """Why this record did not close.

    Read-only: it never changes the record's outcome. It appends exactly
    one `AI_INVESTIGATION` audit event carrying the breakpoint and the
    hypothesis labels — never the prompt, never raw model text.
    """
    context = load_context(record_id, batch_id, policy=PolicyConfig())
    if context is None:
        raise HTTPException(404, "record not found")
    _attach_population(context)
    investigator = Investigator(chain_factory=_chain)
    return investigator.investigate(context, use_ai=use_ai).to_dict()


@router.get("/batch/{batch_id}/breakpoints")
def batch_breakpoints(batch_id: str, limit: int | None = None):
    """Where a whole run's money trails stop, counted by stage and kind.

    Entirely deterministic — the same trace analysis each record's
    drill-in shows, aggregated, so the dashboard and the detail view can
    never disagree. No model is called.

    The default covers the WHOLE run. It used to stop at 2,000 records,
    which on a 3,504-record run produced a summary that silently described
    a subset while the run header described the run — two true numbers that
    read as one contradiction. A partial summary is worse than a slower
    one, so completeness is the default and any truncation is declared in
    the response rather than left for the reader to notice.
    """
    batch = store.get_batch(batch_id)
    if not batch:
        raise HTTPException(404, "batch not found")

    total = batch.get("total_records") or 0
    effective = limit if limit is not None else max(total, 1)
    rows = store.list_records(batch_id, limit=effective)
    sources = store.list_sources(batch_id)
    summary = breakpoint_summary(rows, sources, PolicyConfig())
    summary["batch_id"] = batch_id
    summary["covers_records"] = len(rows)
    summary["batch_total_records"] = total
    summary["truncated"] = bool(total and len(rows) < total)
    return summary
