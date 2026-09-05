import { AnimatePresence, motion } from "motion/react";
import { useCallback, useEffect, useState } from "react";
import { getReviewQueue, submitReviewAction } from "../api.js";
import { DURATION, EASE, expand, listIndexDelay, pageTransition, riseIn, useCountUp } from "../motion.js";
import Investigator from "./Investigator.jsx";
import { count, money } from "./MoneyFlow.jsx";
import "../workspace.css";

const SEVERITY_LABEL = { HIGH: "High", MEDIUM: "Medium", LOW: "Low" };

function humanise(value) {
  return String(value || "").replace(/_/g, " ").toLowerCase();
}

/**
 * Work the pipeline refused to decide on its own.
 *
 * The action buttons are whatever the backend says are available for this
 * record and nothing else. That is deliberate and load-bearing: where the
 * amounts or currencies themselves disagree, the backend withholds
 * "approve match", because the dispute is not about *which* settlement
 * this is — and reconciling a record whose amount is known to be wrong is
 * exactly the failure this product exists to prevent. Hard-coding a
 * button row here would quietly undo that at the last step, so this
 * component never invents an action.
 */
export default function ReviewQueue() {
  const [queue, setQueue] = useState(null);
  const [error, setError] = useState(null);
  const [busyId, setBusyId] = useState(null);
  const [expanded, setExpanded] = useState(null);
  const [investigating, setInvestigating] = useState(null);
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
    <motion.div className="page" {...pageTransition}>
      <div className="page-header">
        <h1 className="page-title">Review queue</h1>
        <p className="page-subtitle">
          Records the pipeline refused to decide on its own, worst first. Every action here is
          written to the same hash-chained ledger as the automated decisions, together with the
          reason the automation escalated.
        </p>
      </div>

      {/* Announced to screen readers without stealing focus. */}
      <div className="sr-only" role="status" aria-live="polite">{notice}</div>

      {error && (
        <p className="wk-note wk-note-bad" role="alert" style={{ marginBottom: 16 }}>
          Could not load the queue: {error}
        </p>
      )}

      {summary && (
        <div className="wk-outcomes" style={{ marginBottom: 22 }}>
          <SummaryTile label="Open items" value={summary.open_count} />
          <SummaryTile label="Value awaiting review" money value={summary.open_amount_minor || 0} />
          {Object.entries(summary.by_exception_type || {})
            .sort((a, b) => b[1].count - a[1].count)
            .slice(0, 3)
            .map(([type, stats]) => (
              <SummaryTile
                key={type}
                label={humanise(type)}
                value={stats.count}
                note={money(stats.amount_minor || 0)}
              />
            ))}
        </div>
      )}

      <div className="card">
        <h2 className="card-title">
          {queue === null
            ? "Loading…"
            : `${items.length} item${items.length === 1 ? "" : "s"} awaiting review`}
        </h2>

        {queue !== null && items.length === 0 && (
          <div className="wk-empty">
            Nothing is waiting. Either no run has escalated a record, or every escalated record has
            been actioned.
          </div>
        )}

        <ul className="review-list">
          <AnimatePresence initial={false}>
            {items.map((item, i) => {
              const open = expanded === item.record_id;
              const investigatingThis = investigating === item.record_id;
              const actions = item.available_actions || [];
              // Only claim the money is the problem where that is the only
              // thing it can be: the engine has settled on a counterpart
              // and *still* will not offer approval. Where there is no
              // matched settlement at all, approval is missing because
              // there is nothing to approve — a different fact, and saying
              // otherwise would teach the operator a false model.
              const approveWithheld =
                !!item.matched_payment_id && !actions.some((a) => a.action === "APPROVE_MATCH");

              return (
                <motion.li
                  key={item.record_id}
                  className="review-item"
                  layout
                  initial={{ opacity: 0, y: 8 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, height: 0, paddingTop: 0, paddingBottom: 0 }}
                  transition={{ duration: DURATION.fast, ease: EASE, delay: listIndexDelay(i) }}
                >
                  <div className="review-head">
                    <span className={`severity severity-${(item.severity || "LOW").toLowerCase()}`}>
                      <span aria-hidden="true" className="severity-dot" />
                      {SEVERITY_LABEL[item.severity] || "Low"}
                    </span>
                    <span className="review-type">{humanise(item.exception_type)}</span>
                    <span className="mono tiny">{item.record_id}</span>
                    <span className="review-amount mono">
                      {money(item.merchant?.amount_minor || 0, {
                        currency: item.merchant?.currency,
                      })}
                    </span>
                  </div>

                  <p className="review-explanation">{item.explanation}</p>
                  <p className="review-action-hint">
                    <span className="label-inline">Recommended</span> {item.recommended_action}
                  </p>

                  {approveWithheld && (
                    <p className="wk-note wk-note-warn" style={{ marginTop: 10 }}>
                      <strong>Approve is not offered here.</strong> The dispute is not about which
                      settlement this is — the money itself disagrees — so reconciling it would sign
                      off a figure already known to be wrong.
                    </p>
                  )}

                  <div className="review-controls">
                    {actions.map((action) => (
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
                      aria-expanded={investigatingThis}
                      aria-controls={`wk-investigate-${item.record_id}`}
                      onClick={() =>
                        setInvestigating(investigatingThis ? null : item.record_id)
                      }
                    >
                      {investigatingThis ? "Hide investigation" : "Investigate"}
                    </button>
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
                    {investigatingThis && (
                      <motion.div
                        id={`wk-investigate-${item.record_id}`}
                        {...expand}
                        style={{ overflow: "hidden" }}
                      >
                        <Investigator
                          recordId={item.record_id}
                          batchId={queue.batch_id}
                          record={item}
                          autoFocus
                        />
                      </motion.div>
                    )}
                  </AnimatePresence>

                  <AnimatePresence initial={false}>
                    {open && (
                      <motion.div
                        id={`evidence-${item.record_id}`}
                        {...expand}
                        style={{ overflow: "hidden" }}
                      >
                        <div className="review-evidence">
                          <h3 className="evidence-heading">Candidates considered</h3>
                          {item.considered_candidates.length === 0 && (
                            <p className="tiny muted">
                              No settlement records were retrieved for this order.
                            </p>
                          )}
                          {item.considered_candidates.length > 0 && (
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
                                          <div className="muted">
                                            − {c.contradicting_signals.join("; ")}
                                          </div>
                                        )}
                                      </td>
                                    </tr>
                                  ))}
                                </tbody>
                              </table>
                            </div>
                          )}
                          <p className="tiny muted" style={{ marginTop: 8 }}>
                            Automated reason: {item.reason}
                          </p>
                        </div>
                      </motion.div>
                    )}
                  </AnimatePresence>
                </motion.li>
              );
            })}
          </AnimatePresence>
        </ul>
      </div>
    </motion.div>
  );
}

function SummaryTile({ label, value, note, money: isMoney }) {
  const shown = useCountUp(Number(value) || 0);
  return (
    <motion.div className="wk-outcome" style={{ cursor: "default" }} {...riseIn}>
      <span className="wk-outcome-label">{label}</span>
      <span className="wk-outcome-value">
        {isMoney ? money(Math.round(shown), { decimals: 0 }) : count(Math.round(shown))}
      </span>
      {note && <span className="wk-outcome-note">{note}</span>}
    </motion.div>
  );
}
