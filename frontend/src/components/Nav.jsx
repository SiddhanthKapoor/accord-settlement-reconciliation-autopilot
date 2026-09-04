import { motion } from "motion/react";

const TABS = [
  { key: "console", label: "Console" },
  { key: "review", label: "Review Queue" },
  { key: "audit", label: "Audit Trail" },
];

export default function Nav({ active, onChange, aiBackend }) {
  return (
    <header className="topnav">
      <div className="topnav-left">
        <div className="brand-mark">
          <svg className="brand-glyph" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
            <path d="M4 12h5l2-6 4 12 2-6h5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
          <span className="brand-name">Reconciliation Autopilot</span>
        </div>
      </div>
      <nav className="tabs" aria-label="Sections">
        {TABS.map((t) => (
          <button
            key={t.key}
            type="button"
            className={"tab" + (active === t.key ? " tab-active" : "")}
            aria-current={active === t.key ? "page" : undefined}
            onClick={() => onChange(t.key)}
          >
            {active === t.key && (
              <motion.span className="tab-indicator" layoutId="tab-indicator" transition={{ duration: 0.25, ease: "easeOut" }} />
            )}
            <span className="tab-label">{t.label}</span>
          </button>
        ))}
      </nav>
      <div className="topnav-right">
        <span className="status-chip">
          <span className="status-dot" aria-hidden="true" />
          Engine Operational
        </span>
        {aiBackend && <span className="status-chip mono">{aiBackend}</span>}
      </div>
    </header>
  );
}
