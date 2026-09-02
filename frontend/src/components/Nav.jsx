const TABS = [
  { key: "overview", label: "Overview" },
  { key: "scenarios", label: "Scenarios" },
  { key: "audit", label: "Audit Trail" },
  { key: "architecture", label: "Architecture" },
];

export default function Nav({ active, onChange, stats }) {
  return (
    <header className="topnav">
      <div className="topnav-left">
        <div className="brand-mark">
          <div className="brand-glyph">IL</div>
          <span className="brand-name">Interlock</span>
        </div>
        <span className="eco-pill">Built for the Razorpay AI Buildathon · Agentic Payments</span>
      </div>
      <nav className="tabs">
        {TABS.map((t) => (
          <div
            key={t.key}
            className={"tab" + (active === t.key ? " tab-active" : "")}
            onClick={() => onChange(t.key)}
          >
            {t.label}
          </div>
        ))}
      </nav>
      <div className="topnav-right">
        <span className="status-chip">
          <span className="status-dot" />
          Integrity Engine Operational
        </span>
        {stats && (
          <span className="status-chip mono">
            {stats.semantic_provider} · {stats.razorpay_configured ? "razorpay live" : "razorpay simulated"}
          </span>
        )}
      </div>
    </header>
  );
}
