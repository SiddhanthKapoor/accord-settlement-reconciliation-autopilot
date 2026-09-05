import { useCallback, useEffect, useState } from "react";
import { motion } from "motion/react";
import { listRuns } from "../api.js";
import { pageTransition } from "../motion.js";
import { Link, navigate } from "../router.jsx";
import { count } from "./MoneyFlow.jsx";
import "../workspace.css";

const OUTCOME_COLOR = {
  RECONCILED: "var(--pass, #046c43)",
  EXCEPTION: "var(--fail, #d51b30)",
  HUMAN_REVIEW: "var(--warn, #b3550a)",
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

  const empty = runs !== null && runs.length === 0;

  return (
    <motion.div className="page" {...pageTransition}>
      <header className="wk-hd">
        <h1 className="wk-hd-title">Reconciliation workspaces</h1>
        <p className="wk-hd-sub">
          Each workspace reconciles a set of uploaded sources against each other. Deterministic
          matching resolves what it can prove; genuinely ambiguous records go to the semantic
          classifier, and anything still unresolved goes to a person.
        </p>
      </header>

      {!empty && (
        <div className="wk-stats">
          <Stat label="Records processed" value={totals.processed} />
          <Stat label="Reconciled" value={totals.reconciled} tone="pass" />
          <Stat label="Exceptions" value={totals.exceptions} tone="fail" />
          <Stat label="Human review" value={totals.review} tone="warn" />
        </div>
      )}

      <section className="wk-block" aria-labelledby="wk-runs-heading">
        <div className="wk-block-head">
          <div className="wk-block-titles">
            <h2 className="wk-h2" id="wk-runs-heading">
              {runs === null
                ? "Loading…"
                : `${count(runs.length)} workspace${runs.length === 1 ? "" : "s"}`}
            </h2>
          </div>
          <div className="wk-block-actions">
            <Link to="/app/runs/new" className="btn-primary btn-sm">
              New workspace
            </Link>
          </div>
        </div>

        {empty && (
          <div className="wk-empty">
            No workspaces yet. Start one by dropping in the files you already have — orders, a
            gateway payout export, a bank statement — or load the sample workspace to see a
            reconciliation end to end.
          </div>
        )}

        {runs !== null && runs.length > 0 && (
          <ul className="wk-runs">
            {runs.map((run) => {
              const counts = run.outcome_counts || {};
              const decided = Object.values(counts).reduce((a, b) => a + b, 0);
              const status = String(run.status || "DRAFT").toLowerCase();
              return (
                <li key={run.batch_id}>
                  <button
                    type="button"
                    className="wk-run"
                    onClick={() => navigate(`/app/runs/${encodeURIComponent(run.batch_id)}`)}
                  >
                    <span className={`wk-run-state wk-run-state-${status}`}>{run.status}</span>
                    <span style={{ minWidth: 0 }}>
                      <span className="wk-run-label">{run.label}</span>
                      <span className="wk-run-id">{run.batch_id}</span>
                    </span>
                    <span className="wk-run-split">
                      {decided > 0 && (
                        <span
                          className="wk-run-bar"
                          role="img"
                          aria-label={`${counts.RECONCILED || 0} reconciled, ${
                            counts.EXCEPTION || 0
                          } exceptions, ${counts.HUMAN_REVIEW || 0} in review`}
                        >
                          {["RECONCILED", "EXCEPTION", "HUMAN_REVIEW"].map((k) => (
                            <span
                              key={k}
                              className="wk-run-seg"
                              style={{
                                width: `${((counts[k] || 0) / decided) * 100}%`,
                                background: OUTCOME_COLOR[k],
                              }}
                            />
                          ))}
                        </span>
                      )}
                    </span>
                    <span className="wk-run-count">
                      {count(run.processed_records)} / {count(run.total_records)}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </section>
    </motion.div>
  );
}

/** Not animated, for the same reason as the run summary: a rolling digit
 *  is a value nobody computed. */
function Stat({ label, value, tone }) {
  return (
    <div className="wk-stat">
      <div className="wk-stat-label">{label}</div>
      <div className={`wk-stat-value${tone ? ` wk-stat-value-${tone}` : ""}`}>{count(value)}</div>
    </div>
  );
}
