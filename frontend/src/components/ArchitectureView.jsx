export default function ArchitectureView() {
  return (
    <div className="page">
      <div className="page-header">
        <div className="page-title">Architecture</div>
        <div className="page-subtitle">
          Three processes. No microservice sprawl — the one real requirement (atomic
          compare-and-swap under concurrency) is met by SQLite's <span className="mono">BEGIN IMMEDIATE</span>, not a Redis/Kafka footprint.
        </div>
      </div>

      <div className="card" style={{ marginBottom: 20 }}>
        <div className="card-title">Request flow</div>
        <div className="arch-flow">
          <div className="arch-box"><div className="arch-box-title">Agent</div><div className="arch-box-sub">Claude, or any AP2-style flow</div></div>
          <div className="arch-arrow">→</div>
          <div className="arch-box"><div className="arch-box-title">Catalog Service</div><div className="arch-box-sub">independent ground truth · :8100</div></div>
          <div className="arch-arrow">→</div>
          <div className="arch-box" style={{ borderColor: "#c7d4ff", background: "var(--accent-soft)" }}><div className="arch-box-title">Interlock</div><div className="arch-box-sub">deterministic engine + semantic classifier · :8000</div></div>
          <div className="arch-arrow">→</div>
          <div className="arch-box"><div className="arch-box-title">Razorpay</div><div className="arch-box-sub">test-mode execution</div></div>
        </div>
      </div>

      <div className="risk-grid" style={{ gridTemplateColumns: "repeat(2, 1fr)" }}>
        <div className="card">
          <div className="card-title">Two comparison axes</div>
          <p className="small" style={{ lineHeight: 1.6 }}>
            <strong>Payment request vs. commitment</strong> — did the agent's final ask drift from
            what it already committed to? Catches manipulated tool output.<br /><br />
            <strong>Commitment vs. live catalog</strong> — did the world change since the
            commitment was made? Catches merchant-side price/availability drift.
          </p>
        </div>
        <div className="card">
          <div className="card-title">Where AI is used — and where it isn't</div>
          <p className="small" style={{ lineHeight: 1.6 }}>
            Every check is deterministic — exact ID matches, numeric tolerance bands, atomic
            ledger lookups — except one: fuzzy product-name equivalence, where deterministic
            matching is genuinely insufficient. That single case is routed to Gemini
            (<span className="mono">gemini-3.5-flash-lite</span>) with a structured, validated
            schema, and never executes a money action itself.
          </p>
        </div>
      </div>

      <div className="card" style={{ marginTop: 20 }}>
        <div className="card-title">Full write-up</div>
        <p className="small">
          The complete component breakdown, the research trail behind what was ruled out and why
          (AP2, UCP, ACP, Permit.io, Lasso Security, and others), and the reproducible evaluation
          methodology live in the repository:
        </p>
        <p className="small mono" style={{ marginTop: 8 }}>README.md · docs/DECISION_REPORT.md</p>
      </div>
    </div>
  );
}
