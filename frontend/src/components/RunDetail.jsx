import { AnimatePresence, motion } from "motion/react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { exportRunUrl, getBreakpoints, getRun, getRunPlan, listBatchRecords, streamAudit } from "../api.js";
import { DURATION, EASE, listIndexDelay, pageTransition, riseIn, useCountUp } from "../motion.js";
import { navigate } from "../router.jsx";
import MoneyFlow, { STAGE_LABEL, STAGE_ORDER, TYPE_STAGES, count, money } from "./MoneyFlow.jsx";
import RecordDetail from "./RecordDetail.jsx";
import "../workspace.css";

const OUTCOMES = ["RECONCILED", "EXCEPTION", "HUMAN_REVIEW"];
const OUTCOME_BADGE = {
  RECONCILED: "badge-pass",
  EXCEPTION: "badge-fail",
  HUMAN_REVIEW: "badge-warn",
};
const OUTCOME_LABEL = {
  RECONCILED: "Reconciled",
  EXCEPTION: "Exceptions",
  HUMAN_REVIEW: "Needs review",
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

  const clean = 
    stageCounts.NONE ?? kindCounts.NONE ?? null;
  delete stageCounts.NONE;
  delete kindCounts.NONE;

  return {
    byStage: stageCounts,
    byKind: kindCounts,
    notEvaluated: numbers(raw.not_evaluated_counts),
    total: typeof raw.total_records === "number" ? raw.total_records : null,
    clean,
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
  const [live, setLive] = useState({ processed: 0, counts: {} });
  const [plan, setPlan] = useState(null);
  const [breakpoints, setBreakpoints] = useState(undefined); // undefined = not fetched yet
  const pushedRecordRef = useRef(false);

  const loadRecords = useCallback(
    (outcome) => {
      listBatchRecords(runId, { outcome, limit: 500 })
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
    refresh();
    loadRecords(null);
  }, [refresh, loadRecords]);

  // Progress comes from the backend's own audit events. Nothing here
  // fabricates timing — if the stream is silent, the bar does not move.
  useEffect(() => {
    const stop = streamAudit((event) => {
      if (event.payload?.batch_id !== runId) return;
      if (event.event_type === "RECORD_DECIDED") {
        setLive((prev) => ({
          processed: (event.payload.seq ?? 0) + 1,
          counts: {
            ...prev.counts,
            [event.payload.outcome]: (prev.counts[event.payload.outcome] || 0) + 1,
          },
        }));
      }
      if (event.event_type === "BATCH_COMPLETED") {
        refresh();
        loadRecords(filter);
      }
    });
    return stop;
  }, [runId, refresh, loadRecords, filter]);

  const running = run?.status === "RUNNING";
  // The tiles read the batch's own committed counts, never the events this
  // component happened to witness: subscribing halfway through a run would
  // otherwise show an undercount that looks like a real result. The SSE
  // stream still drives the progress bar, which is a rate, not a total.
  const counts = run?.outcome_counts || {};
  const total = run?.total_records || 0;
  const processed = running ? Math.max(live.processed, run?.processed_records || 0) : run?.processed_records || 0;

  // A run in flight is re-read on a slow tick. The SSE stream reports each
  // decision as it happens, but only from the moment this view subscribed;
  // the batch row is the only place that knows the whole total.
  useEffect(() => {
    if (!running) return undefined;
    const id = setInterval(refresh, 1500);
    return () => clearInterval(id);
  }, [running, refresh]);

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

  const aiAssisted = records.filter((r) => r.ai_invoked).length;
  const stages = buildStages(plan, run?.sources || [], breakpoints);

  // Opening a record pushes, so Back closes the panel. Closing it again
  // must not push, or Back walks *forward* into the record just dismissed.
  // Where this view opened the record itself, the honest undo is to step
  // back over that entry; on a deep link there is no entry to step over,
  // so the URL is replaced instead of sending the visitor off the site.
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

  return (
    <motion.div className="page" {...pageTransition}>
      <div className="page-header">
        <button
          type="button"
          className="wk-crumb"
          onClick={() => (onBack ? onBack() : navigate("/app/runs"))}
          style={{ marginBottom: 12 }}
        >
          <span aria-hidden="true">←</span> All workspaces
        </button>
        <h1 className="page-title">{run?.label || "Workspace"}</h1>
        <p className="page-subtitle">
          <span className="mono tiny">{runId}</span>
          {run?.sources?.length > 0 && (
            <>
              {" "}· {run.sources.length} source{run.sources.length === 1 ? "" : "s"}:{" "}
              {run.sources.map((s) => s.filename).join(", ")}
            </>
          )}
        </p>
      </div>

      <AnimatePresence>
        {running && (
          <motion.div {...riseIn} exit={{ opacity: 0 }} className="card" style={{ marginBottom: 22 }}>
            <h2 className="card-title">Processing</h2>
            <div className="progress-track">
              <motion.div
                className="progress-fill"
                animate={{ width: `${total ? Math.min(100, (processed / total) * 100) : 0}%` }}
                transition={{ duration: DURATION.base, ease: EASE }}
              />
            </div>
            <div className="progress-label">
              <span>
                {count(processed)} / {count(total)} processed
              </span>
              <span>streaming live from the backend</span>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      <section className="card" aria-labelledby="wk-flow-heading">
        <div className="wk-section-head">
          <div>
            <h2 className="wk-h2" id="wk-flow-heading">Where the money stopped</h2>
            <p className="wk-sub">
              {breakpoints
                ? "Each stage shows how many records break there — the first hop where the trail could not be followed."
                : "Coverage of the money path by the sources in this workspace."}
            </p>
            {breakpoints?.clean != null && breakpoints.total != null && (
              <p className="wk-sub" style={{ marginTop: 6 }}>
                <strong>
                  {count(breakpoints.clean)} of {count(breakpoints.total)} records
                </strong>{" "}
                followed the trail all the way through without breaking.
              </p>
            )}
          </div>
        </div>

        <MoneyFlow
          stages={stages}
          selected={stage}
          onSelect={setStage}
          ariaLabel="Money flow across stages, select to filter records"
        />

        {breakpoints === null && (
          <p className="wk-note" style={{ marginTop: 12 }}>
            Per-stage breakpoint counts are not available from this backend build, so the stages
            above show only which parts of the money path your sources cover. No breakdown has been
            estimated in their place.
          </p>
        )}

        {breakpoints && Object.keys(breakpoints.byKind).length > 0 && (
          <div style={{ marginTop: 16 }}>
            <h3 className="wk-finding-title">Why the trail breaks</h3>
            <ul className="wk-rel wk-rel-inline" style={{ marginTop: 9 }}>
              {Object.entries(breakpoints.byKind)
                .sort((a, b) => b[1] - a[1])
                .map(([kind, n]) => (
                  <li key={kind}>
                    <span style={{ fontWeight: 600 }}>{kind.replace(/_/g, " ").toLowerCase()}</span>
                    <span className="wk-rel-key">{count(n)} record{n === 1 ? "" : "s"}</span>
                  </li>
                ))}
            </ul>
          </div>
        )}

        {stage && !stageDrillable && (
          <p className="wk-note wk-note-warn" style={{ marginTop: 12 }}>
            This backend build does not say which records break at{" "}
            {STAGE_LABEL[stage] || stage}, so the table below is unfiltered rather than showing a
            guessed subset.
          </p>
        )}
      </section>

      <section className="wk-section" aria-labelledby="wk-outcomes-heading">
        <h2 className="wk-h2 sr-only" id="wk-outcomes-heading">Outcomes</h2>
        <div className="wk-outcomes">
          <OutcomeTile
            label="All records"
            value={total}
            active={filter === null}
            onClick={() => {
              setFilter(null);
              loadRecords(null);
            }}
          />
          {OUTCOMES.map((o) => (
            <OutcomeTile
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
          <OutcomeTile
            label="AI-assisted"
            value={aiAssisted}
            note="in the loaded page"
            active={aiOnly}
            onClick={() => setAiOnly((v) => !v)}
          />
        </div>
      </section>

      <div className="card" style={{ marginTop: 18 }}>
        <div className="results-toolbar">
          <div className="filter-row">
            <span className="tiny muted">
              {count(visible.length)} of {count(records.length)} loaded records shown
              {stage && stageDrillable ? ` · breaking at ${STAGE_LABEL[stage] || stage}` : ""}
            </span>
          </div>
          <div className="toolbar-right">
            <label htmlFor="run-search" className="sr-only">
              Search records
            </label>
            <input
              id="run-search"
              type="search"
              className="select-field"
              placeholder="Search reference, description…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
            <a className="btn-ghost" href={exportRunUrl(runId, filter)} download>
              Export CSV
            </a>
          </div>
        </div>

        <div className="table-scroll">
          <table className="records-table">
            <caption className="sr-only">
              Reconciliation results for this workspace. Select a row to open the record.
            </caption>
            <thead>
              <tr>
                <th scope="col">Record</th>
                <th scope="col">Amount</th>
                <th scope="col">Outcome</th>
                <th scope="col">Type</th>
                <th scope="col">AI</th>
                <th scope="col">Explanation</th>
              </tr>
            </thead>
            <tbody>
              {visible.map((r, i) => {
                let merchant = {};
                try {
                  merchant = JSON.parse(r.merchant_json || "{}");
                } catch {
                  merchant = {};
                }
                return (
                  <motion.tr
                    key={r.record_id}
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    transition={{ duration: DURATION.fast, delay: listIndexDelay(i) }}
                    className={"records-row" + (recordId === r.record_id ? " selected" : "")}
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
                    <td className="mono tiny">{r.record_id}</td>
                    <td className="mono tiny">{money(merchant.amount_minor, { currency: merchant.currency })}</td>
                    <td>
                      <span className={"badge " + OUTCOME_BADGE[r.outcome]}>
                        {r.outcome.replace("_", " ")}
                      </span>
                    </td>
                    <td className="tiny">{(r.exception_type || "").replace(/_/g, " ").toLowerCase()}</td>
                    <td className="tiny">{r.ai_invoked ? "yes" : "—"}</td>
                    <td className="small">{r.explanation || r.reason}</td>
                  </motion.tr>
                );
              })}
              {visible.length === 0 && (
                <tr>
                  <td colSpan={6} className="muted small" style={{ padding: "22px 8px" }}>
                    {records.length === 0
                      ? "No records yet — this workspace may still be processing."
                      : "No records match the current filters."}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      <AnimatePresence>
        {recordId && (
          <RecordDetail key={recordId} recordId={recordId} batchId={runId} onClose={closeRecord} />
        )}
      </AnimatePresence>
    </motion.div>
  );
}

function OutcomeTile({ label, value, note, active, onClick }) {
  const shown = useCountUp(Number(value) || 0);
  return (
    <button type="button" className="wk-outcome" aria-pressed={!!active} onClick={onClick}>
      <span className="wk-outcome-label">{label}</span>
      <span className="wk-outcome-value">{count(Math.round(shown))}</span>
      {note && <span className="wk-outcome-note">{note}</span>}
      {active && (
        <motion.span layoutId="wk-outcome-underline" className="wk-outcome-underline" aria-hidden="true" />
      )}
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
        note: `rows from ${covering.map((s) => s.filename).join(", ") || "this workspace"}`,
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
