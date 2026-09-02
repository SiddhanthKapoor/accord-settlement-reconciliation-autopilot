import { motion } from "motion/react";
import { humanizeDecision } from "../decisionCopy.js";

export default function DecisionBanner({ decision }) {
  if (!decision) return null;
  const { statusLine, headline, tone } = humanizeDecision(decision);
  const cls = `decision-banner decision-${tone === "allow" ? "allow" : tone === "block" ? "block" : "warn"}`;

  return (
    <motion.div
      className={cls}
      role="status"
      initial={{ opacity: 0, scale: 0.98 }}
      animate={{ opacity: 1, scale: 1 }}
      transition={{ duration: 0.22, ease: "easeOut" }}
    >
      <div>
        <div className="decision-status-line">{statusLine}</div>
        <div className="decision-title">{headline}</div>
      </div>
    </motion.div>
  );
}
