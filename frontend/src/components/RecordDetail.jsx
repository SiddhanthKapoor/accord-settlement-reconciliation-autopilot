import { motion } from "motion/react";
import { useEffect, useState } from "react";
import { getRecord } from "../api.js";

const OUTCOME_TONE = { RECONCILED: "allow", EXCEPTION: "block", HUMAN_REVIEW: "warn" };
const STATUS_BADGE = { PASS: "badge-pass", WARN: "badge-warn", FAIL: "badge-fail" };

function money(minor) {
  return `₹${(minor / 100).toLocaleString("en-IN", { minimumFractionDigits: 2 })}`;
}

export default function RecordDetail({ recordId, onClose }) {
  const [record, setRecord] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    setRecord(null);
    setError(null);
    getRecord(recordId).then(setRecord).catch((e) => setError(e.message));
  }, [recordId]);

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
      <div className="detail-title">{recordId}</div>
      {record?.ground_truth_case && <div className="tiny muted">synthetic case: {record.ground_truth_case}</div>}

      {error && <p className="small" style={{ color: "var(--fail)" }}>{error}</p>}
      {!record && !error && <p className="muted small">Loading…</p>}

      {record && (
        <>
          <div className={`decision-banner decision-${tone}`} style={{ marginTop: 16 }}>
            <div>
              <div className="decision-status-line">{record.outcome.replace("_", " ")}</div>
              <div className="decision-reason">{record.reason}</div>
            </div>
          </div>

          <div className="detail-section">
            <div className="card-title">Merchant vs. Razorpay</div>
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
              <div className="card-title">Semantic classifier</div>
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
              <div className="card-title">Semantic classifier</div>
              <p className="small muted">Not invoked — resolved deterministically.</p>
            </div>
          )}

          <div className="detail-section">
            <div className="card-title">Deterministic checks</div>
            <table className="checks-table">
              <thead><tr><th>Check</th><th>Status</th><th>Detail</th></tr></thead>
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
            <div className="card-title">Audit history</div>
            {record.audit_trail.map((e) => (
              <div key={e.seq} className="small" style={{ padding: "6px 0", borderBottom: "1px solid var(--border-subtle)" }}>
                <span className="mono tiny muted">#{e.seq}</span> {e.event_type} <span className="tiny muted">{new Date(e.timestamp).toLocaleTimeString()}</span>
              </div>
            ))}
          </div>

          <div className="tiny muted" style={{ marginTop: 16 }}>processed in {record.latency_ms.toFixed(2)}ms</div>
        </>
      )}
    </motion.div>
  );
}
