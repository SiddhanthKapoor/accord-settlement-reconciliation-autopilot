import { motion } from "motion/react";
import { listIndexDelay, traceStepIn } from "../motion.js";
import { STAGE_LABEL, money } from "./MoneyFlow.jsx";
import "../workspace.css";

/**
 * Trace status vocabulary.
 *
 * Every status carries a distinct glyph and a distinct word. Colour is
 * the third channel, never the only one — an operator with a red/green
 * deficiency has to be able to read this at a glance, and so does anyone
 * looking at a printed exception report.
 *
 * The distinction that matters most commercially is PENDING vs MISSING.
 * "Not settled yet, and not due yet" is a healthy state; "should have
 * arrived and did not" is an exception. They are drawn differently (a
 * dashed waiting ring against a solid cross) and worded differently.
 */
export const STATUS_META = {
  FOUND: { glyph: "✓", word: "Found", blurb: "Observed in the uploaded data." },
  MISSING: { glyph: "✕", word: "Missing", blurb: "Expected at this stage and not present." },
  PENDING: { glyph: "…", word: "Pending", blurb: "Not due yet — inside the expected window." },
  AMBIGUOUS: { glyph: "?", word: "Ambiguous", blurb: "More than one candidate fits; none is decisive." },
  CONTRADICTORY: { glyph: "!", word: "Contradictory", blurb: "Records at this stage disagree." },
  NOT_EVALUATED: { glyph: "–", word: "Not evaluated", blurb: "No source covering this stage was uploaded." },
};

export function statusMeta(status) {
  return STATUS_META[status] || { glyph: "·", word: String(status || "unknown"), blurb: "" };
}

export function statusClass(status) {
  return `wk-st-${String(status || "not_evaluated").toLowerCase()}`;
}

/** Status as an inline marker: shape + word, never colour alone. */
export function StatusMark({ status }) {
  const meta = statusMeta(status);
  return (
    <span className={`wk-status ${statusClass(status)}`}>
      <span className="wk-status-glyph" aria-hidden="true">{meta.glyph}</span>
      {meta.word}
    </span>
  );
}

/**
 * The money path for one record, top to bottom.
 *
 * `trace` comes straight from the investigator. Nothing is inferred here:
 * a stage the backend did not return is simply not drawn, and a stage it
 * returned as NOT_EVALUATED says so rather than being scored.
 */
export default function BreakpointTrace({ trace, breakpointStage, breakpointKind, currency = "INR" }) {
  if (!Array.isArray(trace) || trace.length === 0) {
    return (
      <p className="wk-empty">
        No money-flow trace was returned for this record.
      </p>
    );
  }

  return (
    <ol className="wk-trace">
      {trace.map((step, i) => {
        const meta = statusMeta(step.status);
        const isBreak = breakpointStage && step.stage === breakpointStage;
        return (
          <motion.li
            key={`${step.stage}-${i}`}
            className="wk-trace-step"
            {...traceStepIn}
            transition={{ ...traceStepIn.transition, delay: listIndexDelay(i, 0.05, 6) }}
          >
            <span className={`wk-trace-node ${statusClass(step.status)}`} aria-hidden="true">
              {meta.glyph}
            </span>
            <div className="wk-trace-body">
              <div className="wk-trace-head">
                <span className="wk-trace-stage">{STAGE_LABEL[step.stage] || step.stage}</span>
                <span className={`wk-trace-status ${statusClass(step.status)}`}>{meta.word}</span>
                {step.amount_minor != null && (
                  <span className="wk-trace-amount">{money(step.amount_minor, { currency })}</span>
                )}
              </div>
              <p className="wk-trace-detail">{step.detail || meta.blurb}</p>
              {Array.isArray(step.evidence) && step.evidence.length > 0 && (
                <ul className="wk-trace-evidence">
                  {step.evidence.map((e, j) => (
                    <li key={j}>{e}</li>
                  ))}
                </ul>
              )}
              {isBreak && (
                <span className="wk-breakpoint-flag">
                  <span aria-hidden="true">◆</span>
                  Breakpoint{breakpointKind && breakpointKind !== "NONE" ? ` · ${breakpointKind.toLowerCase()}` : ""}
                </span>
              )}
            </div>
          </motion.li>
        );
      })}
    </ol>
  );
}
