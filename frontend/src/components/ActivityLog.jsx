export default function ActivityLog({ entries }) {
  return (
    <div className="panel activity-log">
      <h3>Agent / commerce activity</h3>
      <div className="log-scroll">
        {entries.length === 0 && <p className="muted">Pick a scenario on the left to start.</p>}
        {entries.map((e, i) => (
          <div className="log-line" key={i}>
            <span className="log-time">{e.t}</span>
            <span className={e.warn ? "log-text log-warn" : "log-text"}>{e.text}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
