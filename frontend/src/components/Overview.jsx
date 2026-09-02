import { useEffect, useState } from "react";
import { getStats } from "../api.js";
import Pipeline from "./Pipeline.jsx";

const RISKS = [
  {
    color: "var(--fail)", title: "Replay", threat: "T-31",
    body: "A commitment that already backed a completed payment is presented again.",
    diagram: (
      <div className="risk-diagram">
        <div className="risk-diagram-row"><span>txn_9f2a</span><span>EXECUTED ✓</span></div>
        <div className="risk-diagram-arrow">↓ presented again</div>
        <div className="risk-diagram-row"><span className="risk-diagram-dup">txn_9f2a</span><span className="risk-diagram-diff">REJECTED</span></div>
      </div>
    ),
  },
  {
    color: "var(--warn)", title: "State Mutation", threat: "T-32",
    body: "The transaction reaching payment execution no longer matches what was verified.",
    diagram: (
      <div className="risk-diagram">
        <div className="risk-diagram-row"><span>Verified</span><span>1 × Mouse · ₹1,499</span></div>
        <div className="risk-diagram-arrow">↓</div>
        <div className="risk-diagram-row"><span>Observed</span><span className="risk-diagram-diff">3 × Mouse · ₹4,497</span></div>
      </div>
    ),
  },
  {
    color: "var(--accent)", title: "Shared Budget Race", threat: "T-33",
    body: "Multiple agent sessions spend against the same delegated budget at once.",
    diagram: (
      <div className="risk-diagram">
        <div className="risk-diagram-row"><span>8 agents</span><span>₹2,000 budget</span></div>
        <div className="risk-diagram-arrow">↓ atomic reservation</div>
        <div className="risk-diagram-row"><span>1 acquires</span><span className="risk-diagram-diff">7 rejected</span></div>
      </div>
    ),
  },
];

export default function Overview({ onNavigateScenarios }) {
  const [stats, setStats] = useState(null);

  useEffect(() => {
    let alive = true;
    const load = () => getStats().then((s) => alive && setStats(s)).catch(() => {});
    load();
    const id = setInterval(load, 4000);
    return () => { alive = false; clearInterval(id); };
  }, []);

  return (
    <div className="page">
      <section className="hero">
        <span className="hero-kicker">Transaction Integrity for Agentic Payments</span>
        <h1 className="hero-title">The last integrity check before an AI agent moves money.</h1>
        <p className="hero-sub">
          An agent prepares a transaction. Something can change before the money actually moves —
          the amount, the item, or a stale approval. Interlock re-checks the transaction right at
          the payment execution boundary, immediately before Razorpay is called, and only lets it
          through if it's still exactly what was verified.
        </p>
        <details className="disclosure" style={{ marginTop: 10 }}>
          <summary>How this relates to signed mandates (AP2) →</summary>
          <p className="small muted" style={{ maxWidth: 620, lineHeight: 1.6, marginTop: 8 }}>
            A cryptographically signed mandate proves a transaction was <em>authorized</em>. It
            doesn't prove the transaction is still <em>true</em> at the moment it executes — that
            it hasn't been replayed, hasn't drifted from what was verified, and isn't racing
            another session for the same budget. That's the specific, narrow gap Interlock closes.
          </p>
        </details>

        <div className="pipeline-wrap">
          <Pipeline />
        </div>
      </section>

      <div className="card-title" style={{ marginTop: 8 }}>What it protects against</div>
      <div className="risk-grid">
        {RISKS.map((r) => (
          <div className="risk-card" style={{ "--risk-color": r.color }} key={r.title}>
            <div className="risk-title">{r.title}</div>
            <div className="risk-body">{r.body}</div>
            {r.diagram}
            <span className="risk-threat">AP2 {r.threat}</span>
          </div>
        ))}
      </div>

      <div className="card-title">What's happening right now</div>
      <div className="session-grid">
        <div className="session-primary">
          <div className="session-count">{stats?.counts.total ?? "—"}</div>
          <div className="session-count-label">transactions processed this session</div>
          <div className="session-outcomes">
            <div className="session-outcome">
              <span className="session-outcome-dot" style={{ background: "var(--pass)" }} />
              <span className="session-outcome-value">{stats?.counts.allowed ?? "—"}</span>
              <span className="session-outcome-label">Allowed</span>
            </div>
            <div className="session-outcome">
              <span className="session-outcome-dot" style={{ background: "var(--fail)" }} />
              <span className="session-outcome-value">{stats?.counts.blocked ?? "—"}</span>
              <span className="session-outcome-label">Blocked</span>
            </div>
            <div className="session-outcome">
              <span className="session-outcome-dot" style={{ background: "var(--warn)" }} />
              <span className="session-outcome-value">{stats?.counts.requires_reconfirmation ?? "—"}</span>
              <span className="session-outcome-label">Reconfirmation</span>
            </div>
          </div>
        </div>
        <div className="audit-widget">
          <div className={"audit-widget-status " + (stats?.chain.intact ? "audit-widget-ok" : "audit-widget-bad")}>
            {stats ? (stats.chain.intact ? "✓ Intact" : "✕ Broken") : "Audit Chain"}
          </div>
          <div className="audit-widget-note">
            {stats ? `${stats.chain.total_events} events verified` : "waiting for activity"}
          </div>
        </div>
      </div>
      <div className="session-note">
        This is live session data from this running instance, not production traffic — every
        number reflects transactions run on this machine. Reset any time from the Scenarios tab.
      </div>

      <div style={{ marginTop: 30 }}>
        <button className="btn-small" onClick={onNavigateScenarios}>Run a scenario →</button>
      </div>
    </div>
  );
}
