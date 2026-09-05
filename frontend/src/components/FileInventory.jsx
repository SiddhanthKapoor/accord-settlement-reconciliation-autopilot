import { AnimatePresence, motion } from "motion/react";
import { useState } from "react";
import { DURATION, EASE, expand, listIndexDelay } from "../motion.js";
import { count, money } from "./MoneyFlow.jsx";
import "../workspace.css";

export const SOURCE_TYPES = [
  { value: "ORDERS", label: "Orders / invoices", role: "LEDGER" },
  { value: "ACCOUNTING", label: "Accounting / ERP export", role: "LEDGER" },
  { value: "PAYMENT_GATEWAY", label: "Payment gateway payouts", role: "SETTLEMENT" },
  { value: "BANK_STATEMENT", label: "Bank statement", role: "SETTLEMENT" },
  { value: "OTHER", label: "Other", role: "LEDGER" },
];

export const TYPE_LABEL = Object.fromEntries(SOURCE_TYPES.map((t) => [t.value, t.label]));

export const CANONICAL_LABELS = {
  transaction_id: "Transaction ID",
  reference: "Reference",
  amount: "Amount",
  net_amount: "Net amount",
  currency: "Currency",
  date: "Date",
  settlement_date: "Settlement date",
  description: "Description",
  status: "Status",
  fee: "Fee",
  tax: "Tax",
  refund_amount: "Refund amount",
  counterparty: "Counterparty",
};

const REQUIRED = ["amount", "date"];

/** Below this, the classification is offered as a question, not a result. */
export const CONFIDENCE_FLOOR = 0.75;

/**
 * Is this row still holding the run back, and why?
 *
 * Two independent gates, both hard. A required column that is not mapped
 * means the engine literally cannot read the file. A low-confidence
 * classification that the operator has not confirmed means Accord does
 * not know which side of the reconciliation the file belongs on, and
 * guessing that wrong produces a page of confident nonsense.
 */
export function blockersFor(source) {
  const out = [];
  const unmapped = source.detection?.unmapped_required || [];
  if (unmapped.length > 0) {
    out.push(
      `${source.filename}: ${unmapped.map((c) => CANONICAL_LABELS[c] || c).join(" and ")} ${
        unmapped.length === 1 ? "column is" : "columns are"
      } not mapped.`
    );
  }
  if (source.needs_confirmation && !source.confirmed) {
    out.push(
      `${source.filename}: Accord is not confident this is ${
        source.detected_source_type ? TYPE_LABEL[source.detected_source_type] || source.detected_source_type : "a known source type"
      } — confirm what it is.`
    );
  }
  if (source.duplicate_of && !source.duplicate_ack) {
    out.push(
      `${source.filename}: looks like a duplicate of ${
        source.duplicate_of_filename || "an earlier upload"
      } — keep it or remove it.`
    );
  }
  return out;
}

function Confidence({ value, confirmed }) {
  // A file the operator has settled is no longer a question. Continuing to
  // label it "not classified" after they answered reads as the product
  // ignoring them.
  if (confirmed) {
    return (
      <span className="wk-conf wk-conf-ok">
        <span aria-hidden="true">✓</span> confirmed by you
      </span>
    );
  }
  if (value == null) {
    return <span className="wk-conf wk-conf-low">not classified</span>;
  }
  const pct = Math.round(Math.max(0, Math.min(1, value)) * 100);
  const low = value < CONFIDENCE_FLOOR;
  return (
    <span className={`wk-conf${low ? " wk-conf-low" : ""}`}>
      <span className="wk-conf-bar" aria-hidden="true">
        <motion.span
          className="wk-conf-fill"
          initial={{ width: 0 }}
          animate={{ width: `${pct}%` }}
          transition={{ duration: DURATION.slow, ease: EASE }}
        />
      </span>
      {pct}% {low ? "· low" : "· confident"}
    </span>
  );
}

function formatRange(range) {
  if (!range) return null;
  const from = range.from ?? range.min;
  const to = range.to ?? range.max;
  if (from == null && to == null) return null;
  return `${from ?? "?"} → ${to ?? "?"}`;
}

/** Money reads as money, with separators, not as a bare float. */
function formatAmountRange(range, currency) {
  const { min_minor: lo, max_minor: hi } = range;
  if (lo != null && hi != null) {
    return `${money(lo, { currency: currency || "INR", decimals: 0 })} → ${money(hi, {
      currency: currency || "INR",
      decimals: 0,
    })}`;
  }
  return formatRange(range);
}

/**
 * One row per uploaded file: what it is, how sure we are, how much is in
 * it, and — where we are not sure — an explicit control to settle it.
 */
export default function FileInventory({ sources, onConfirm, onRemove, onRemap, busy }) {
  const [open, setOpen] = useState(null);

  return (
    <ul className="wk-inv">
      <AnimatePresence initial={false}>
        {sources.map((source, i) => {
          const expandedRow = open === source.source_id;
          const unmapped = source.detection?.unmapped_required || [];
          // A duplicate warning has to be dismissible, or a workspace that
          // legitimately holds two similar exports nags for ever. Keeping
          // it is a decision the operator is allowed to make, once.
          const needsConfirm =
            (source.needs_confirmation && !source.confirmed) ||
            (!!source.duplicate_of && !source.duplicate_ack);
          const dateRange = formatRange(source.date_range);
          const amountRange = source.amount_range;

          return (
            <motion.li
              key={source.source_id}
              className="wk-inv-item"
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, height: 0, marginTop: 0, paddingTop: 0, paddingBottom: 0 }}
              transition={{ duration: DURATION.fast, ease: EASE, delay: listIndexDelay(i, 0.035, 6) }}
              layout
            >
              <div className="wk-inv-head">
                <div className="wk-inv-file">
                  <div className="wk-inv-name">{source.filename}</div>
                  <div className="wk-inv-sub">
                    {source.role === "SETTLEMENT" ? "Settlement side" : "Ledger side"}
                    {source.currency ? ` · ${source.currency}` : ""}
                  </div>
                </div>

                <div className="wk-inv-class">
                  <span className="wk-inv-type">
                    {TYPE_LABEL[source.source_type] || source.source_type || "Unclassified"}
                  </span>
                  {source.provider && <span className="wk-inv-provider">{source.provider}</span>}
                  <Confidence value={source.detection_confidence} confirmed={source.confirmed} />
                </div>

                <div className="wk-inv-stat">
                  <div className="wk-inv-count">{count(source.row_count)}</div>
                  <div className="wk-inv-count-label">records</div>
                </div>

                <div className="wk-inv-actions">
                  <button
                    type="button"
                    className="btn-ghost"
                    aria-expanded={expandedRow}
                    aria-controls={`wk-cols-${source.source_id}`}
                    onClick={() => setOpen(expandedRow ? null : source.source_id)}
                  >
                    {expandedRow ? "Hide columns" : "Review columns"}
                    {unmapped.length > 0 && (
                      <span className="badge badge-warn" style={{ marginLeft: 6 }}>
                        {unmapped.length} unmapped
                      </span>
                    )}
                  </button>
                  <button
                    type="button"
                    className="btn-ghost"
                    onClick={() => onRemove(source.source_id)}
                    disabled={busy}
                  >
                    Remove
                  </button>
                </div>
              </div>

              {(dateRange || amountRange) && (
                <div className="wk-facts">
                  {dateRange && (
                    <span className="wk-fact">
                      Dates <strong>{dateRange}</strong>
                    </span>
                  )}
                  {amountRange && (amountRange.min_minor != null || amountRange.min != null) && (
                    <span className="wk-fact">
                      Amounts <strong>{formatAmountRange(amountRange, source.currency)}</strong>
                    </span>
                  )}
                </div>
              )}

              {needsConfirm && (
                <div className="wk-confirm">
                  <div className="wk-confirm-title">
                    <span aria-hidden="true">▲</span>
                    {source.duplicate_of ? "Possible duplicate" : "Confirm what this file is"}
                  </div>
                  <p className="wk-confirm-body">
                    {source.duplicate_of
                      ? `The contents look like ${
                          source.duplicate_of_filename || source.duplicate_of
                        }, already in this workspace. Reconciling the same records twice would double-count them.`
                      : source.detected_source_type
                      ? `Accord read this as ${
                          TYPE_LABEL[source.detected_source_type] || source.detected_source_type
                        } but is not confident. Reconciling against the wrong side produces confident nonsense, so it asks.`
                      : "Accord could not classify this file. Tell it which side of the reconciliation the file belongs on."}
                  </p>
                  <div className="wk-confirm-row">
                    <label className="sr-only" htmlFor={`wk-type-${source.source_id}`}>
                      Source type for {source.filename}
                    </label>
                    <select
                      id={`wk-type-${source.source_id}`}
                      className="select-field"
                      value={source.source_type || ""}
                      onChange={(e) => onConfirm(source, e.target.value, { confirm: false })}
                    >
                      <option value="" disabled>
                        Choose a source type…
                      </option>
                      {SOURCE_TYPES.map((t) => (
                        <option key={t.value} value={t.value}>
                          {t.label}
                        </option>
                      ))}
                    </select>
                    <button
                      type="button"
                      className="btn-small"
                      disabled={busy || !source.source_type}
                      onClick={() => onConfirm(source, source.source_type, { confirm: true })}
                    >
                      {source.duplicate_of && !source.duplicate_ack ? "Keep both" : "Confirm"}
                    </button>
                    {source.duplicate_of && (
                      <button
                        type="button"
                        className="btn-ghost"
                        disabled={busy}
                        onClick={() => onRemove(source.source_id)}
                      >
                        Remove the duplicate
                      </button>
                    )}
                  </div>
                </div>
              )}

              <AnimatePresence initial={false}>
                {expandedRow && (
                  <motion.div
                    id={`wk-cols-${source.source_id}`}
                    {...expand}
                    style={{ overflow: "hidden" }}
                  >
                    <div style={{ paddingTop: 12 }}>
                      <p className="wk-sub" style={{ marginBottom: 10 }}>
                        Every column below was matched by header and by the values underneath it.
                        Where the evidence is weak, Accord asks rather than guessing — a
                        mis-read amount column is the one error this product cannot afford.
                      </p>
                      <div className="wk-map-grid">
                        {Object.keys(CANONICAL_LABELS).map((canonical) => {
                          const guess = (source.detection?.guesses || []).find(
                            (g) => g.canonical === canonical
                          );
                          const required = REQUIRED.includes(canonical);
                          const missing = required && !source.mapping?.[canonical];
                          return (
                            <div className="wk-map-row" key={canonical}>
                              <label htmlFor={`${source.source_id}-${canonical}`}>
                                {CANONICAL_LABELS[canonical]}
                                {required && (
                                  <>
                                    <span aria-hidden="true"> *</span>
                                    <span className="sr-only"> (required)</span>
                                  </>
                                )}
                              </label>
                              <select
                                id={`${source.source_id}-${canonical}`}
                                className={`select-field${missing ? " wk-map-missing" : ""}`}
                                value={source.mapping?.[canonical] || ""}
                                onChange={(e) => onRemap(source, canonical, e.target.value)}
                              >
                                <option value="">— not mapped —</option>
                                {(source.detection?.columns || []).map((c) => (
                                  <option key={c} value={c}>
                                    {c}
                                  </option>
                                ))}
                              </select>
                              <span className="wk-map-reason">
                                {guess
                                  ? `${Math.round(guess.confidence * 100)}% · ${guess.reason}`
                                  : "not detected"}
                              </span>
                            </div>
                          );
                        })}
                      </div>
                      <p className="wk-map-reason" style={{ marginTop: 10 }}>
                        Amounts read as{" "}
                        <strong>
                          {source.detection?.amount_scale === "minor"
                            ? "minor units (paise)"
                            : "major units (rupees)"}
                        </strong>
                        {source.detection?.debit_column
                          ? ` · split debit/credit columns detected (${source.detection.debit_column} / ${source.detection.credit_column})`
                          : ""}
                      </p>
                    </div>
                  </motion.div>
                )}
              </AnimatePresence>
            </motion.li>
          );
        })}
      </AnimatePresence>
    </ul>
  );
}
