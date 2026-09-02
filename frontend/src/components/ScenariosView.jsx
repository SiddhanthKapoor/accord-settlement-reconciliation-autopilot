import { AnimatePresence, motion } from "motion/react";
import { useEffect, useRef, useState } from "react";
import { adminReset, catalogReset, getStats, streamAudit } from "../api.js";
import { HERO_SCENARIOS, SECONDARY_SCENARIOS, runSharedBudgetRace } from "../scenarios.js";
import ActivityLog from "./ActivityLog.jsx";
import ChecksPanel from "./ChecksPanel.jsx";
import DecisionBanner from "./DecisionBanner.jsx";
import ExecutionHandoff from "./ExecutionHandoff.jsx";
import LiveChecklist from "./LiveChecklist.jsx";
import MutationCompare from "./MutationCompare.jsx";
import Pipeline, { deriveNodes } from "./Pipeline.jsx";
import RaceVisualization from "./RaceVisualization.jsx";
import ScenarioPreview from "./ScenarioPreview.jsx";

// txnState: idle -> preview -> verifying -> [executing] -> decided
// This is the single source of truth for what the UI shows. Nothing here
// is a fixed-duration animation — every transition is triggered by a real
// event: a click, a log line from an in-flight request, or a promise
// resolving.

export default function ScenariosView() {
  const [entries, setEntries] = useState([]);
  const [selected, setSelected] = useState(null);
  const [txnState, setTxnState] = useState("idle");
  const [result, setResult] = useState(null);
  const [liveChecks, setLiveChecks] = useState([]);
  const [raceAgents, setRaceAgents] = useState([]);
  const [raceDone, setRaceDone] = useState(false);
  const [raceWinners, setRaceWinners] = useState(0);
  const [resetting, setResetting] = useState(false);
  const [auditCount, setAuditCount] = useState(null);

  const currentTxnIdRef = useRef(null);
  const stageRef = useRef(null);

  // Live audit stream — the SAME event source the Audit Trail page reads.
  // Individual CHECK_EXECUTED events genuinely land here while a /verify
  // request is still in flight server-side (it runs in a worker thread;
  // this SSE loop runs concurrently and picks up each check's commit as
  // it happens). This is not a simulated progress bar.
  useEffect(() => {
    const stop = streamAudit((event) => {
      if (event.transaction_id !== currentTxnIdRef.current) return;
      if (event.event_type === "CHECK_EXECUTED") {
        setLiveChecks((prev) => [...prev, event.payload]);
      }
    });
    return stop;
  }, []);

  useEffect(() => {
    let alive = true;
    const load = () => getStats().then((s) => alive && setAuditCount(s.chain.total_events)).catch(() => {});
    load();
    const id = setInterval(load, 2000);
    return () => { alive = false; clearInterval(id); };
  }, []);

  // If a scenario is selected but its result area isn't in view, bring it
  // into view once — never re-scroll on every state change, only on the
  // deliberate act of selecting something new.
  useEffect(() => {
    if (!selected || !stageRef.current) return;
    const rect = stageRef.current.getBoundingClientRect();
    const inView = rect.top >= 0 && rect.bottom <= window.innerHeight;
    if (!inView) stageRef.current.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [selected]);

  function log(text, warn = false, data) {
    const t = new Date().toLocaleTimeString();
    setEntries((prev) => [...prev, { t, text, warn }]);
    if (data?.commitmentId) currentTxnIdRef.current = data.commitmentId;
    if (data?.phase === "executing") setTxnState("executing");
  }

  function selectScenario(key) {
    if (txnState === "verifying" || txnState === "executing") return; // don't interrupt a live run
    setSelected(key);
    setTxnState("preview");
    setResult(null);
    setEntries([]);
    setLiveChecks([]);
    setRaceAgents([]);
    setRaceDone(false);
    currentTxnIdRef.current = null;
  }

  async function runSelected() {
    const def = HERO_SCENARIOS[selected] || SECONDARY_SCENARIOS[selected];
    if (!def) return;
    setTxnState("verifying");
    setLiveChecks([]);
    setEntries([]);
    try {
      if (def.kind === "race") {
        setRaceAgents(Array(8).fill("idle"));
        const { winners } = await runSharedBudgetRace(
          (i, status) => setRaceAgents((prev) => { const next = [...prev]; next[i] = status; return next; }),
          log
        );
        setRaceWinners(winners);
        setRaceDone(true);
        setTxnState("decided");
      } else {
        const r = await def.run(log);
        setResult(r);
        setTxnState("decided");
      }
    } catch (err) {
      log(`ERROR: ${err.message}`, true);
      setTxnState("preview");
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
      setTxnState("idle");
      setLiveChecks([]);
      setRaceAgents([]);
      setRaceDone(false);
    } finally {
      setResetting(false);
    }
  }

  const activeDef = selected ? (HERO_SCENARIOS[selected] || SECONDARY_SCENARIOS[selected]) : null;
  const isRace = activeDef?.kind === "race";
  const isLive = txnState === "verifying" || txnState === "executing" || txnState === "decided";
  const busy = txnState === "verifying" || txnState === "executing";
  const pipelinePhase = txnState === "decided" ? "decided" : isLive ? "running" : "idle";
  const pipelineNodes = !isRace && isLive ? deriveNodes({ phase: pipelinePhase, executing: txnState === "executing", result }) : null;

  return (
    <div className="page">
      <div className="page-header" style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-end" }}>
        <div>
          <div className="page-title">Scenarios</div>
          <div className="page-subtitle">Select a scenario, then run it. Every run drives the real backend end to end.</div>
        </div>
        <button className="run-again-btn" style={{ width: "auto" }} onClick={handleReset} disabled={resetting}>
          {resetting ? "Resetting…" : "Reset session"}
        </button>
      </div>

      <div className="hero-scenario-grid">
        {Object.entries(HERO_SCENARIOS).map(([key, s], i) => (
          <button key={key} className={"hero-scenario-card" + (selected === key ? " active" : "")} onClick={() => selectScenario(key)} disabled={busy}>
            <div className="hero-scenario-num">SCENARIO {i + 1}</div>
            <div className="hero-scenario-title">{s.label}</div>
            <div className="hero-scenario-desc">{s.description}</div>
          </button>
        ))}
      </div>

      <div ref={stageRef} className="stage">
        <AnimatePresence mode="wait">
          {txnState === "idle" && (
            <motion.div key="empty" className="card result-empty" exit={{ opacity: 0 }}>
              Select a scenario above to see what Interlock will check.
            </motion.div>
          )}

          {txnState === "preview" && (
            <ScenarioPreview key="preview" label={activeDef.label} preview={activeDef.preview} onRun={runSelected} running={false} />
          )}

          {isLive && (
            <motion.div key="live" initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }} transition={{ duration: 0.2 }}>
              {isRace ? (
                <RaceVisualization agents={raceAgents} winners={raceWinners} total={raceAgents.length} done={raceDone} />
              ) : (
                <>
                  <div className="pipeline-wrap pipeline-wrap-live" style={{ marginBottom: 16 }}>
                    <Pipeline live={pipelineNodes} />
                  </div>
                  {txnState !== "decided" && <LiveChecklist checks={liveChecks} awaiting />}
                  <AnimatePresence>
                    {result && (
                      <motion.div key="result" initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
                        <DecisionBanner decision={result.decision} />
                        {result.compare && (
                          <MutationCompare verified={result.compare.verified} observed={result.compare.observed} />
                        )}
                        <ChecksPanel decision={result.decision} />
                        <ExecutionHandoff execution={result.execution} executionError={result.executionError} />
                      </motion.div>
                    )}
                  </AnimatePresence>
                </>
              )}
            </motion.div>
          )}
        </AnimatePresence>
      </div>

      <details className="disclosure" style={{ marginTop: 24 }}>
        <summary>More scenarios and raw activity trace →</summary>
        <div className="scenario-layout" style={{ marginTop: 14 }}>
          <div className="card">
            <div className="card-title">More scenarios</div>
            <div className="secondary-list">
              {Object.entries(SECONDARY_SCENARIOS).map(([key, s]) => (
                <button key={key} className={"secondary-item" + (selected === key ? " active" : "")} onClick={() => selectScenario(key)} disabled={busy}>
                  <span className="secondary-item-title">{s.label}</span>
                  <span className="secondary-item-arrow">→</span>
                </button>
              ))}
            </div>
          </div>
          <div className="card">
            <ActivityLog entries={entries} />
            {auditCount !== null && (
              <div className="tiny muted" style={{ marginTop: 10 }}>Audit ledger: {auditCount} events recorded this session</div>
            )}
          </div>
        </div>
      </details>
    </div>
  );
}
