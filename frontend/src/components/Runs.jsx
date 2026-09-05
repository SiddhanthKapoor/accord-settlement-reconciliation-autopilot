import { AnimatePresence, motion } from "motion/react";
import { useCallback, useEffect, useState } from "react";
import { getLatestEvaluation, listRuns } from "../api.js";
import { listIndexDelay, pageTransition, riseIn } from "../motion.js";
import NewRun from "./NewRun.jsx";
import RunDetail from "./RunDetail.jsx";

const OUTCOME_COLOR = {
  RECONCILED: "var(--pass)",
  EXCEPTION: "var(--fail)",
  HUMAN_REVIEW: "var(--warn)",
};

function money(minor) {
  return `₹${((minor || 0) / 100).toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
}

export default function Runs() {
  const [runs, setRuns] = useState(null);
  const [creating, setCreating] = useState(false);
  const [openRunId, setOpenRunId] = useState(null);
  const [evaluation, setEvaluation] = useState(null);

  const load = useCallback(() => {
    listRuns().then((d) => setRuns(d.runs)).catch(() => setRuns([]));
  }, []);

  useEffect(() => {
    load();
    // The evaluation report is optional context, not a dependency — the
    // product works with no evaluation ever having been run.
    getLatestEvaluation("v3").then(setEvaluation).catch(() => {});
  }, [load]);

  if (openRunId) {
    return (
      <RunDetail
        runId={openRunId}
        onBack={() => {
          setOpenRunId(null);
          load();
        }}
      />
    );
  }

  if (creating) {
    return (
      <NewRun
        onRunStarted={(runId) => {
          setCreating(false);
          setOpenRunId(runId);
        }}
      />
    );
  }

  const totals = (runs || []).reduce(
    (acc, r) => {
      const c = r.outcome_counts || {};
      acc.processed += r.processed_records || 0;
      acc.reconciled += c.RECONCILED || 0;
      acc.exceptions += c.EXCEPTION || 0;
      acc.review += c.HUMAN_REVIEW || 0;
      return acc;
    },
    { processed: 0, reconciled: 0, exceptions: 0, review: 0 }
  );

  return (
    <motion.div className="page" {...pageTransition}>
      <div className="page-header">
        <h1 className="page-title">Reconciliation runs</h1>
        <p className="page-subtitle">
          Each run reconciles a set of uploaded sources against each other. Deterministic matching
          resolves what it can prove; genuinely ambiguous records go to the semantic classifier, and
          anything still unresolved goes to a person.
        </p>
      </div>

      <div className="stats-grid" style={{ marginBottom: 22 }}>
        <Tile label="Records processed" value={totals.processed.toLocaleString("en-IN")} />
        <Tile label="Reconciled" value={totals.reconciled.toLocaleString("en-IN")} tone="pass" />
        <Tile label="Exceptions" value={totals.exceptions.toLocaleString("en-IN")} tone="fail" />
        <Tile label="Human review" value={totals.review.toLocaleString("en-IN")} tone="warn" />
        <Tile
          label="Held-out accuracy"
          value={
            evaluation?.metrics?.reconciliation_accuracy != null
              ? `${(evaluation.metrics.reconciliation_accuracy * 100).toFixed(1)}%`
              : "—"
          }
          note={evaluation ? `${evaluation.record_count} records` : "no evaluation run"}
        />
      </div>

      <div className="card">
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
          <h2 className="card-title" style={{ marginBottom: 0 }}>
            {runs === null ? "Loading…" : `${runs.length} run${runs.length === 1 ? "" : "s"}`}
          </h2>
          <motion.button
            type="button"
            className="btn-small"
            onClick={() => setCreating(true)}
            whileHover={{ y: -1 }}
            whileTap={{ scale: 0.985 }}
          >
            New run
          </motion.button>
        </div>

        {runs !== null && runs.length === 0 && (
          <p className="muted small" style={{ padding: "20px 0" }}>
            No runs yet. Start one by uploading a ledger source and a settlement source.
          </p>
        )}

        <ul className="run-list">
          <AnimatePresence initial={false}>
            {(runs || []).map((run, i) => {
              const counts = run.outcome_counts || {};
              const total = Object.values(counts).reduce((a, b) => a + b, 0) || 1;
              return (
                <motion.li
                  key={run.batch_id}
                  {...riseIn}
                  transition={{ ...riseIn.transition, delay: listIndexDelay(i) }}
                >
                  <button type="button" className="run-row" onClick={() => setOpenRunId(run.batch_id)}>
                    <span className={`status-pill status-${(run.status || "draft").toLowerCase()}`}>
                      {run.status}
                    </span>
                    <span className="run-label">{run.label}</span>
                    <span className="tiny muted mono">{run.batch_id}</span>
                    <span className="run-meta">
                      <span className="mini-bar" role="img"
                        aria-label={`${counts.RECONCILED || 0} reconciled, ${counts.EXCEPTION || 0} exceptions, ${counts.HUMAN_REVIEW || 0} in review`}>
                        {["RECONCILED", "EXCEPTION", "HUMAN_REVIEW"].map((k) => (
                          <span key={k} className="mini-seg"
                            style={{ width: `${((counts[k] || 0) / total) * 100}%`, background: OUTCOME_COLOR[k] }} />
                        ))}
                      </span>
                      <span className="tiny muted">
                        {run.processed_records}/{run.total_records}
                      </span>
                    </span>
                  </button>
                </motion.li>
              );
            })}
          </AnimatePresence>
        </ul>
      </div>
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
