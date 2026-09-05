import { AnimatePresence, motion } from "motion/react";
import { useCallback, useEffect, useRef, useState } from "react";
import { exportRunUrl, getRun, listBatchRecords, streamAudit } from "../api.js";
import { DURATION, EASE, listIndexDelay, pageTransition, riseIn } from "../motion.js";
import RecordDetail from "./RecordDetail.jsx";

const OUTCOMES = ["RECONCILED", "EXCEPTION", "HUMAN_REVIEW"];
const OUTCOME_COLOR = {
  RECONCILED: "var(--pass)",
  EXCEPTION: "var(--fail)",
  HUMAN_REVIEW: "var(--warn)",
};
const OUTCOME_BADGE = {
  RECONCILED: "badge-pass",
  EXCEPTION: "badge-fail",
  HUMAN_REVIEW: "badge-warn",
};

function money(minor) {
  return `₹${((minor || 0) / 100).toLocaleString("en-IN", { minimumFractionDigits: 2 })}`;
}

export default function RunDetail({ runId, onBack }) {
  const [run, setRun] = useState(null);
  const [records, setRecords] = useState([]);
  const [filter, setFilter] = useState(null);
  const [aiOnly, setAiOnly] = useState(false);
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState(null);
  const [live, setLive] = useState({ processed: 0, counts: {} });
  const runningRef = useRef(false);

  const loadRecords = useCallback(
    (outcome) => {
      listBatchRecords(runId, { outcome, limit: 500 }).then(setRecords).catch(() => {});
    },
    [runId]
  );

  const refresh = useCallback(() => {
    getRun(runId)
      .then((r) => {
        setRun(r);
        runningRef.current = r.status === "RUNNING";
      })
      .catch(() => {});
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

  const counts = run?.status === "RUNNING" ? live.counts : run?.outcome_counts || {};
  const total = run?.total_records || 0;
  const processed = run?.status === "RUNNING" ? live.processed : run?.processed_records || 0;

  const visible = records.filter((r) => {
    if (aiOnly && !r.ai_invoked) return false;
    if (!query) return true;
    const merchant = JSON.parse(r.merchant_json || "{}");
    const haystack = `${r.record_id} ${merchant.reference_id || ""} ${merchant.description || ""} ${r.reason || ""}`;
    return haystack.toLowerCase().includes(query.toLowerCase());
  });

  const aiAssisted = records.filter((r) => r.ai_invoked).length;

  return (
    <motion.div className="page" {...pageTransition}>
      <div className="page-header">
        <button type="button" className="btn-ghost" onClick={onBack} style={{ marginBottom: 12 }}>
          ← All runs
        </button>
        <h1 className="page-title">{run?.label || "Run"}</h1>
        <p className="page-subtitle">
          <span className="mono tiny">{runId}</span>
          {run?.sources?.length > 0 && (
            <> · {run.sources.length} source{run.sources.length === 1 ? "" : "s"}:{" "}
              {run.sources.map((s) => s.filename).join(", ")}</>
          )}
        </p>
      </div>

      <div className="stats-grid" style={{ marginBottom: 22 }}>
        <Tile label="Records" value={total.toLocaleString("en-IN")} />
        <Tile label="Reconciled" value={counts.RECONCILED || 0} tone="pass" />
        <Tile label="Exceptions" value={counts.EXCEPTION || 0} tone="fail" />
        <Tile label="Human review" value={counts.HUMAN_REVIEW || 0} tone="warn" />
        <Tile label="AI-assisted" value={aiAssisted} note="records needing semantics" />
      </div>

      <AnimatePresence>
        {run?.status === "RUNNING" && (
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
              <span>{processed} / {total} processed</span>
              <span>streaming live from the backend</span>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      <div className="card">
        <div className="results-toolbar">
          <div className="filter-row">
            {[null, ...OUTCOMES].map((o) => (
              <button
                key={o || "all"}
                type="button"
                className={"btn-ghost" + (filter === o ? " active" : "")}
                aria-pressed={filter === o}
                onClick={() => {
                  setFilter(o);
                  loadRecords(o);
                }}
              >
                {o ? o.replace("_", " ") : "All"}
                {o && counts[o] != null && <span className="tiny muted"> {counts[o]}</span>}
              </button>
            ))}
            <button
              type="button"
              className={"btn-ghost" + (aiOnly ? " active" : "")}
              aria-pressed={aiOnly}
              onClick={() => setAiOnly((v) => !v)}
            >
              AI-assisted
            </button>
          </div>
          <div className="toolbar-right">
            <label htmlFor="run-search" className="sr-only">Search records</label>
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
            <caption className="sr-only">Reconciliation results for this run</caption>
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
                const merchant = JSON.parse(r.merchant_json || "{}");
                return (
                  <motion.tr
                    key={r.record_id}
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    transition={{ duration: DURATION.fast, delay: listIndexDelay(i) }}
                    className={"records-row" + (selected === r.record_id ? " selected" : "")}
                    tabIndex={0}
                    role="button"
                    aria-label={`Open ${r.record_id}, ${r.outcome.replace("_", " ")}`}
                    onClick={() => setSelected(r.record_id)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        setSelected(r.record_id);
                      }
                    }}
                  >
                    <td className="mono tiny">{r.record_id}</td>
                    <td className="mono tiny">{money(merchant.amount_minor)}</td>
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
                      ? "No records yet — this run may still be processing."
                      : "No records match the current filters."}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      <AnimatePresence>
        {selected && (
          <RecordDetail
            key={selected}
            recordId={selected}
            batchId={runId}
            onClose={() => setSelected(null)}
          />
        )}
      </AnimatePresence>
    </motion.div>
  );
}

function Tile({ label, value, tone, note }) {
  return (
    <motion.div className="stat-tile" {...riseIn}>
      <div className="stat-label">{label}</div>
      <div className={`stat-value${tone ? ` stat-value-${tone}` : ""}`}>{value}</div>
      {note && <div className="stat-note">{note}</div>}
    </motion.div>
  );
}
