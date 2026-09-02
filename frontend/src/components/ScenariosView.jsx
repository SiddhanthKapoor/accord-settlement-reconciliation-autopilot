import { useEffect, useState } from "react";
import { adminReset, catalogReset, getStats } from "../api.js";
import { HERO_SCENARIOS, SECONDARY_SCENARIOS, runSharedBudgetRace } from "../scenarios.js";
import ActivityLog from "./ActivityLog.jsx";
import ChecksPanel from "./ChecksPanel.jsx";
import ExecutionHandoff from "./ExecutionHandoff.jsx";
import MutationCompare from "./MutationCompare.jsx";
import Pipeline, { deriveNodes } from "./Pipeline.jsx";
import RaceVisualization from "./RaceVisualization.jsx";

export default function ScenariosView() {
  const [entries, setEntries] = useState([]);
  const [selected, setSelected] = useState(null);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState(null);
  const [raceAgents, setRaceAgents] = useState([]);
  const [raceDone, setRaceDone] = useState(false);
  const [raceWinners, setRaceWinners] = useState(0);
  const [resetting, setResetting] = useState(false);
  const [phase, setPhase] = useState("idle"); // idle -> running -> decided
  const [executing, setExecuting] = useState(false);
  const [auditCount, setAuditCount] = useState(null);

  useEffect(() => {
    let alive = true;
    const load = () => getStats().then((s) => alive && setAuditCount(s.chain.total_events)).catch(() => {});
    load();
    const id = setInterval(load, 2000);
    return () => { alive = false; clearInterval(id); };
  }, []);

  function log(text, warn = false) {
    const t = new Date().toLocaleTimeString();
    setEntries((prev) => [...prev, { t, text, warn }]);
    if (text.includes("reaches Interlock")) setPhase("running");
    if (text.includes("handing off to Razorpay") || text.includes("settling the transaction")) setExecuting(true);
  }

  async function run(key, def) {
    setSelected(key);
    setRunning(true);
    setResult(null);
    setRaceAgents([]);
    setRaceDone(false);
    setEntries([]);
    setPhase("idle");
    setExecuting(false);
    try {
      if (def.kind === "race") {
        setRaceAgents(Array(8).fill("idle"));
        const { winners } = await runSharedBudgetRace(
          (i, status) => setRaceAgents((prev) => { const next = [...prev]; next[i] = status; return next; }),
          log
        );
        setRaceWinners(winners);
        setRaceDone(true);
      } else {
        const r = await def.run(log);
        setResult(r);
        setPhase("decided");
        setExecuting(false);
      }
    } catch (err) {
      log(`ERROR: ${err.message}`, true);
      setPhase("idle");
    } finally {
      setRunning(false);
    }
  }

  async function handleReset() {
    setResetting(true);
    try {
      await adminReset();
      await catalogReset();
      setEntries([]);
      setResult(null);
      setSelected(null);
      setRaceAgents([]);
      setRaceDone(false);
      setPhase("idle");
    } finally {
      setResetting(false);
    }
  }

  const activeHero = selected && HERO_SCENARIOS[selected] ? selected : null;
  const activeSecondary = selected && SECONDARY_SCENARIOS[selected] ? selected : null;
  const activeDef = activeHero ? HERO_SCENARIOS[selected] : activeSecondary ? SECONDARY_SCENARIOS[selected] : null;
  const pipelineNodes = activeDef?.kind !== "race" ? deriveNodes({ phase, executing, result }) : null;

  return (
    <div className="page">
      <div className="page-header" style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-end" }}>
        <div>
          <div className="page-title">Scenarios</div>
          <div className="page-subtitle">Every run drives the real backend end to end. What you see is what the API returned.</div>
        </div>
        <button className="run-again-btn" style={{ width: "auto" }} onClick={handleReset} disabled={resetting}>
          {resetting ? "Resetting…" : "Reset session"}
        </button>
      </div>

      <div className="hero-scenario-grid">
        {Object.entries(HERO_SCENARIOS).map(([key, s], i) => (
          <button key={key} className={"hero-scenario-card" + (selected === key ? " active" : "")} onClick={() => run(key, s)} disabled={running}>
            <div className="hero-scenario-num">SCENARIO {i + 1}</div>
            <div className="hero-scenario-title">{s.label}</div>
            <div className="hero-scenario-desc">{s.description}</div>
          </button>
        ))}
      </div>

      <div className="scenario-layout">
        <div>
          <div className="card" style={{ marginBottom: 14 }}>
            <div className="card-title">More scenarios</div>
            <div className="secondary-list">
              {Object.entries(SECONDARY_SCENARIOS).map(([key, s]) => (
                <button key={key} className={"secondary-item" + (selected === key ? " active" : "")} onClick={() => run(key, s)} disabled={running}>
                  <span className="secondary-item-title">{s.label}</span>
                  <span className="secondary-item-arrow">→</span>
                </button>
              ))}
            </div>
            {activeDef && (
              <button className="run-again-btn" onClick={() => run(selected, activeDef)} disabled={running}>
                {running ? "Running…" : "Run again"}
              </button>
            )}
          </div>
          <div className="card" style={{ marginBottom: 14 }}>
            <ActivityLog entries={entries} />
          </div>
          {auditCount !== null && (
            <div className="card" style={{ padding: "12px 16px" }}>
              <span className="tiny muted">Audit ledger: {auditCount} events recorded this session</span>
            </div>
          )}
        </div>

        <div>
          {!selected && (
            <div className="card result-empty">Pick a scenario to see Interlock verify it live.</div>
          )}

          {selected && activeDef?.kind === "race" && (
            <RaceVisualization agents={raceAgents} winners={raceWinners} total={raceAgents.length} done={raceDone} />
          )}

          {selected && pipelineNodes && (
            <div className={"pipeline-wrap pipeline-wrap-live"} style={{ marginBottom: 16 }}>
              <Pipeline live={pipelineNodes} />
            </div>
          )}

          {selected && result?.compare && (
            <>
              <MutationCompare verified={result.compare.verified} observed={result.compare.observed} />
              <ChecksPanel decision={result.decision} />
            </>
          )}

          {selected && result && !result.compare && (
            <>
              <ChecksPanel decision={result.decision} />
              <ExecutionHandoff execution={result.execution} />
            </>
          )}
        </div>
      </div>
    </div>
  );
}
