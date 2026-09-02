import { useEffect, useState } from "react";
import { getStats } from "../api.js";

const PIPELINE = [
  { title: "Agent", sub: "decides to spend" },
  { title: "Commerce State", sub: "cart, catalog, mandate" },
  { title: "Interlock", sub: "settlement-time gate", em: true },
  { title: "Verification", sub: "deterministic + semantic" },
  { title: "Decision", sub: "allow / block / reconfirm" },
  { title: "Razorpay", sub: "execution" },
];

const RISKS = [
  {
    icon: "R", cls: "risk-icon-replay", title: "Replay",
    body: "A commitment that already backed a completed payment is presented again — with a new session, a new token, or a re-signed artifact.",
    threat: "T-31",
  },
  {
    icon: "M", cls: "risk-icon-mutation", title: "State Mutation",
    body: "The transaction that reaches the payment layer no longer matches what was verified — a different quantity, price, product, or merchant.",
    threat: "T-32",
  },
  {
    icon: "S", cls: "risk-icon-race", title: "Shared Budget Race",
    body: "Multiple agent sessions attempt to spend against the same delegated budget at the same instant. Only one may legitimately win.",
    threat: "T-33",
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
          Interlock sits at the settlement boundary between an agent-initiated transaction and
          Razorpay execution. It independently re-verifies replay status, shared-budget
          concurrency, and drift against merchant ground truth — the three things a signed
          mandate proves were <em>authorized</em>, not that they're still <em>true</em>.
        </p>

        <div className="pipeline">
          {PIPELINE.map((p, i) => (
            <div key={p.title} style={{ display: "flex", alignItems: "center" }}>
              <div className={"pipe-node" + (p.em ? " pipe-node-em" : "")}>
                <div className="pipe-node-title">{p.title}</div>
                <div className="pipe-node-sub">{p.sub}</div>
              </div>
              {i < PIPELINE.length - 1 && <div className="pipe-arrow">→</div>}
            </div>
          ))}
        </div>
      </section>

      <div className="card-title" style={{ marginTop: 8 }}>What it protects against</div>
      <div className="risk-grid">
        {RISKS.map((r) => (
          <div className="card risk-card" key={r.title}>
            <div className={"risk-icon " + r.cls}>{r.icon}</div>
            <div className="risk-title">{r.title}</div>
            <div className="risk-body">{r.body}</div>
            <span className="risk-threat">AP2 {r.threat}</span>
          </div>
        ))}
      </div>

      <div className="card-title">What's happening right now</div>
      <div className="stats-grid">
        <div className="card stat-card">
          <div className="stat-label">Transactions</div>
          <div className="stat-value">{stats?.counts.total ?? "—"}</div>
        </div>
        <div className="card stat-card">
          <div className="stat-label">Allowed</div>
          <div className="stat-value stat-value-pass">{stats?.counts.allowed ?? "—"}</div>
        </div>
        <div className="card stat-card">
          <div className="stat-label">Blocked</div>
          <div className="stat-value stat-value-fail">{stats?.counts.blocked ?? "—"}</div>
        </div>
        <div className="card stat-card">
          <div className="stat-label">Reconfirmation</div>
          <div className="stat-value">{stats?.counts.requires_reconfirmation ?? "—"}</div>
        </div>
        <div className="card stat-card">
          <div className="stat-label">Audit Chain</div>
          <div className={"stat-value " + (stats?.chain.intact ? "stat-value-pass" : "stat-value-fail")}>
            {stats ? (stats.chain.intact ? "Intact" : "Broken") : "—"}
          </div>
          <div className="stat-note">{stats ? `${stats.chain.total_events} events` : ""}</div>
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
