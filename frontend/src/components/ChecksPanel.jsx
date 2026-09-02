function StatusBadge({ status }) {
  const cls = { PASS: "badge badge-pass", WARN: "badge badge-warn", FAIL: "badge badge-fail", SKIPPED: "badge" }[status] || "badge";
  return <span className={cls}>{status}</span>;
}

export default function ChecksPanel({ decision }) {
  if (!decision) {
    return (
      <div className="panel">
        <h3>Integrity checks</h3>
        <p className="muted">Run a scenario to see checks execute here.</p>
      </div>
    );
  }
  const outcomeCls =
    decision.outcome === "ALLOW" ? "decision-banner decision-allow" :
    decision.outcome === "BLOCK" ? "decision-banner decision-block" :
    "decision-banner decision-warn";

  return (
    <div className="panel">
      <h3>Integrity checks</h3>
      <div className={outcomeCls}>
        <strong>{decision.outcome.replace(/_/g, " ")}</strong>
        <div className="decision-reason">{decision.reason}</div>
      </div>
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
              <td className="mono small">{c.expected ?? "—"}</td>
              <td className="mono small">{c.observed ?? "—"}</td>
              <td className="small">
                {c.detail}
                {c.confidence != null && <span className="muted"> (confidence {c.confidence})</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
