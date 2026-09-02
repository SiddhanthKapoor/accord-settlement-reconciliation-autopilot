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
          <svg className="brand-glyph" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
            <rect x="3" y="9" width="12" height="8" rx="4" stroke="currentColor" strokeWidth="2" />
            <rect x="9" y="7" width="12" height="8" rx="4" stroke="currentColor" strokeWidth="2" />
          </svg>
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
