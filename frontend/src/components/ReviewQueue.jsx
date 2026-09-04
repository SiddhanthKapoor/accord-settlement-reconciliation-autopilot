import { AnimatePresence, motion } from "motion/react";
import { useCallback, useEffect, useState } from "react";
import { getReviewQueue, submitReviewAction } from "../api.js";

const SEVERITY_LABEL = { HIGH: "High", MEDIUM: "Medium", LOW: "Low" };

function money(minor) {
  return `₹${(minor / 100).toLocaleString("en-IN", { minimumFractionDigits: 2 })}`;
}

function humanise(value) {
  return String(value || "").replace(/_/g, " ").toLowerCase();
}

export default function ReviewQueue() {
  const [queue, setQueue] = useState(null);
  const [error, setError] = useState(null);
  const [busyId, setBusyId] = useState(null);
  const [expanded, setExpanded] = useState(null);
  const [notice, setNotice] = useState("");

  const load = useCallback(() => {
    getReviewQueue({ limit: 50 })
      .then((data) => {
        setQueue(data);
        setError(null);
      })
      .catch((e) => setError(e.message));
  }, []);

  useEffect(load, [load]);

  async function act(item, action) {
    setBusyId(item.record_id);
    try {
      await submitReviewAction(item.record_id, { batchId: queue.batch_id, action });
      // The queue is a view over pipeline decisions, so it is re-read
      // rather than patched locally — the screen should never show a
      // state the backend does not actually hold.
      setNotice(`${item.record_id}: ${humanise(action)} recorded in the audit ledger.`);
      load();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusyId(null);
    }
  }

  const summary = queue?.summary;
  const items = queue?.items || [];

  return (
    <div className="page">
      <div className="page-header">
        <h1 className="page-title">Review queue</h1>
        <p className="page-subtitle">
          Records the pipeline refused to decide on its own, worst first. Every action here is written
          to the same hash-chained ledger as the automated decisions, together with the reason the
          automation escalated.
        </p>
      </div>

      {/* Announced to screen readers without stealing focus. */}
      <div className="sr-only" role="status" aria-live="polite">{notice}</div>

      {error && (
        <div className="card" role="alert">
          <p className="small" style={{ color: "var(--fail)" }}>Could not load the queue: {error}</p>
        </div>
      )}

      {summary && (
        <div className="stats-grid" style={{ marginBottom: 22 }}>
          <div className="stat-tile">
            <div className="stat-label">Open items</div>
            <div className="stat-value">{summary.open_count}</div>
          </div>
          <div className="stat-tile">
            <div className="stat-label">Value awaiting review</div>
            <div className="stat-value">{money(summary.open_amount_minor || 0)}</div>
          </div>
          {Object.entries(summary.by_exception_type || {})
            .sort((a, b) => b[1].count - a[1].count)
            .slice(0, 3)
            .map(([type, stats]) => (
              <div className="stat-tile" key={type}>
                <div className="stat-label">{humanise(type)}</div>
                <div className="stat-value">{stats.count}</div>
                <div className="stat-note">{money(stats.amount_minor || 0)}</div>
              </div>
            ))}
        </div>
      )}

      <div className="card">
        <h2 className="card-title">
          {queue === null ? "Loading…" : `${items.length} item${items.length === 1 ? "" : "s"} awaiting review`}
        </h2>

        {queue !== null && items.length === 0 && (
          <p className="muted small" style={{ padding: "18px 0" }}>
            Nothing is waiting. Run a batch from the Console, or every escalated record has been actioned.
          </p>
        )}

        <ul className="review-list">
          {items.map((item) => {
            const open = expanded === item.record_id;
            return (
              <li key={item.record_id} className="review-item">
                <div className="review-head">
                  <span className={`severity severity-${(item.severity || "LOW").toLowerCase()}`}>
                    <span aria-hidden="true" className="severity-dot" />
                    {SEVERITY_LABEL[item.severity] || "Low"}
                  </span>
                  <span className="review-type">{humanise(item.exception_type)}</span>
                  <span className="mono tiny">{item.record_id}</span>
                  <span className="review-amount mono">{money(item.merchant?.amount_minor || 0)}</span>
                </div>

                <p className="review-explanation">{item.explanation}</p>
                <p className="review-action-hint">
                  <span className="label-inline">Recommended</span> {item.recommended_action}
                </p>

                <div className="review-controls">
                  {item.available_actions.map((action) => (
                    <button
                      key={action.action}
                      type="button"
                      className="btn-small"
                      disabled={busyId === item.record_id}
                      onClick={() => act(item, action.action)}
                      title={action.description}
                    >
                      {action.label}
                    </button>
                  ))}
                  <button
                    type="button"
                    className="btn-ghost"
                    aria-expanded={open}
                    aria-controls={`evidence-${item.record_id}`}
                    onClick={() => setExpanded(open ? null : item.record_id)}
                  >
                    {open ? "Hide evidence" : "Show evidence"}
                  </button>
                </div>

                <AnimatePresence initial={false}>
                  {open && (
                    <motion.div
                      id={`evidence-${item.record_id}`}
                      initial={{ height: 0, opacity: 0 }}
                      animate={{ height: "auto", opacity: 1 }}
                      exit={{ height: 0, opacity: 0 }}
                      transition={{ duration: 0.2 }}
                      style={{ overflow: "hidden" }}
                    >
                      <div className="review-evidence">
                        <h3 className="evidence-heading">Candidates considered</h3>
                        {item.considered_candidates.length === 0 && (
                          <p className="tiny muted">No settlement records were retrieved for this order.</p>
                        )}
                        <div className="table-scroll">
                          <table className="records-table">
                            <caption className="sr-only">
                              Settlement records considered for {item.record_id}
                            </caption>
                            <thead>
                              <tr>
                                <th scope="col">Payment</th>
                                <th scope="col">Reference</th>
                                <th scope="col">Amount</th>
                                <th scope="col">Admitted</th>
                                <th scope="col">Evidence</th>
                              </tr>
                            </thead>
                            <tbody>
                              {item.considered_candidates.map((c) => (
                                <tr key={c.payment_id}>
                                  <td className="mono tiny">{c.payment_id}</td>
                                  <td className="mono tiny">{c.order_reference}</td>
                                  <td className="mono tiny">{money(c.gross_amount_minor)}</td>
                                  <td className="tiny">
                                    {c.admissible ? "yes" : "no"}
                                    <span className="muted"> — {c.admissibility_reason}</span>
                                  </td>
                                  <td className="tiny">
                                    {c.supporting_signals.length > 0 && (
                                      <div>+ {c.supporting_signals.join("; ")}</div>
                                    )}
                                    {c.contradicting_signals.length > 0 && (
                                      <div className="muted">− {c.contradicting_signals.join("; ")}</div>
                                    )}
                                  </td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </div>
                        <p className="tiny muted" style={{ marginTop: 8 }}>
                          Automated reason: {item.reason}
                        </p>
                      </div>
                    </motion.div>
                  )}
                </AnimatePresence>
              </li>
            );
          })}
        </ul>
      </div>
    </div>
  );
}
