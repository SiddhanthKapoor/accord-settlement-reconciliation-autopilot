import { motion } from "motion/react";
import { useEffect, useRef, useState } from "react";
import { getRecord } from "../api.js";
import { backdrop, slideOverWide } from "../motion.js";
import Investigator from "./Investigator.jsx";
import { money } from "./MoneyFlow.jsx";
import "../workspace.css";

const OUTCOME_TONE = { RECONCILED: "allow", EXCEPTION: "block", HUMAN_REVIEW: "warn" };
const STATUS_BADGE = { PASS: "badge-pass", WARN: "badge-warn", FAIL: "badge-fail" };

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * Focus containment for a slide-over.
 *
 * A panel that covers the page has to behave like a dialog: Escape closes
 * it, Tab cycles inside it rather than wandering into the table
 * underneath, and focus goes back to whatever opened it. Without this a
 * keyboard user tabs into content they cannot see.
 */
export function useDialogFocus(ref, onClose) {
  const returnTo = useRef(null);

  useEffect(() => {
    returnTo.current = document.activeElement;
    const node = ref.current;
    if (node) {
      const first = node.querySelector(FOCUSABLE);
      (first || node).focus({ preventScroll: true });
    }
    return () => {
      const target = returnTo.current;
      if (target && typeof target.focus === "function" && document.contains(target)) {
        target.focus({ preventScroll: true });
      }
    };
  }, [ref]);

  useEffect(() => {
    function onKeyDown(event) {
      if (event.key === "Escape") {
        event.stopPropagation();
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const node = ref.current;
      if (!node) return;
      const items = Array.from(node.querySelectorAll(FOCUSABLE)).filter(
        (el) => el.offsetParent !== null || el === document.activeElement
      );
      if (items.length === 0) {
        event.preventDefault();
        node.focus({ preventScroll: true });
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
    document.addEventListener("keydown", onKeyDown, true);
    return () => document.removeEventListener("keydown", onKeyDown, true);
  }, [ref, onClose]);
}

export default function RecordDetail({ recordId, batchId, onClose }) {
  const [record, setRecord] = useState(null);
  const [error, setError] = useState(null);
  const panelRef = useRef(null);

  useDialogFocus(panelRef, onClose);

  useEffect(() => {
    setRecord(null);
    setError(null);
    getRecord(recordId, batchId)
      .then(setRecord)
      .catch((e) => setError(e.message));
  }, [recordId, batchId]);

  const considered = considerationsOf(record);
  const matched = record?.candidates?.find((c) => c.payment_id === record.matched_payment_id);
  const tone = record ? OUTCOME_TONE[record.outcome] || "warn" : "warn";
  const currency = record?.merchant?.currency || "INR";

  return (
    <>
      <motion.div className="wk-scrim" {...backdrop} onClick={onClose} aria-hidden="true" />
      <motion.div
        className="wk-panel wk-panel-wide"
        role="dialog"
        aria-modal="true"
        aria-labelledby="wk-record-title"
        ref={panelRef}
        tabIndex={-1}
        {...slideOverWide}
      >
        <div className="wk-panel-head">
          <div>
            <h2 className="wk-panel-title" id="wk-record-title">
              {record?.merchant?.order_id || recordId}
            </h2>
            <div className="wk-panel-sub">
              {record?.merchant?.reference_id ? `ref ${record.merchant.reference_id} · ` : ""}
              {recordId}
              {batchId ? ` · ${batchId}` : ""}
            </div>
          </div>
          <button type="button" className="wk-panel-close" onClick={onClose} aria-label="Close record">
            <span aria-hidden="true">✕</span>
          </button>
        </div>

        <div className="wk-panel-body">
          {error && (
            <p className="wk-note wk-note-bad" role="alert">
              {error}
            </p>
          )}
          {!record && !error && <p className="muted small">Loading…</p>}

          {record && (
            <>
              <div className={`decision-banner decision-${tone}`}>
                <div>
                  <div className="decision-status-line">
                    {record.outcome.replace("_", " ")}
                    {record.exception_type && (
                      <span className="decision-type">
                        {" "}· {record.exception_type.replace(/_/g, " ").toLowerCase()}
                      </span>
                    )}
                    {record.severity && (
                      <span
                        className={`severity severity-${record.severity.toLowerCase()}`}
                        style={{ marginLeft: 8 }}
                      >
                        <span aria-hidden="true" className="severity-dot" />
                        {record.severity.toLowerCase()} priority
                      </span>
                    )}
                  </div>
                  <div className="decision-reason">{record.explanation || record.reason}</div>
                  {record.recommended_action && (
                    <div className="decision-next">
                      <span className="label-inline">Next</span> {record.recommended_action}
                    </div>
                  )}
                </div>
              </div>

              <Investigator recordId={recordId} batchId={batchId} record={record} />

              {considered.length > 0 && (
                <div className="wk-section">
                  <h3 className="wk-h2">Candidates considered</h3>
                  <div className="table-scroll">
                    <table className="checks-table">
                      <caption className="sr-only">
                        Settlement records considered for {recordId}
                      </caption>
                      <thead>
                        <tr>
                          <th scope="col">Payment</th>
                          <th scope="col">Amount</th>
                          <th scope="col">Admitted</th>
                          <th scope="col">Evidence</th>
                        </tr>
                      </thead>
                      <tbody>
                        {considered.map((c) => (
                          <tr
                            key={c.payment_id}
                            className={c.payment_id === record.matched_payment_id ? "row-pass" : ""}
                          >
                            <td className="mono tiny">{c.payment_id}</td>
                            <td className="mono tiny">{money(c.gross_amount_minor, { currency })}</td>
                            <td className="tiny">
                              {c.admissible ? "yes" : "no"}
                              {c.admissibility_reason && (
                                <span className="muted"> — {c.admissibility_reason}</span>
                              )}
                            </td>
                            <td className="tiny">
                              {c.supporting_signals?.length > 0 && (
                                <div>+ {c.supporting_signals.join("; ")}</div>
                              )}
                              {c.contradicting_signals?.length > 0 && (
                                <div className="muted">− {c.contradicting_signals.join("; ")}</div>
                              )}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}

              <div className="wk-section">
                <h3 className="wk-h2">Ledger against settlement</h3>
                <div className="compare-grid">
                  <div className="compare-col">
                    <div className="compare-col-label">Ledger record</div>
                    <Line label="Order" value={record.merchant.order_id} />
                    <Line label="Reference" value={record.merchant.reference_id || "—"} />
                    <Line label="Amount" value={money(record.merchant.amount_minor, { currency })} />
                    <Line label="Status" value={record.merchant.status} />
                    <Line label="Description" value={record.merchant.description} />
                  </div>
                  <div className="compare-col">
                    <div className="compare-col-label">
                      Settlement record
                      {record.candidate_count > 1 ? ` (1 of ${record.candidate_count})` : ""}
                    </div>
                    {matched ? (
                      <>
                        <Line label="Payment" value={matched.payment_id} />
                        <Line label="Gross" value={money(matched.gross_amount_minor, { currency })} />
                        <Line
                          label="Fee + tax"
                          value={money((matched.fee_minor || 0) + (matched.tax_minor || 0), { currency })}
                        />
                        <Line label="Net" value={money(matched.net_amount_minor, { currency })} />
                        <Line
                          label="Settled"
                          value={
                            matched.settlement_date
                              ? new Date(matched.settlement_date).toLocaleDateString()
                              : "—"
                          }
                        />
                      </>
                    ) : (
                      <p className="small muted">No matched settlement record.</p>
                    )}
                  </div>
                </div>
              </div>

              <div className="wk-section">
                <h3 className="wk-h2">Deterministic checks</h3>
                <div className="table-scroll">
                  <table className="checks-table">
                    <caption className="sr-only">Deterministic checks run for {recordId}</caption>
                    <thead>
                      <tr>
                        <th scope="col">Check</th>
                        <th scope="col">Status</th>
                        <th scope="col">Detail</th>
                      </tr>
                    </thead>
                    <tbody>
                      {record.checks.map((c, i) => (
                        <tr
                          key={i}
                          className={
                            c.status === "FAIL" ? "row-fail" : c.status === "WARN" ? "row-warn" : ""
                          }
                        >
                          <td className="mono tiny">{c.name}</td>
                          <td>
                            <span className={"badge " + STATUS_BADGE[c.status]}>{c.status}</span>
                          </td>
                          <td className="tiny">{c.detail}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>

              <details className="wk-section disclosure">
                <summary className="card-title disclosure-summary">Technical detail</summary>
                <dl className="kv-list">
                  <dt>Match classification</dt>
                  <dd className="mono tiny">{record.classification}</dd>
                  <dt>Policy threshold</dt>
                  <dd className="mono tiny">{record.policy_threshold}</dd>
                  <dt>Semantic classifier</dt>
                  <dd className="mono tiny">
                    {record.ai_invoked
                      ? `${record.ai_backend} · confidence ${
                          record.ai_confidence?.toFixed(2) ?? "n/a"
                        } · ${record.ai_calls} call(s)`
                      : "not invoked — resolved deterministically"}
                  </dd>
                  <dt>Processing time</dt>
                  <dd className="mono tiny">{record.latency_ms?.toFixed(2)} ms</dd>
                </dl>
              </details>

              <div className="wk-section">
                <h3 className="wk-h2">Audit history</h3>
                {record.audit_trail.map((e) => (
                  <div
                    key={e.seq}
                    className="small"
                    style={{ padding: "6px 0", borderBottom: "1px solid var(--border-subtle)" }}
                  >
                    <span className="mono tiny muted">#{e.seq}</span> {e.event_type}{" "}
                    <span className="tiny muted">{new Date(e.timestamp).toLocaleTimeString()}</span>
                  </div>
                ))}
              </div>
            </>
          )}
        </div>
      </motion.div>
    </>
  );
}

function Line({ label, value }) {
  return (
    <div className="compare-line">
      <span className="compare-line-label">{label}</span>
      <span className="compare-line-value">{value}</span>
    </div>
  );
}

/**
 * `GET /records/{id}` leaves the considered-candidates column as raw JSON
 * while the review queue hydrates it. Read both, so this panel shows the
 * rejected-candidate reasoning wherever it is opened from.
 */
function considerationsOf(record) {
  if (!record) return [];
  if (Array.isArray(record.considered_candidates)) return record.considered_candidates;
  if (typeof record.considered_json === "string") {
    try {
      const parsed = JSON.parse(record.considered_json);
      if (Array.isArray(parsed)) return parsed;
    } catch {
      /* unparseable column — show nothing rather than something invented */
    }
  }
  return [];
}
