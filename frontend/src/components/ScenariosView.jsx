import { useState } from "react";
import { adminReset, catalogReset } from "../api.js";
import { HERO_SCENARIOS, SECONDARY_SCENARIOS, runSharedBudgetRace } from "../scenarios.js";
import ActivityLog from "./ActivityLog.jsx";
import ChecksPanel from "./ChecksPanel.jsx";
import ExecutionHandoff from "./ExecutionHandoff.jsx";
import MiniPipeline from "./MiniPipeline.jsx";
import MutationCompare from "./MutationCompare.jsx";
import RaceVisualization from "./RaceVisualization.jsx";

function stageFromLog(text) {
  if (text.includes("declaring intent")) return 0;
  if (text.includes("discovers product") || text.includes("commits to cart")) return 1;
  if (text.includes("reaches Interlock")) return 2;
  if (text.includes("decision:")) return 4;
  if (text.includes("handing off to Razorpay") || text.includes("execution simulated") || text.includes("payment link created") || text.includes("settling the transaction")) return 5;
  return null;
}

export default function ScenariosView() {
  const [entries, setEntries] = useState([]);
  const [selected, setSelected] = useState(null);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState(null);
  const [raceAgents, setRaceAgents] = useState([]);
  const [raceDone, setRaceDone] = useState(false);
  const [raceWinners, setRaceWinners] = useState(0);
  const [resetting, setResetting] = useState(false);
  const [stage, setStage] = useState(null);

  function log(text, warn = false) {
    const t = new Date().toLocaleTimeString();
    setEntries((prev) => [...prev, { t, text, warn }]);
    const s = stageFromLog(text);
    if (s !== null) setStage(s);
    // integrity checks run right after the "reaches Interlock" line
    if (text.includes("running integrity checks")) setStage(3);
  }

  async function run(key, def) {
    setSelected(key);
    setRunning(true);
    setResult(null);
    setRaceAgents([]);
    setRaceDone(false);
    setEntries([]);
    setStage(null);
    try {
      if (def.kind === "race") {
        setRaceAgents(Array(8).fill("idle"));
        const { winners, n } = await runSharedBudgetRace(
          (i, status) => setRaceAgents((prev) => { const next = [...prev]; next[i] = status; return next; }),
          log
        );
        setRaceWinners(winners);
        setRaceDone(true);
      } else {
        const r = await def.run(log);
        setResult(r);
      }
    } catch (err) {
      log(`ERROR: ${err.message}`, true);
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
    } finally {
      setResetting(false);
    }
  }

  const activeHero = selected && HERO_SCENARIOS[selected] ? selected : null;
  const activeSecondary = selected && SECONDARY_SCENARIOS[selected] ? selected : null;
  const activeDef = activeHero ? HERO_SCENARIOS[selected] : activeSecondary ? SECONDARY_SCENARIOS[selected] : null;

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
          <div className="card">
            <ActivityLog entries={entries} />
          </div>
        </div>

        <div>
          {!selected && (
            <div className="card result-empty">Pick a scenario to see Interlock verify it live.</div>
          )}

          {selected && activeDef?.kind === "race" && (
            <RaceVisualization agents={raceAgents} winners={raceWinners} total={raceAgents.length} done={raceDone} />
          )}

          {selected && activeDef?.kind !== "race" && stage !== null && <MiniPipeline stage={stage} />}

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
