import { AnimatePresence, motion } from "motion/react";

// Renders checks as they genuinely arrive over the audit SSE stream —
// see ScenariosView's subscription. Each row here corresponds to a real
// CHECK_EXECUTED event written by the backend during the live /verify
// request; if a check takes longer for real reasons (e.g. the semantic
// classifier), the visible gap before the next row appears is real too.

const STATUS_SYMBOL = { PASS: "✓", WARN: "!", FAIL: "✕" };

export default function LiveChecklist({ checks, awaiting }) {
  // Defensive: never let one malformed event (a shape mismatch, a stray
  // keep-alive) blank the whole page — skip it, don't crash on it.
  const safeChecks = checks.filter((c) => c?.name && c?.status);
  if (safeChecks.length === 0 && !awaiting) return null;
  return (
    <div className="live-checklist">
      <AnimatePresence initial={false}>
        {safeChecks.map((c) => (
          <motion.div
            key={c.name}
            className={`live-check live-check-${c.status.toLowerCase()}`}
            initial={{ opacity: 0, x: -8 }}
            animate={{ opacity: 1, x: 0 }}
            transition={{ duration: 0.18 }}
          >
            <span className="live-check-symbol">{STATUS_SYMBOL[c.status] || "•"}</span>
            <span className="live-check-name">{c.name.replace(/_/g, " ")}</span>
          </motion.div>
        ))}
      </AnimatePresence>
      {awaiting && (
        <motion.div className="live-check live-check-pending" animate={{ opacity: [0.4, 1, 0.4] }} transition={{ duration: 1.3, repeat: Infinity }}>
          <span className="live-check-symbol">…</span>
          <span className="live-check-name">verifying</span>
        </motion.div>
      )}
    </div>
  );
}
