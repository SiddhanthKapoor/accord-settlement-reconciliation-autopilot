import { useState } from "react";
import ActivityLog from "./components/ActivityLog.jsx";
import AuditTrail from "./components/AuditTrail.jsx";
import ChecksPanel from "./components/ChecksPanel.jsx";
import LifecycleStepper from "./components/LifecycleStepper.jsx";
import { SCENARIOS, runSharedBudgetRace } from "./scenarios.js";

export default function App() {
  const [entries, setEntries] = useState([]);
  const [decision, setDecision] = useState(null);
  const [stage, setStage] = useState("DECLARED");
  const [running, setRunning] = useState(null);
  const [raceResult, setRaceResult] = useState(null);

  function log(text, warn = false) {
    const t = new Date().toLocaleTimeString();
    setEntries((prev) => [...prev, { t, text, warn }]);
  }

  async function runScenario(key) {
    setRunning(key);
    setDecision(null);
    setRaceResult(null);
    setStage("DECLARED");
    setEntries([]);
    try {
      setStage("SELECTED");
      const result = await SCENARIOS[key].run((text) => {
        log(text, text.startsWith("!!"));
        if (text.includes("commits to cart")) setStage("CHECKOUT_READY");
        if (text.includes("reaches Interlock")) setStage("PAYMENT_REQUESTED");
      });
      setDecision(result.decision);
      const newStage = { ALLOW: "ALLOWED", BLOCK: "BLOCKED", REQUIRE_RECONFIRMATION: "REQUIRES_RECONFIRMATION" }[
        result.decision.outcome
      ];
      setStage(newStage);
    } catch (err) {
      log(`ERROR: ${err.message}`, true);
    } finally {
      setRunning(null);
    }
  }

  async function runRace() {
    setRunning("race");
    setEntries([]);
    setDecision(null);
    setRaceResult(null);
    setStage("CHECKOUT_READY");
    try {
      const result = await runSharedBudgetRace((text) => log(text));
      setRaceResult(result);
    } catch (err) {
      log(`ERROR: ${err.message}`, true);
    } finally {
      setRunning(null);
    }
  }

  return (
    <div className="app">
      <header className="app-header">
        <div>
          <span className="brand">Interlock</span>
          <span className="tagline">The mandate was signed. The transaction was still a lie.</span>
        </div>
      </header>

      <div className="layout">
        <aside className="col col-left">
          <div className="panel">
            <h3>Demo scenarios</h3>
            {Object.entries(SCENARIOS).map(([key, s]) => (
              <button
                key={key}
                className={"scenario-btn" + (running === key ? " scenario-btn-active" : "")}
                onClick={() => runScenario(key)}
                disabled={!!running}
              >
                <div className="scenario-label">{s.label}</div>
                <div className="scenario-desc">{s.description}</div>
              </button>
            ))}
            <button
              className={"scenario-btn" + (running === "race" ? " scenario-btn-active" : "")}
              onClick={runRace}
              disabled={!!running}
            >
              <div className="scenario-label">Shared-budget race (T-33)</div>
              <div className="scenario-desc">8 concurrent commits race one single-use budget. Exactly one should win.</div>
            </button>
            {raceResult && (
              <div className={raceResult.winners === 1 ? "chain-status chain-ok" : "chain-status chain-broken"}>
                {raceResult.winners}/{raceResult.attempts.length} attempts won the budget (expected 1)
              </div>
            )}
          </div>
          <ActivityLog entries={entries} />
        </aside>

        <main className="col col-center">
          <div className="panel">
            <h3>Transaction lifecycle</h3>
            <LifecycleStepper stage={stage} outcome={decision?.outcome} />
          </div>
          <ChecksPanel decision={decision} />
        </main>

        <aside className="col col-right">
          <AuditTrail />
        </aside>
      </div>
    </div>
  );
}
