import { motion } from "motion/react";

export default function ScenarioPreview({ label, preview, onRun, running }) {
  if (!preview) return null;
  return (
    <motion.div
      className="preview"
      initial={{ opacity: 0, y: 6 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.2 }}
    >
      <div className="preview-title">{label}</div>
      <dl className="preview-grid">
        <dt>Agent intends</dt>
        <dd>{preview.intent}</dd>
        <dt>What changes</dt>
        <dd>{preview.changes}</dd>
        <dt>Interlock verifies</dt>
        <dd>{preview.verifies}</dd>
        <dt>Expected outcome</dt>
        <dd>{preview.risk}</dd>
      </dl>
      <button className="run-btn" onClick={onRun} disabled={running}>
        {running ? "Verifying…" : "Run verification"}
      </button>
    </motion.div>
  );
}
