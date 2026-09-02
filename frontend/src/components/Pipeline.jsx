// One component, two modes:
//  - idle (no `live` prop): the calm explainer strip on Overview.
//  - live (`live` = array of 6 {status, label}, from deriveNodes below):
//    driven entirely by real backend responses in ScenariosView. No
//    node ever advances except in reaction to an actual API call
//    resolving — see deriveNodes() for exactly which real event moves
//    each node.

const NODE_DEFS = [
  { title: "Agent", idle: "decides to spend" },
  { title: "Commerce State", idle: "cart, catalog, mandate" },
  { title: "Interlock", idle: "ready", em: true },
  { title: "Verification", idle: "deterministic + semantic" },
  { title: "Decision", idle: "allow / block / reconfirm" },
  { title: "Razorpay", idle: "execution" },
];

export function deriveNodes({ phase, executing, result }) {
  if (phase === "idle" || !phase) return null;

  const nodes = NODE_DEFS.map(() => ({ status: "idle", label: "" }));

  if (phase === "running" && !executing) {
    nodes[0] = { status: "done", label: "✓" };
    nodes[1] = { status: "done", label: "✓" };
    nodes[2] = { status: "done", label: "✓" };
    nodes[3] = { status: "active", label: "VERIFYING" };
    return nodes;
  }

  if (phase === "running" && executing) {
    // The only path that reaches this state already returned ALLOW from
    // the verify() call — see scenarios.js: the "handing off to Razorpay"
    // / "settling the transaction" log lines only fire inside the ALLOW
    // branch. So this is real, already-known information, just revealed
    // slightly ahead of the outer promise resolving.
    for (let i = 0; i < 4; i++) nodes[i] = { status: "done", label: "✓" };
    nodes[4] = { status: "done", label: "ALLOW" };
    nodes[5] = { status: "active", label: "EXECUTING" };
    return nodes;
  }

  // phase === "decided": authoritative, derived from the real Decision.
  if (result?.decision) {
    for (let i = 0; i < 3; i++) nodes[i] = { status: "done", label: "✓" };
    const outcome = result.decision.outcome;
    if (outcome === "ALLOW") {
      nodes[3] = { status: "done", label: "✓" };
      nodes[4] = { status: "done", label: "ALLOW" };
      nodes[5] = result.execution
        ? { status: "done", label: result.execution.simulated ? "SIMULATED" : "EXECUTED" }
        : { status: "idle", label: "WAITING" };
    } else if (outcome === "BLOCK") {
      const failing = result.decision.checks.find((c) => c.status === "FAIL");
      const threatLabels = { "T-31": "REPLAY DETECTED", "T-32": "DRIFT DETECTED", "T-33": "BUDGET UNAVAILABLE" };
      nodes[3] = { status: "blocked", label: "✕ " + (threatLabels[failing?.threat_ref] || "VIOLATION DETECTED") };
      nodes[4] = { status: "blocked", label: "BLOCK" };
      nodes[5] = { status: "idle", label: "NOT REACHED" };
    } else {
      nodes[3] = { status: "warn", label: "NEEDS REVIEW" };
      nodes[4] = { status: "warn", label: "RECONFIRM" };
      nodes[5] = { status: "idle", label: "NOT REACHED" };
    }
  }
  return nodes;
}

export default function Pipeline({ live }) {
  return (
    <div className={"pipeline" + (live ? " pipeline-live" : "")}>
      {NODE_DEFS.map((def, i) => {
        const node = live?.[i];
        const status = node?.status || "idle";
        const label = node?.label;
        return (
          <div key={def.title} style={{ display: "flex", alignItems: "center" }}>
            <div className={`pipe-node pipe-node-${status}` + (def.em && !live ? " pipe-node-em" : "")}>
              {def.em && !live && <span className="pipe-ready-dot" aria-hidden="true" />}
              <div className="pipe-node-title">{def.title}</div>
              <div className={"pipe-node-sub" + (status !== "idle" ? " pipe-node-sub-live" : "")}>
                {label || def.idle}
              </div>
            </div>
            {i < NODE_DEFS.length - 1 && (
              <div className={"pipe-arrow" + (status === "done" ? " pipe-arrow-done" : "")}>→</div>
            )}
          </div>
        );
      })}
    </div>
  );
}
