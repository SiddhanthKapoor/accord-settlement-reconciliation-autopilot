import { humanizeDecision } from "../decisionCopy.js";

function StatusBadge({ status }) {
  const cls = { PASS: "badge badge-pass", WARN: "badge badge-warn", FAIL: "badge badge-fail" }[status] || "badge";
  return <span className={cls}>{status}</span>;
}

export default function ChecksPanel({ decision }) {
  if (!decision) return null;

  const { statusLine, headline, tone } = humanizeDecision(decision);
  const outcomeCls = `decision-banner decision-${tone === "allow" ? "allow" : tone === "block" ? "block" : "warn"}`;
  const semanticCheck = decision.checks.find((c) => c.name === "product_identity" && c.confidence != null);

  return (
    <div>
      <div className={outcomeCls} role="status">
        <div>
          <div className="decision-status-line">{statusLine}</div>
          <div className="decision-title">{headline}</div>
        </div>
      </div>

      {semanticCheck && (
        <div className="card" style={{ marginBottom: 14 }}>
          <div className="card-title">Semantic comparison</div>
          <div className="compare-line">
            <span className="compare-line-label">Verified</span>
            <span className="compare-line-value">{semanticCheck.expected}</span>
          </div>
          <div className="compare-line">
            <span className="compare-line-label">Observed</span>
            <span className="compare-line-value">{semanticCheck.observed}</span>
          </div>
          <div className="compare-line">
            <span className="compare-line-label">Result</span>
            <span className="compare-line-value">
              <StatusBadge status={semanticCheck.status} />
            </span>
          </div>
          <div className="small muted" style={{ marginTop: 8, lineHeight: 1.5 }}>
            {semanticCheck.detail.replace(/^\[.*?\]\s*/, "").replace(/^[A-Z_]+:\s*/, "")}
          </div>
        </div>
      )}

      <details className="decision-evidence">
        <summary>Technical evidence ({decision.checks.length} checks) →</summary>
        <div className="card" style={{ marginBottom: 14 }}>
          <div className="decision-reason" style={{ marginBottom: 12 }}>{decision.reason}</div>
          <table className="checks-table">
            <thead>
              <tr>
                <th>Check</th><th>Status</th><th>Threat</th><th>Expected</th><th>Observed</th><th>Detail</th>
              </tr>
            </thead>
            <tbody>
              {decision.checks.map((c, i) => (
                <tr key={i} className={c.status === "FAIL" ? "row-fail" : c.status === "WARN" ? "row-warn" : ""}>
                  <td className="mono">{c.name}</td>
                  <td><StatusBadge status={c.status} /></td>
                  <td>{c.threat_ref ? <span className="threat-badge">{c.threat_ref}</span> : "—"}</td>
                  <td className="mono tiny">{c.expected ?? "—"}</td>
                  <td className="mono tiny">{c.observed ?? "—"}</td>
                  <td className="small">{c.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  );
}
