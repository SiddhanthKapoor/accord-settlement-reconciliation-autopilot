import { motion } from "motion/react";
import { useEffect, useState } from "react";
import { getRecord } from "../api.js";

const OUTCOME_TONE = { RECONCILED: "allow", EXCEPTION: "block", HUMAN_REVIEW: "warn" };
const STATUS_BADGE = { PASS: "badge-pass", WARN: "badge-warn", FAIL: "badge-fail" };

function money(minor) {
  return `₹${(minor / 100).toLocaleString("en-IN", { minimumFractionDigits: 2 })}`;
}

export default function RecordDetail({ recordId, batchId, onClose }) {
  const [record, setRecord] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    setRecord(null);
    setError(null);
    getRecord(recordId, batchId).then(setRecord).catch((e) => setError(e.message));
  }, [recordId, batchId]);

  const matched = record?.candidates?.find((c) => c.payment_id === record.matched_payment_id);
  const tone = record ? OUTCOME_TONE[record.outcome] || "warn" : "warn";

  return (
    <motion.div
      className="detail-panel"
      initial={{ x: 480, opacity: 0 }}
      animate={{ x: 0, opacity: 1 }}
      exit={{ x: 480, opacity: 0 }}
      transition={{ duration: 0.22, ease: "easeOut" }}
    >
      <button className="detail-close" onClick={onClose} aria-label="Close">✕</button>
      <h2 className="detail-title">{recordId}</h2>
      {record?.ground_truth_case && <div className="tiny muted">synthetic case: {record.ground_truth_case}</div>}

      {error && <p className="small" style={{ color: "var(--fail)" }}>{error}</p>}
      {!record && !error && <p className="muted small">Loading…</p>}

      {record && (
        <>
          <div className={`decision-banner decision-${tone}`} style={{ marginTop: 16 }}>
            <div>
              <div className="decision-status-line">
                {record.outcome.replace("_", " ")}
                {record.exception_type && (
                  <span className="decision-type"> · {record.exception_type.replace(/_/g, " ").toLowerCase()}</span>
                )}
                {record.severity && (
                  <span className={`severity severity-${record.severity.toLowerCase()}`} style={{ marginLeft: 8 }}>
                    <span aria-hidden="true" className="severity-dot" />
                    {record.severity.toLowerCase()} priority
                  </span>
                )}
              </div>
              {/* Level 1: what happened, in the operator's language. */}
              <div className="decision-reason">{record.explanation || record.reason}</div>
              {record.recommended_action && (
                <div className="decision-next"><span className="label-inline">Next</span> {record.recommended_action}</div>
              )}
            </div>
          </div>

          {/* Level 2: the candidates weighed, and what argued for and
              against each. This is the part that makes a refusal
              checkable instead of merely stated. */}
          {record.considered_candidates?.length > 0 && (
            <div className="detail-section">
              <h3 className="card-title">Candidates considered</h3>
              <div className="table-scroll">
                <table className="checks-table">
                  <caption className="sr-only">Settlement records considered for {recordId}</caption>
                  <thead>
                    <tr>
                      <th scope="col">Payment</th>
                      <th scope="col">Amount</th>
                      <th scope="col">Admitted</th>
                      <th scope="col">Evidence</th>
                    </tr>
                  </thead>
                  <tbody>
                    {record.considered_candidates.map((c) => (
                      <tr key={c.payment_id} className={c.payment_id === record.matched_payment_id ? "row-pass" : ""}>
                        <td className="mono tiny">{c.payment_id}</td>
                        <td className="mono tiny">{money(c.gross_amount_minor)}</td>
                        <td className="tiny">{c.admissible ? "yes" : "no"}<span className="muted"> — {c.admissibility_reason}</span></td>
                        <td className="tiny">
                          {c.supporting_signals.length > 0 && <div>+ {c.supporting_signals.join("; ")}</div>}
                          {c.contradicting_signals.length > 0 && <div className="muted">− {c.contradicting_signals.join("; ")}</div>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          <div className="detail-section">
            <h3 className="card-title">Merchant vs. Razorpay</h3>
            <div className="compare-grid">
              <div className="compare-col">
                <div className="compare-col-label">Merchant record</div>
                <div className="compare-line"><span className="compare-line-label">Order</span><span className="compare-line-value">{record.merchant.order_id}</span></div>
                <div className="compare-line"><span className="compare-line-label">Reference</span><span className="compare-line-value">{record.merchant.reference_id || "—"}</span></div>
                <div className="compare-line"><span className="compare-line-label">Amount</span><span className="compare-line-value">{money(record.merchant.amount_minor)}</span></div>
                <div className="compare-line"><span className="compare-line-label">Status</span><span className="compare-line-value">{record.merchant.status}</span></div>
                <div className="compare-line"><span className="compare-line-label">Description</span><span className="compare-line-value">{record.merchant.description}</span></div>
              </div>
              <div className="compare-col">
                <div className="compare-col-label">Razorpay record{record.candidate_count > 1 ? ` (1 of ${record.candidate_count})` : ""}</div>
                {matched ? (
                  <>
                    <div className="compare-line"><span className="compare-line-label">Payment</span><span className="compare-line-value">{matched.payment_id}</span></div>
                    <div className="compare-line"><span className="compare-line-label">Gross</span><span className="compare-line-value">{money(matched.gross_amount_minor)}</span></div>
                    <div className="compare-line"><span className="compare-line-label">Fee + tax</span><span className="compare-line-value">{money(matched.fee_minor + matched.tax_minor)}</span></div>
                    <div className="compare-line"><span className="compare-line-label">Net</span><span className="compare-line-value">{money(matched.net_amount_minor)}</span></div>
                    <div className="compare-line"><span className="compare-line-label">Settled</span><span className="compare-line-value">{new Date(matched.settlement_date).toLocaleDateString()}</span></div>
                  </>
                ) : (
                  <p className="small muted">No matched settlement record.</p>
                )}
              </div>
            </div>
          </div>

          {record.ai_invoked ? (
            <div className="detail-section">
              <h3 className="card-title">Semantic classifier</h3>
              <div className="ai-panel">
                <div><strong>{record.ai_backend}</strong></div>
                <div className="small" style={{ marginTop: 4 }}>
                  confidence {record.ai_confidence?.toFixed(2)} — policy threshold {record.policy_threshold}
                  {record.ai_confidence != null && record.ai_confidence < record.policy_threshold && (
                    <span style={{ color: "var(--warn)", fontWeight: 600 }}> — below threshold, cannot auto-reconcile</span>
                  )}
                </div>
              </div>
            </div>
          ) : (
            <div className="detail-section">
              <h3 className="card-title">Semantic classifier</h3>
              <p className="small muted">
                Not invoked — resolved deterministically. The policy threshold of{" "}
                {record.policy_threshold} applies only to model-resolved matches.
              </p>
            </div>
          )}

          <details className="detail-section disclosure">
            <summary className="card-title disclosure-summary">Technical detail</summary>
            <dl className="kv-list">
              <dt>Match classification</dt><dd className="mono tiny">{record.classification}</dd>
              <dt>Policy threshold</dt><dd className="mono tiny">{record.policy_threshold}</dd>
              <dt>Semantic classifier</dt>
              <dd className="mono tiny">
                {record.ai_invoked
                  ? `${record.ai_backend} · confidence ${record.ai_confidence?.toFixed(2) ?? "n/a"} · ${record.ai_calls} call(s)`
                  : "not invoked — resolved deterministically"}
              </dd>
              <dt>Processing time</dt><dd className="mono tiny">{record.latency_ms.toFixed(2)} ms</dd>
            </dl>
          </details>

          <div className="detail-section">
            <h3 className="card-title">Deterministic checks</h3>
            <table className="checks-table">
              <caption className="sr-only">Deterministic checks run for {recordId}</caption>
              <thead><tr><th scope="col">Check</th><th scope="col">Status</th><th scope="col">Detail</th></tr></thead>
              <tbody>
                {record.checks.map((c, i) => (
                  <tr key={i} className={c.status === "FAIL" ? "row-fail" : c.status === "WARN" ? "row-warn" : ""}>
                    <td className="mono">{c.name}</td>
                    <td><span className={"badge " + STATUS_BADGE[c.status]}>{c.status}</span></td>
                    <td>{c.detail}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="detail-section">
            <h3 className="card-title">Audit history</h3>
            {record.audit_trail.map((e) => (
              <div key={e.seq} className="small" style={{ padding: "6px 0", borderBottom: "1px solid var(--border-subtle)" }}>
                <span className="mono tiny muted">#{e.seq}</span> {e.event_type} <span className="tiny muted">{new Date(e.timestamp).toLocaleTimeString()}</span>
              </div>
            ))}
          </div>


        </>
      )}
    </motion.div>
  );
}
