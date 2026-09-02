export default function ActivityLog({ entries }) {
  return (
    <div>
      <div className="card-title">Agent / commerce activity</div>
      <div className="log-scroll">
        {entries.length === 0 && <p className="muted small">Pick a scenario to start.</p>}
        {entries.map((e, i) => (
          <div className="log-line" key={i}>
            <span className="log-time">{e.t}</span>
            <span className={e.warn ? "log-warn" : ""}>{e.text}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
