import { motion } from "motion/react";
import { Fragment, useEffect, useRef, useState } from "react";
import { streamAudit, verifyChain } from "../api.js";

export default function AuditTrail() {
  const [events, setEvents] = useState([]);
  const [chainStatus, setChainStatus] = useState(null);
  const [verifying, setVerifying] = useState(false);
  const [expanded, setExpanded] = useState(null);
  const scrollRef = useRef(null);

  useEffect(() => {
    const stop = streamAudit((event) => {
      setEvents((prev) => [...prev.slice(-999), event]);
    });
    return stop;
  }, []);

  // The chain's status is a fact about the system right now, not
  // something that should require a click to learn — verify once on
  // load; the button re-runs it on demand afterwards.
  useEffect(() => {
    verifyChain().then(setChainStatus).catch(() => {});
  }, []);

  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [events]);

  async function handleVerify() {
    setVerifying(true);
    setChainStatus(null);
    const started = Date.now();
    const status = await verifyChain();
    const elapsed = Date.now() - started;
    setTimeout(() => {
      setChainStatus(status);
      setVerifying(false);
    }, Math.max(0, 500 - elapsed)); // let the "verifying" state be visible even when instant
  }

  return (
    <div className="page">
      <div className="page-header">
        <div className="page-title">Audit Trail</div>
        <div className="page-subtitle">
          Every integrity decision is written by Interlock itself into a hash-chained,
          receiver-attested ledger — the agent's own logs are never the source of truth.
        </div>
      </div>

      <div className="card" style={{ marginBottom: 16 }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div className="card-title" style={{ marginBottom: 0 }}>Chain integrity</div>
          <button className="btn-small" onClick={handleVerify} disabled={verifying}>
            {verifying ? "Verifying…" : "Verify chain integrity"}
          </button>
        </div>
        {chainStatus && (
          <div className={"audit-summary " + (chainStatus.intact ? "audit-summary-ok" : "audit-summary-bad")} style={{ marginTop: 12, marginBottom: 0 }}>
            <div>
              <div className="audit-summary-text">
                {chainStatus.intact ? `✓ ${chainStatus.total_events} events verified` : "✗ CHAIN TAMPERED"}
              </div>
              <div className="small muted" style={{ marginTop: 2 }}>
                {chainStatus.intact ? "Chain integrity confirmed — no gaps, no rewritten history." : `${chainStatus.breaks.length} break(s) found`}
              </div>
            </div>
            <span className="mono tiny muted">head {chainStatus.head_hash.slice(0, 16)}…</span>
          </div>
        )}
      </div>

      <div className="card">
        <div className="card-title">Live event feed ({events.length})</div>
        <div className="log-scroll" style={{ maxHeight: 560 }} ref={scrollRef}>
          <table className="audit-table">
            <thead>
              <tr><th>#</th><th>Transaction</th><th>Event</th><th>State</th><th></th></tr>
            </thead>
            <tbody>
              {events.map((e) => (
                <Fragment key={e.seq}>
                  <motion.tr
                    initial={{ opacity: 0 }}
                    animate={{ opacity: 1 }}
                    transition={{ duration: 0.25 }}
                    className="audit-row-toggle"
                    tabIndex={0}
                    role="button"
                    aria-expanded={expanded === e.seq}
                    onClick={() => setExpanded(expanded === e.seq ? null : e.seq)}
                    onKeyDown={(ev) => {
                      if (ev.key === "Enter" || ev.key === " ") {
                        ev.preventDefault();
                        setExpanded(expanded === e.seq ? null : e.seq);
                      }
                    }}
                  >
                    <td className="mono tiny">{e.seq}</td>
                    <td className="mono tiny">{e.transaction_id}</td>
                    <td className="small">{e.event_type}</td>
                    <td className="small">{e.new_state || "—"}</td>
                    <td className="tiny muted">{expanded === e.seq ? "▲" : "▼"}</td>
                  </motion.tr>
                  {expanded === e.seq && (
                    <tr className="audit-expand">
                      <td colSpan={5}>
                        prev_hash: {e.prev_hash}<br />
                        hash: {e.hash}<br />
                        timestamp: {e.timestamp}
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
              {events.length === 0 && (
                <tr><td colSpan={5} className="muted small" style={{ padding: "20px 8px" }}>No events yet — run a scenario.</td></tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
