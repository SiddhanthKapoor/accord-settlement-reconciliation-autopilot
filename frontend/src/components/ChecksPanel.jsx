function StatusBadge({ status }) {
  const cls = { PASS: "badge badge-pass", WARN: "badge badge-warn", FAIL: "badge badge-fail" }[status] || "badge";
  return <span className={cls}>{status}</span>;
}

export default function ChecksPanel({ decision }) {
  if (!decision) return null;

  const outcomeCls =
    decision.outcome === "ALLOW" ? "decision-banner decision-allow" :
    decision.outcome === "BLOCK" ? "decision-banner decision-block" :
    "decision-banner decision-warn";
  const outcomeLabel =
    decision.outcome === "ALLOW" ? "ALLOWED" :
    decision.outcome === "BLOCK" ? "BLOCKED" : "RECONFIRMATION REQUIRED";

  const semanticCheck = decision.checks.find((c) => c.name === "product_identity" && c.confidence != null);

  return (
    <div>
      <div className={outcomeCls}>
        <div>
          <div className="decision-title">{outcomeLabel}</div>
          <div className="decision-reason">{decision.reason}</div>
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

      <div className="card">
        <div className="card-title">Integrity checks</div>
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
    </div>
  );
}
