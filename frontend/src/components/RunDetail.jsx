import { AnimatePresence, motion } from "motion/react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  exportRunUrl, getBreakpoints, getRun, getRunPlan, getRunProgress, listBatchRecords, streamAudit,
} from "../api.js";
import { pageTransition } from "../motion.js";
import { navigate } from "../router.jsx";
import MoneyFlow, { STAGE_LABEL, STAGE_ORDER, TYPE_STAGES, count, money } from "./MoneyFlow.jsx";
import RecordDetail from "./RecordDetail.jsx";
import RunProgress from "./RunProgress.jsx";
import "../workspace.css";

const OUTCOMES = ["RECONCILED", "EXCEPTION", "HUMAN_REVIEW"];

/**
 * Records fetched per request.
 *
 * A workspace of twenty files runs to thousands of records, so the table
 * is paged. The page size is stated on screen rather than hidden: a
 * filter that searches "everything" while holding five hundred of two
 * thousand rows is a quiet lie, and this is the surface where a quiet lie
 * costs the most.
 */
const PAGE = 500;

/**
 * A filtered view at or below this size is loaded whole, immediately.
 *
 * Exceptions and human review are the views an operator actually works,
 * and they are small. Loading them entirely means the row count under the
 * table can never disagree with the count on the tile above it — a
 * disagreement the reader has no way to resolve and every reason to
 * distrust.
 */
const AUTO_ALL = 2000;
const OUTCOME_BADGE = {
  RECONCILED: "badge-pass",
  EXCEPTION: "badge-fail",
  HUMAN_REVIEW: "badge-warn",
};
const OUTCOME_LABEL = {
  RECONCILED: "Reconciled",
  EXCEPTION: "Exceptions",
  HUMAN_REVIEW: "Human review",
};

/**
 * Read the breakpoint summary.
 *
 * Returns `null` when the endpoint is absent, which the UI renders as "not
 * available" — it never falls back to computing a stage distribution of
 * its own. A breakpoint is a claim about where money stopped moving, and
 * the frontend has no standing to make that claim.
 *
 * `NONE` is not a stage: it counts the records whose trail never broke.
 * It is lifted out so it can be reported as the good news it is rather
 * than drawn as a sixth column in the money path.
 */
function normaliseBreakpoints(raw) {
  if (!raw || typeof raw !== "object") return null;

  const numbers = (obj) => {
    const out = {};
    if (obj && typeof obj === "object" && !Array.isArray(obj)) {
      for (const [k, v] of Object.entries(obj)) if (typeof v === "number") out[k] = v;
    }
    return out;
  };

  const stageCounts = numbers(raw.by_breakpoint_stage ?? raw.by_stage);
  const kindCounts = numbers(raw.by_breakpoint_kind ?? raw.by_kind);
  if (Object.keys(stageCounts).length === 0 && Object.keys(kindCounts).length === 0) return null;

  const clean = stageCounts.NONE ?? kindCounts.NONE ?? null;
  delete stageCounts.NONE;
  delete kindCounts.NONE;

  return {
    byStage: stageCounts,
    byKind: kindCounts,
    notEvaluated: numbers(raw.not_evaluated_counts),
    total: typeof raw.total_records === "number" ? raw.total_records : null,
    clean,
    // The summary now states its own coverage. Read it rather than
    // inferring one by comparing totals across endpoints.
    covers: typeof raw.covers_records === "number" ? raw.covers_records : null,
    batchTotal: typeof raw.batch_total_records === "number" ? raw.batch_total_records : null,
    truncated: raw.truncated === true,
    sourcesPresent: raw.sources_present || null,
    // No build returns per-record breakpoint ids yet; when one does, the
    // stage tiles become drillable without any further change here.
    recordsByStage: null,
  };
}

export default function RunDetail({ runId, recordId, onBack }) {
  const [run, setRun] = useState(null);
  const [records, setRecords] = useState([]);
  const [filter, setFilter] = useState(null);
  const [aiOnly, setAiOnly] = useState(false);
  const [query, setQuery] = useState("");
  const [stage, setStage] = useState(null);
  const [live, setLive] = useState({ processed: 0 });
  const [plan, setPlan] = useState(null);
  const [progress, setProgress] = useState(null);
  const [breakpoints, setBreakpoints] = useState(undefined); // undefined = not fetched yet
  const pushedRecordRef = useRef(false);

  const [loadingMore, setLoadingMore] = useState(false);
  // Views already pulled in full, keyed by outcome filter, so completing a
  // view twice cannot happen and typing cannot re-trigger a fetch per
  // keystroke.
  const completedRef = useRef(new Set());

  const loadRecords = useCallback(
    (outcome) => {
      listBatchRecords(runId, { outcome, limit: PAGE, offset: 0 })
        .then(setRecords)
        .catch(() => {});
    },
    [runId]
  );

  const refresh = useCallback(() => {
    getRun(runId).then(setRun).catch(() => {});
    getRunPlan(runId)
      .then(setPlan)
      .catch(() => setPlan(null));
    getBreakpoints(runId)
      .then((b) => setBreakpoints(normaliseBreakpoints(b)))
      .catch(() => setBreakpoints(null));
  }, [runId]);

  useEffect(() => {
    completedRef.current = new Set();
    refresh();
    loadRecords(null);
  }, [refresh, loadRecords]);

  // Progress comes from the backend's own audit events. Nothing here
  // fabricates timing — if the stream is silent, the count does not move.
  useEffect(() => {
    const stop = streamAudit((event) => {
      if (event.payload?.batch_id !== runId) return;
      if (event.event_type === "RECORD_DECIDED") {
        setLive({ processed: (event.payload.seq ?? 0) + 1 });
      }
      if (event.event_type === "BATCH_COMPLETED") {
        refresh();
        loadRecords(filter);
      }
    });
    return stop;
  }, [runId, refresh, loadRecords, filter]);

  const loadMore = useCallback(() => {
    setLoadingMore(true);
    listBatchRecords(runId, { outcome: filter, limit: PAGE, offset: records.length })
      .then((rows) => setRecords((prev) => [...prev, ...rows]))
      .catch(() => {})
      .finally(() => setLoadingMore(false));
  }, [runId, filter, records.length]);

  /**
   * Pull the whole of the current view in one request.
   *
   * Search and the filters run in the browser, so a partial page makes
   * them liars: typing a record id that exists but sits past row 500 got
   * the answer "No records match", which is a false statement about the
   * run, not a slow path. So any operation whose answer depends on seeing
   * everything completes the view first.
   */
  const completeView = useCallback(
    (howMany) => {
      const key = `${runId}|${filter || "ALL"}`;
      if (completedRef.current.has(key)) return Promise.resolve();
      const want = Number(howMany) || 0;
      if (want <= 0) return Promise.resolve();
      completedRef.current.add(key);
      setLoadingMore(true);
      return listBatchRecords(runId, { outcome: filter, limit: want, offset: 0 })
        .then(setRecords)
        .catch(() => {
          // A failed completion must not be remembered as done, or the
          // table would keep claiming a scope it never loaded.
          completedRef.current.delete(key);
        })
        .finally(() => setLoadingMore(false));
    },
    [runId, filter]
  );

  const running = run?.status === "RUNNING";

  // `RunProgress` polls this endpoint only while the run is in flight, but
  // it keeps answering afterwards — and it is the only place that counts
  // AI escalations against the whole store rather than against the page of
  // records this table happens to hold. So it is read once more when the
  // run settles, which is what makes "AI consulted" a run figure instead
  // of a page figure. It is never substituted for when absent.
  useEffect(() => {
    if (!runId || running) return undefined;
    let cancelled = false;
    getRunProgress(runId)
      .then((d) => {
        if (!cancelled && d && typeof d.ai_consulted === "number") setProgress(d);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [runId, running]);

  // The tiles read the batch's own committed counts, never the events this
  // component happened to witness: subscribing halfway through a run would
  // otherwise show an undercount that looks like a real result.
  const counts = run?.outcome_counts || {};
  const total = run?.total_records || 0;
  const processed = running
    ? Math.max(live.processed, run?.processed_records || 0)
    : run?.processed_records || 0;

  // How many records this filter could yield, from the batch's own counts —
  // so "loaded" is always stated against a real denominator rather than
  // against the size of the page that happens to be in memory.
  const availableForFilter = filter ? counts[filter] || 0 : total;
  const hasMore = records.length < availableForFilter;
  const viewIsComplete = availableForFilter > 0 && !hasMore;

  // A run in flight is re-read on a slow tick. The SSE stream reports each
  // decision as it happens, but only from the moment this view subscribed;
  // the batch row is the only place that knows the whole total.
  useEffect(() => {
    if (!running) return undefined;
    const id = setInterval(refresh, 1500);
    return () => clearInterval(id);
  }, [running, refresh]);

  // A small filtered view is loaded whole as soon as its size is known, so
  // the rows under the table always add up to the tile above it.
  useEffect(() => {
    if (running) return;
    if (availableForFilter > 0 && availableForFilter <= AUTO_ALL && hasMore) {
      completeView(availableForFilter);
    }
  }, [running, availableForFilter, hasMore, completeView]);

  // Searching, and filtering to the escalated records, are both claims
  // about the whole run and so may not be answered from a page. The first
  // keystroke pulls the rest of the view in; the guard inside completeView
  // makes that one request, not one per letter.
  useEffect(() => {
    if (running || !hasMore) return;
    if (query || aiOnly) completeView(availableForFilter);
  }, [query, aiOnly, running, hasMore, availableForFilter, completeView]);

  // ---- stage drill-in ---------------------------------------------------
  const stageRecordIds = stage && breakpoints?.recordsByStage?.[stage];
  const recordsCarryStage = records.some((r) => r.breakpoint_stage != null);
  const stageDrillable = !stage || !!stageRecordIds || recordsCarryStage;

  const visible = useMemo(() => {
    const idSet = stageRecordIds ? new Set(stageRecordIds) : null;
    return records.filter((r) => {
      if (aiOnly && !r.ai_invoked) return false;
      if (stage) {
        if (idSet) {
          if (!idSet.has(r.record_id)) return false;
        } else if (recordsCarryStage && r.breakpoint_stage !== stage) {
          return false;
        }
      }
      if (!query) return true;
      let merchant = {};
      try {
        merchant = JSON.parse(r.merchant_json || "{}");
      } catch {
        merchant = {};
      }
      const haystack = `${r.record_id} ${merchant.reference_id || ""} ${merchant.description || ""} ${
        r.reason || ""
      }`;
      return haystack.toLowerCase().includes(query.toLowerCase());
    });
  }, [records, aiOnly, stage, stageRecordIds, recordsCarryStage, query]);

  // ---- AI consulted -----------------------------------------------------
  // Preference order, and never a number that is presented as more than it
  // is. The pipeline endpoint counts against the store. Failing that, the
  // loaded page can be counted — but only claimed as a run total once the
  // page demonstrably holds every record in the run.
  const aiInLoaded = records.filter((r) => r.ai_invoked).length;
  const loadedIsWhole = total > 0 && records.length >= total && !filter && !running;
  const fromProgress = typeof progress?.ai_consulted === "number";

  // `semantic_backend` says who actually served the ambiguity tier: a model
  // provider, Accord's offline verifier, or both. The tile has to be worded
  // for whichever it was — calling an offline verifier "AI consulted" would
  // be the exact false claim this product refuses to make.
  const backend = progress?.semantic_backend || null;
  const tierInvoked =
    typeof progress?.semantic_tier_invoked === "number" ? progress.semantic_tier_invoked : null;
  const heuristicOnly = backend === "heuristic";
  const escalationLabel = heuristicOnly ? "Escalated" : "AI consulted";
  const aiConsulted = !fromProgress
    ? aiInLoaded
    : heuristicOnly
    ? tierInvoked ?? progress.heuristic_consulted ?? 0
    : progress.ai_consulted;

  let aiNote;
  if (!fromProgress && !loadedIsWhole) {
    aiNote = `counted across ${count(records.length)} loaded records`;
  } else if (heuristicOnly) {
    aiNote = "offline verifier · no model calls";
  } else if (backend === "mixed" && typeof progress?.heuristic_consulted === "number") {
    aiNote = `plus ${count(progress.heuristic_consulted)} by the offline verifier`;
  } else if (backend === "model") {
    aiNote = "records escalated to the model";
  } else {
    aiNote = "records escalated";
  }

  // ---- internal consistency --------------------------------------------
  // The outcome counts and the processed count come from different columns
  // of the same row. If they ever disagree, say so — quietly, precisely,
  // and without adjusting either number to make the page tidy.
  const outcomeSum = OUTCOMES.reduce((a, k) => a + (counts[k] || 0), 0);
  const otherOutcomes = Object.entries(counts).filter(([k]) => !OUTCOMES.includes(k));
  const otherSum = otherOutcomes.reduce((a, [, v]) => a + v, 0);
  const inconsistent = !running && total > 0 && outcomeSum + otherSum !== processed;

  const stages = buildStages(plan, run?.sources || [], breakpoints);

  // Opening a record pushes, so Back closes the panel. Closing it again
  // must not push, or Back walks *forward* into the record just dismissed.
  const openRecord = (id) => {
    pushedRecordRef.current = true;
    navigate(`/app/runs/${encodeURIComponent(runId)}/records/${encodeURIComponent(id)}`);
  };
  const closeRecord = () => {
    if (pushedRecordRef.current) {
      pushedRecordRef.current = false;
      window.history.back();
    } else {
      navigate(`/app/runs/${encodeURIComponent(runId)}`, { replace: true });
    }
  };

  const sourceCount = run?.sources?.length ?? 0;
  // What the AI/escalation filter should be able to reach once everything
  // is loaded: every record that entered the semantic tier, however it was
  // answered.
  const escalatedTotal = fromProgress && tierInvoked != null ? tierInvoked : aiConsulted;

  return (
    <motion.div className="page" {...pageTransition}>
      <header className="wk-hd">
        <button
          type="button"
          className="wk-crumb"
          onClick={() => (onBack ? onBack() : navigate("/app/runs"))}
          style={{ marginBottom: 12 }}
        >
          <span aria-hidden="true">←</span> All workspaces
        </button>
        <h1 className="wk-hd-title">{run?.label || "Workspace"}</h1>
        <div className="wk-hd-meta">
          <span className="wk-num">{runId}</span>
          <span className="wk-hd-sep" aria-hidden="true">·</span>
          <span>
            {count(sourceCount)} source{sourceCount === 1 ? "" : "s"}
          </span>
          {run?.completed_at && (
            <>
              <span className="wk-hd-sep" aria-hidden="true">·</span>
              <span>finished {new Date(run.completed_at).toLocaleString()}</span>
            </>
          )}
          {running && (
            <>
              <span className="wk-hd-sep" aria-hidden="true">·</span>
              <span>running</span>
            </>
          )}
        </div>
      </header>

      <AnimatePresence>
        {running && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            style={{ marginBottom: 26 }}
          >
            <RunProgress
              runId={runId}
              active={running}
              sseProcessed={processed}
              sseTotal={total}
              onData={setProgress}
            />
          </motion.div>
        )}
      </AnimatePresence>

      {/* The headline. One band of figures, hairline-divided — this is the
          first thing the eye should land on when a run finishes. */}
      <section aria-labelledby="wk-summary-heading">
        <h2 className="sr-only" id="wk-summary-heading">
          Run summary
        </h2>
        <div className="wk-stats">
          <Stat label="Sources" value={sourceCount} />
          <Stat label="Records processed" value={processed} note={total ? `of ${count(total)}` : null} />
          <Stat label="Reconciled" value={counts.RECONCILED || 0} tone="pass" />
          <Stat label="Exceptions" value={counts.EXCEPTION || 0} tone="fail" />
          <Stat label="Human review" value={counts.HUMAN_REVIEW || 0} tone="warn" />
          <Stat label={escalationLabel} value={aiConsulted} note={aiNote} />
        </div>
      </section>

      {inconsistent && (
        <p className="wk-note wk-note-warn" role="status" style={{ marginTop: 12 }}>
          The outcome counts total {count(outcomeSum + otherSum)} but {count(processed)} records were
          recorded as processed. Both figures are shown as the backend reported them; neither has
          been adjusted to make them agree.
        </p>
      )}

      <section className="wk-block" aria-labelledby="wk-flow-heading">
        <div className="wk-block-head">
          <div className="wk-block-titles">
            <h2 className="wk-h2" id="wk-flow-heading">
              Where the money stopped
            </h2>
            <p className="wk-sub">
              {breakpoints
                ? "Each stage shows how many records break there — the first hop where the trail could not be followed."
                : "Coverage of the money path by the sources in this workspace."}
            </p>
          </div>
          {breakpoints?.clean != null && breakpoints.total != null && (
            <span className="wk-count-line">
              {count(breakpoints.clean)} of {count(breakpoints.total)} never broke
            </span>
          )}
        </div>

        <MoneyFlow
          stages={stages}
          selected={stage}
          onSelect={setStage}
          ariaLabel="Money flow across stages, select to filter records"
        />

        {/* The summary reports its own coverage. It is only remarked on
            when it says it is short of the run — no figure is scaled to
            make the two agree. */}
        {breakpoints &&
          (breakpoints.truncated ||
            (breakpoints.covers != null &&
              breakpoints.batchTotal != null &&
              breakpoints.covers < breakpoints.batchTotal)) && (
            <p className="wk-note wk-note-warn" style={{ marginTop: 12 }}>
              The breakpoint summary covers {count(breakpoints.covers ?? breakpoints.total)} of the{" "}
              {count(breakpoints.batchTotal ?? total)} records in this run, so the stage counts
              above describe a subset.
            </p>
          )}

        {breakpoints === null && (
          <p className="wk-note" style={{ marginTop: 12 }}>
            Per-stage breakpoint counts are not available from this backend build, so the stages
            above show only which parts of the money path your sources cover. No breakdown has been
            estimated in their place.
          </p>
        )}

        {breakpoints && Object.keys(breakpoints.byKind).length > 0 && (
          <div className="wk-block wk-block-sub" style={{ marginTop: 24 }}>
            <div className="wk-block-head">
              <div className="wk-block-titles">
                <h3 className="wk-h3">Why the trail breaks</h3>
              </div>
            </div>
            <ul className="wk-rel wk-rel-inline">
              {Object.entries(breakpoints.byKind)
                .sort((a, b) => b[1] - a[1])
                .map(([kind, n]) => (
                  <li key={kind}>
                    <span style={{ fontWeight: 600 }}>{kind.replace(/_/g, " ").toLowerCase()}</span>
                    <span className="wk-rel-key">
                      {count(n)} record{n === 1 ? "" : "s"}
                    </span>
                  </li>
                ))}
            </ul>
          </div>
        )}

        {stage && !stageDrillable && (
          <p className="wk-note wk-note-warn" style={{ marginTop: 12 }}>
            This backend build does not say which records break at {STAGE_LABEL[stage] || stage}, so
            the table below is unfiltered rather than showing a guessed subset.
          </p>
        )}
      </section>

      <section className="wk-block" aria-labelledby="wk-records-heading">
        <div className="wk-block-head">
          <div className="wk-block-titles">
            <h2 className="wk-h2" id="wk-records-heading">
              Every record, and what decided it
            </h2>
            <p className="wk-sub">
              Open any row to see the evidence, the checks that ran, and whether a model was
              consulted.
            </p>
          </div>
          <div className="wk-block-actions">
            <a className="btn-ghost" href={exportRunUrl(runId, filter)} download>
              Export CSV
            </a>
          </div>
        </div>

        <div className="wk-outcomes" style={{ marginBottom: 14 }}>
          <FilterPill
            label="All"
            value={total}
            active={filter === null && !aiOnly}
            onClick={() => {
              setFilter(null);
              setAiOnly(false);
              loadRecords(null);
            }}
          />
          {OUTCOMES.map((o) => (
            <FilterPill
              key={o}
              label={OUTCOME_LABEL[o]}
              value={counts[o] || 0}
              active={filter === o}
              onClick={() => {
                setFilter(o);
                loadRecords(o);
              }}
            />
          ))}
          <FilterPill
            label={heuristicOnly ? "Escalated" : "AI-assisted"}
            value={fromProgress && tierInvoked != null ? tierInvoked : aiConsulted}
            active={aiOnly}
            onClick={() => setAiOnly((v) => !v)}
          />
        </div>

        {aiOnly && escalatedTotal > aiInLoaded && hasMore && !loadingMore && (
          <p className="wk-note wk-note-warn" style={{ marginBottom: 14 }}>
            {count(escalatedTotal)} record{escalatedTotal === 1 ? " was" : "s were"} escalated to
            the semantic tier in this run, but only {count(aiInLoaded)} of them{" "}
            {aiInLoaded === 1 ? "is" : "are"} in the {count(records.length)} records loaded so far.
            This filter searches what is loaded, not the whole run.{" "}
            <button
              type="button"
              className="wk-inv-rowlink"
              onClick={() => completeView(availableForFilter)}
              disabled={loadingMore}
            >
              {loadingMore ? "Loading…" : `Load all ${count(availableForFilter)} records`}
            </button>
          </p>
        )}

        <div className="wk-toolbar">
          <span className="wk-count-line">
            {count(visible.length)} shown ·{" "}
            {loadingMore
              ? `loading all ${count(availableForFilter)}…`
              : viewIsComplete
              ? `searching all ${count(availableForFilter)} records in this view`
              : `${count(records.length)} of ${count(availableForFilter)} loaded`}
            {stage && stageDrillable ? ` · breaking at ${STAGE_LABEL[stage] || stage}` : ""}
          </span>
          <div className="wk-toolbar-right">
            <label htmlFor="run-search" className="sr-only">
              Search records
            </label>
            <input
              id="run-search"
              type="search"
              className="wk-search"
              placeholder="Search reference, description…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </div>
        </div>

        <div className="wk-surface">
          <div className="wk-tablewrap">
          <table className="wk-table wk-table-pad wk-table-clickable" style={{ minWidth: 780 }}>
            <caption className="sr-only">
              Reconciliation results for this workspace. Select a row to open the record.
            </caption>
            <thead>
              <tr>
                <th scope="col">Record</th>
                <th scope="col" className="wk-col-num">Amount</th>
                <th scope="col">Outcome</th>
                <th scope="col">Exception</th>
                <th scope="col">AI</th>
                <th scope="col">What decided it</th>
              </tr>
            </thead>
            <tbody>
              {visible.map((r) => {
                let merchant = {};
                try {
                  merchant = JSON.parse(r.merchant_json || "{}");
                } catch {
                  merchant = {};
                }
                return (
                  <tr
                    key={r.record_id}
                    className={
                      "wk-row-data wk-row-click" +
                      (recordId === r.record_id ? " wk-row-selected" : "")
                    }
                    tabIndex={0}
                    role="button"
                    aria-label={`Open ${r.record_id}, ${r.outcome.replace("_", " ")}`}
                    onClick={() => openRecord(r.record_id)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        openRecord(r.record_id);
                      }
                    }}
                  >
                    <th scope="row" style={{ textAlign: "left", fontWeight: 400, padding: "10px 12px" }}>
                      <span className="wk-inv-name">{r.record_id}</span>
                      {merchant.reference_id && (
                        <span className="wk-inv-side">ref {merchant.reference_id}</span>
                      )}
                    </th>
                    <td className="wk-col-num">
                      {money(merchant.amount_minor, { currency: merchant.currency })}
                    </td>
                    <td>
                      <span className={"badge " + OUTCOME_BADGE[r.outcome]}>
                        {r.outcome.replace("_", " ")}
                      </span>
                    </td>
                    <td style={{ fontSize: 11.5, color: "var(--wk-muted)" }}>
                      {(r.exception_type || "—").replace(/_/g, " ").toLowerCase()}
                    </td>
                    <td style={{ fontSize: 11.5, color: "var(--wk-muted)" }}>
                      {r.ai_invoked ? "consulted" : "—"}
                    </td>
                    <td style={{ fontSize: 12, color: "var(--wk-muted)", maxWidth: 380 }}>
                      {r.explanation || r.reason}
                    </td>
                  </tr>
                );
              })}
              {visible.length === 0 && (
                <tr>
                  <td colSpan={6} className="wk-table-empty">
                    <EmptyState
                      running={running}
                      loading={loadingMore}
                      loaded={records.length}
                      available={availableForFilter}
                      complete={viewIsComplete}
                      query={query}
                      onLoadAll={() => completeView(availableForFilter)}
                    />
                  </td>
                </tr>
              )}
            </tbody>
          </table>
          </div>
        </div>

        {hasMore && (
          <div style={{ marginTop: 14, display: "flex", alignItems: "center", gap: 14, flexWrap: "wrap" }}>
            <button type="button" className="btn-ghost" onClick={loadMore} disabled={loadingMore}>
              {loadingMore
                ? "Loading…"
                : `Load ${count(Math.min(PAGE, availableForFilter - records.length))} more`}
            </button>
            <button
              type="button"
              className="wk-inv-rowlink"
              onClick={() => completeView(availableForFilter)}
              disabled={loadingMore}
            >
              Load all {count(availableForFilter)}
            </button>
            <span className="wk-count-line">
              {count(availableForFilter - records.length)} record
              {availableForFilter - records.length === 1 ? "" : "s"} in this run are not loaded yet.
              Search and the AI filter only look at what is loaded.
            </span>
          </div>
        )}
      </section>

      <AnimatePresence>
        {recordId && (
          <RecordDetail key={recordId} recordId={recordId} batchId={runId} onClose={closeRecord} />
        )}
      </AnimatePresence>
    </motion.div>
  );
}

/**
 * What an empty table is allowed to say.
 *
 * "No records match the current filters" is only true if the filters were
 * applied to every record in the run. Applied to a page of five hundred it
 * is a false statement about the workspace — and both records this demo
 * turns on sit past that page. So the scope is always named, and where the
 * view is short of the run, the way to widen it is in the same sentence.
 */
function EmptyState({ running, loading, loaded, available, complete, query, onLoadAll }) {
  if (loading) {
    return <span>Loading all {count(available)} records so the whole run can be searched…</span>;
  }
  if (loaded === 0) {
    return (
      <span>
        {running
          ? "No records decided yet — this workspace is still processing."
          : "No records in this workspace."}
      </span>
    );
  }
  if (complete) {
    return (
      <span>
        No match in any of the {count(available)} records in this view
        {query ? ` for \u201C${query}\u201D` : ""}.
      </span>
    );
  }
  return (
    <span>
      No match in the {count(loaded)} records loaded so far
      {query ? ` for \u201C${query}\u201D` : ""} — the run holds {count(available)}.{" "}
      <button type="button" className="wk-inv-rowlink" onClick={onLoadAll}>
        Load all {count(available)} and search the whole run
      </button>
    </span>
  );
}

/**
 * One figure in the summary band.
 *
 * Deliberately not animated. A number that counts up renders, for a few
 * hundred milliseconds, values the backend never reported — and on a live
 * run, where this band re-reads every 1.5s, it does so continuously. On a
 * reconciliation surface that is not a flourish, it is a false reading.
 * `null` renders as a gap, never as a zero.
 */
function Stat({ label, value, tone, note }) {
  const numeric = typeof value === "number";
  return (
    <div className="wk-stat">
      <div className="wk-stat-label">{label}</div>
      <div className={`wk-stat-value${tone ? ` wk-stat-value-${tone}` : ""}`}>
        {numeric ? count(value) : "—"}
      </div>
      {note && <div className="wk-stat-note">{note}</div>}
    </div>
  );
}

function FilterPill({ label, value, note, active, onClick }) {
  return (
    <button
      type="button"
      className="wk-outcome wk-outcome-pill"
      aria-pressed={!!active}
      onClick={onClick}
    >
      <span className="wk-outcome-label">{label}</span>
      <span className="wk-outcome-value">{count(value)}</span>
      {note && <span className="wk-outcome-note">{note}</span>}
    </button>
  );
}

/**
 * Stage descriptors for a completed run.
 *
 * Every number comes from the breakpoint summary. A stage where *every*
 * record went un-evaluated is reported as not evaluated — because nothing
 * about it was checked, which is not the same as nothing being wrong. A
 * stage checked for some records and not others says exactly that; it is
 * never rounded up into a clean pass or down into a failure.
 */
function buildStages(plan, sources, breakpoints) {
  if (!breakpoints) {
    // Coverage only. Which parts of the money path the uploaded files can
    // speak to, and nothing about how the run went.
    return STAGE_ORDER.map((key) => {
      const covering = sources.filter((s) => (TYPE_STAGES[s.source_type] || []).includes(key));
      const rows = covering.reduce((a, s) => a + (s.row_count || 0), 0);
      return {
        key,
        label: STAGE_LABEL[key],
        evaluated: covering.length > 0,
        headline: count(rows),
        note: `rows from ${covering.length} file${covering.length === 1 ? "" : "s"}`,
        selectable: false,
      };
    });
  }

  const total = breakpoints.total;
  return STAGE_ORDER.map((key) => {
    const skipped = breakpoints.notEvaluated[key] ?? 0;
    const breaks = breakpoints.byStage[key] ?? 0;
    const wholeStageSkipped = total != null && skipped >= total && breaks === 0;

    if (wholeStageSkipped) {
      return {
        key,
        label: STAGE_LABEL[key],
        evaluated: false,
        headline: null,
        // Say only what the counts prove: nothing was checked here. Why
        // nothing was checked — no source, or an engine that does not
        // chain this hop — is not in these numbers, and guessing at it
        // would be inventing a cause.
        note: "No record in this run was checked at this stage.",
        selectable: false,
      };
    }

    const parts = [breaks === 0 ? "no records break here" : "records break here"];
    if (skipped > 0 && total != null) parts.push(`${count(skipped)} of ${count(total)} not evaluated`);
    return {
      key,
      label: STAGE_LABEL[key],
      evaluated: true,
      headline: count(breaks),
      note: parts.join(" · "),
    };
  });
}
