import { useEffect, useRef, useState } from "react";
import { streamAudit, verifyChain } from "../api.js";

export default function AuditTrail() {
  const [events, setEvents] = useState([]);
  const [chainStatus, setChainStatus] = useState(null);
  const scrollRef = useRef(null);

  useEffect(() => {
    const stop = streamAudit((event) => {
      setEvents((prev) => [...prev.slice(-499), event]);
    });
    return stop;
  }, []);

  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [events]);

  async function handleVerify() {
    const status = await verifyChain();
    setChainStatus(status);
  }

  return (
    <div className="panel audit-panel">
      <div className="audit-header">
        <h3>Audit ledger (hash-chained, live)</h3>
        <button className="btn-small" onClick={handleVerify}>Verify chain integrity</button>
      </div>
      {chainStatus && (
        <div className={chainStatus.intact ? "chain-status chain-ok" : "chain-status chain-broken"}>
          {chainStatus.intact ? "✓ intact" : "✗ TAMPERED"} — {chainStatus.total_events} events, head {chainStatus.head_hash.slice(0, 12)}…
        </div>
      )}
      <div className="log-scroll audit-scroll" ref={scrollRef}>
        <table className="audit-table">
          <thead>
            <tr><th>#</th><th>tx</th><th>event</th><th>state</th><th>hash</th></tr>
          </thead>
          <tbody>
            {events.map((e) => (
              <tr key={e.seq}>
                <td>{e.seq}</td>
                <td className="mono small">{e.transaction_id}</td>
                <td className="small">{e.event_type}</td>
                <td className="small">{e.new_state || "—"}</td>
                <td className="mono small">{e.hash?.slice(0, 10)}…</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
