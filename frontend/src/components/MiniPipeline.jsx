const STAGES = ["Agent", "Commerce State", "Interlock", "Verification", "Decision", "Razorpay"];

export default function MiniPipeline({ stage }) {
  return (
    <div className="pipeline" style={{ margin: "0 0 16px" }}>
      {STAGES.map((s, i) => (
        <div key={s} style={{ display: "flex", alignItems: "center" }}>
          <div
            className={"pipe-node" + (i === stage ? " pipe-node-em" : "")}
            style={{ minWidth: 100, padding: "8px 12px", opacity: i > stage ? 0.45 : 1 }}
          >
            <div className="pipe-node-title" style={{ fontSize: 11 }}>{s}</div>
          </div>
          {i < STAGES.length - 1 && <div className="pipe-arrow" style={{ fontSize: 13, padding: "0 6px" }}>→</div>}
        </div>
      ))}
    </div>
  );
}
