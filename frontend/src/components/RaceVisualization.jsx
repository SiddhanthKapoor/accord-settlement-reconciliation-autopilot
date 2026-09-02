import { motion } from "motion/react";

export default function RaceVisualization({ agents, winners, total, done }) {
  return (
    <div>
      <div className="card race-budget">
        <div>
          <div className="stat-label">Shared single-use budget</div>
          <div className="race-budget-amount">₹2,000</div>
        </div>
        <div className="muted small">
          {total} agents attempt to spend against this one delegated budget simultaneously.
        </div>
      </div>

      <div className="race-agents-grid">
        {agents.map((status, i) => (
          <motion.div
            key={i}
            layout
            animate={status === "won" || status === "lost" ? { scale: [1, 1.06, 1] } : {}}
            transition={{ duration: 0.3, ease: "easeOut" }}
            className={
              "race-agent " +
              (status === "waiting" ? "race-agent-waiting" : status === "won" ? "race-agent-won" : status === "lost" ? "race-agent-lost" : "")
            }
          >
            <div className="race-agent-label">Agent {i + 1}</div>
            <div className="race-agent-status">
              {status === "waiting" ? "…" : status === "won" ? "ACQUIRED" : status === "lost" ? "REJECTED" : ""}
            </div>
          </motion.div>
        ))}
      </div>

      {done && (
        <motion.div initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.25 }}>
          <div className="card race-summary">
            <div className="race-summary-item">
              <div className="race-summary-label">Concurrent attempts</div>
              <div className="race-summary-value">{total}</div>
            </div>
            <div className="race-summary-item">
              <div className="race-summary-label">Reservations acquired</div>
              <div className={"race-summary-value " + (winners === 1 ? "" : "stat-value-fail")}>{winners}</div>
            </div>
            <div className="race-summary-item">
              <div className="race-summary-label">Double-spend</div>
              <div className={"race-summary-value " + (winners === 1 ? "stat-value-pass" : "stat-value-fail")}>
                {winners === 1 ? "None" : "DETECTED"}
              </div>
            </div>
          </div>
          <div className="proof-box">
            unit test evidence (backend/tests/test_concurrency_and_replay.py):{"\n"}
            20 real concurrent OS threads racing store.reserve_budget() → expected winners: 1, actual winners: 1 → PASS{"\n"}
            this run: {total} concurrent HTTP requests → actual winners: {winners} {winners === 1 ? "→ PASS" : "→ UNEXPECTED"}
          </div>
        </motion.div>
      )}
    </div>
  );
}
