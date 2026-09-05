import { AnimatePresence, motion } from "motion/react";
import { useCallback, useEffect, useState } from "react";
import { listRuns } from "../api.js";
import { listIndexDelay, pageTransition, riseIn, useCountUp } from "../motion.js";
import { Link, navigate } from "../router.jsx";
import { count } from "./MoneyFlow.jsx";
import "../workspace.css";

const OUTCOME_COLOR = {
  RECONCILED: "var(--pass)",
  EXCEPTION: "var(--fail)",
  HUMAN_REVIEW: "var(--warn)",
};

/**
 * The list of reconciliation workspaces.
 *
 * Navigation here is real navigation. This component used to own its own
 * list / create / detail switch as a state variable, which meant the
 * browser Back button walked out of the application from a run detail.
 * Opening a run is a route change now, so Back returns to the list.
 */
export default function Runs() {
  const [runs, setRuns] = useState(null);

  const load = useCallback(() => {
    listRuns()
      .then((d) => setRuns(d.runs))
      .catch(() => setRuns([]));
  }, []);

  useEffect(load, [load]);

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
        <h1 className="page-title">Reconciliation workspaces</h1>
        <p className="page-subtitle">
          Each workspace reconciles a set of uploaded sources against each other. Deterministic
          matching resolves what it can prove; genuinely ambiguous records go to the semantic
          classifier, and anything still unresolved goes to a person.
        </p>
      </div>

      <div className="stats-grid" style={{ marginBottom: 22 }}>
        <Tile label="Records processed" value={totals.processed} />
        <Tile label="Reconciled" value={totals.reconciled} tone="pass" />
        <Tile label="Exceptions" value={totals.exceptions} tone="fail" />
        <Tile label="Human review" value={totals.review} tone="warn" />
      </div>

      <div className="card">
        <div
          style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}
        >
          <h2 className="card-title" style={{ marginBottom: 0 }}>
            {runs === null ? "Loading…" : `${runs.length} workspace${runs.length === 1 ? "" : "s"}`}
          </h2>
          <Link to="/app/runs/new" className="btn-small">
            New workspace
          </Link>
        </div>

        {runs !== null && runs.length === 0 && (
          <div className="wk-empty" style={{ marginTop: 16 }}>
            No workspaces yet. Start one by dropping in the files you already have — orders, a
            gateway payout export, a bank statement — and Accord will work out what they are.
          </div>
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
                  <button
                    type="button"
                    className="run-row"
                    onClick={() => navigate(`/app/runs/${encodeURIComponent(run.batch_id)}`)}
                  >
                    <span className={`status-pill status-${(run.status || "draft").toLowerCase()}`}>
                      {run.status}
                    </span>
                    <span className="run-label">{run.label}</span>
                    <span className="tiny muted mono">{run.batch_id}</span>
                    <span className="run-meta">
                      <span
                        className="mini-bar"
                        role="img"
                        aria-label={`${counts.RECONCILED || 0} reconciled, ${
                          counts.EXCEPTION || 0
                        } exceptions, ${counts.HUMAN_REVIEW || 0} in review`}
                      >
                        {["RECONCILED", "EXCEPTION", "HUMAN_REVIEW"].map((k) => (
                          <span
                            key={k}
                            className="mini-seg"
                            style={{
                              width: `${((counts[k] || 0) / total) * 100}%`,
                              background: OUTCOME_COLOR[k],
                            }}
                          />
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
  const shown = useCountUp(Number(value) || 0);
  return (
    <motion.div className="stat-tile" {...riseIn}>
      <div className="stat-label">{label}</div>
      <div className={`stat-value${tone ? ` stat-value-${tone}` : ""}`}>
        {count(Math.round(shown))}
      </div>
      {note && <div className="stat-note">{note}</div>}
    </motion.div>
  );
}
