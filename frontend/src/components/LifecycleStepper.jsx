const STAGES = [
  "DECLARED",
  "SELECTED",
  "CHECKOUT_READY",
  "PAYMENT_REQUESTED",
  "DECIDED",
  "EXECUTED",
];

function stageIndex(stage) {
  if (["ALLOWED", "BLOCKED", "REQUIRES_RECONFIRMATION"].includes(stage)) return 4;
  const i = STAGES.indexOf(stage);
  return i === -1 ? -1 : i;
}

export default function LifecycleStepper({ stage, outcome }) {
  const activeIndex = stageIndex(stage);
  return (
    <div className="stepper">
      {STAGES.map((s, i) => {
        const isActive = i === activeIndex;
        const isDone = i < activeIndex || (i === activeIndex && s !== "DECIDED");
        let label = s.replace(/_/g, " ");
        let cls = "step";
        if (i === 4 && isActive) {
          label = outcome ? outcome.replace(/_/g, " ") : "DECIDED";
          cls += outcome === "ALLOW" ? " step-allow" : outcome === "BLOCK" ? " step-block" : " step-warn";
        } else if (isActive) {
          cls += " step-active";
        } else if (i < activeIndex) {
          cls += " step-done";
        }
        return (
          <div className={cls} key={s}>
            <div className="step-dot" />
            <div className="step-label">{label}</div>
            {i < STAGES.length - 1 && <div className="step-line" />}
          </div>
        );
      })}
    </div>
  );
}
