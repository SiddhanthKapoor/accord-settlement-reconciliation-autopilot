import { Fragment } from "react";
import { motion } from "motion/react";
import { flowStageIn, listIndexDelay } from "../motion.js";
import "../workspace.css";

/**
 * The five hops money makes between a booked order and a closed ledger.
 *
 * The canonical order matters: a stage is only "after" the one to its
 * left, and the first stage that is not FOUND is the breakpoint. Anything
 * the backend reports under a stage name not in this list is appended
 * rather than dropped, so a widened engine still renders.
 */
export const STAGE_ORDER = ["ORDER", "PAYMENT", "SETTLEMENT", "BANK", "BOOKS"];

export const STAGE_LABEL = {
  ORDER: "Orders",
  PAYMENT: "Payment gateway",
  SETTLEMENT: "Settlement",
  BANK: "Bank",
  BOOKS: "Books",
};

export function money(minor, { currency = "INR", decimals = 2 } = {}) {
  if (minor == null || Number.isNaN(Number(minor))) return "—";
  const symbol = currency === "INR" ? "₹" : `${currency} `;
  return (
    symbol +
    (Number(minor) / 100).toLocaleString("en-IN", {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals,
    })
  );
}

/**
 * Which hops of the money path a given kind of file can speak to.
 *
 * A statement about the files, not a measurement: a gateway payout export
 * carries both the capture and the payout date, so it covers two hops; a
 * bank statement covers the credit landing and nothing else. It decides
 * only whether a stage can be evaluated at all. A stage with no source
 * behind it is reported as not evaluated — never as zero, never as a
 * failure, and never with a percentage attached.
 */
export const TYPE_STAGES = {
  ORDERS: ["ORDER"],
  ACCOUNTING: ["BOOKS"],
  PAYMENT_GATEWAY: ["PAYMENT", "SETTLEMENT"],
  BANK_STATEMENT: ["BANK"],
  OTHER: [],
};

export function count(n) {
  return Number(n || 0).toLocaleString("en-IN");
}

function Arrow() {
  return (
    <div className="wk-flow-arrow" aria-hidden="true">
      <svg width="16" height="10" viewBox="0 0 16 10" fill="none">
        <path d="M0 5h13M9.5 1L14 5l-4.5 4" stroke="currentColor" strokeWidth="1.3" />
      </svg>
    </div>
  );
}

/**
 * A stage strip.
 *
 * `stages` are supplied by the caller already resolved — this component
 * never derives a number. A stage with `evaluated: false` renders as "not
 * evaluated", which is materially different from zero: it means no source
 * covering that hop was uploaded, so nothing about it is known. It is
 * never shown as a failure and never carries a percentage.
 */
export default function MoneyFlow({ stages, selected, onSelect, ariaLabel = "Money flow across stages" }) {
  if (!stages || stages.length === 0) return null;
  const interactive = typeof onSelect === "function";

  return (
    <>
      <div className="wk-flow" role="group" aria-label={ariaLabel}>
        {stages.map((stage, i) => {
          const active = selected === stage.key;
          const className = [
            "wk-flow-stage",
            stage.evaluated ? "" : "wk-flow-stage-dim",
            active ? "wk-flow-stage-active" : "",
          ]
            .filter(Boolean)
            .join(" ");

          const body = (
            <>
              <span className="wk-flow-name">{stage.label}</span>
              {stage.evaluated ? (
                <span className="wk-flow-value">{stage.headline}</span>
              ) : (
                <span className="wk-flow-value-none">Not evaluated</span>
              )}
              <span className="wk-flow-note">
                {stage.evaluated
                  ? stage.note
                  : stage.note || "Nothing in this workspace covers this stage."}
              </span>
            </>
          );

          const transition = {
            ...flowStageIn.transition,
            delay: listIndexDelay(i, 0.045, 6),
          };

          return (
            <Fragment key={stage.key}>
              {i > 0 && <Arrow />}
              {interactive && stage.selectable !== false ? (
                <motion.button
                  type="button"
                  className={className}
                  aria-pressed={active}
                  onClick={() => onSelect(active ? null : stage.key)}
                  {...flowStageIn}
                  transition={transition}
                >
                  {body}
                </motion.button>
              ) : (
                <motion.div className={className} {...flowStageIn} transition={transition}>
                  {body}
                </motion.div>
              )}
            </Fragment>
          );
        })}
      </div>
      <p className="wk-flow-legend">
        A stage marked <strong>not evaluated</strong> was not checked in this run. Accord reports
        nothing about a stage it did not observe — not a pass, not a failure, and never a
        percentage.
      </p>
    </>
  );
}
