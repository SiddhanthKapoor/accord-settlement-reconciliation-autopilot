import { motion } from "motion/react";
import { useState } from "react";
import { expand } from "../motion.js";
import { count, money } from "./MoneyFlow.jsx";
import "../workspace.css";

export const SOURCE_TYPES = [
  { value: "ORDERS", label: "Orders / invoices", role: "LEDGER", phrase: "an orders or invoice export" },
  { value: "ACCOUNTING", label: "Accounting / ERP export", role: "LEDGER", phrase: "an accounting or ERP export" },
  { value: "PAYMENT_GATEWAY", label: "Payment gateway payouts", role: "SETTLEMENT", phrase: "a payment gateway settlement export" },
  { value: "BANK_STATEMENT", label: "Bank statement", role: "SETTLEMENT", phrase: "a bank statement" },
  { value: "OTHER", label: "Other", role: "LEDGER", phrase: "some other financial export" },
];

export const TYPE_LABEL = Object.fromEntries(SOURCE_TYPES.map((t) => [t.value, t.label]));
const TYPE_PHRASE = Object.fromEntries(SOURCE_TYPES.map((t) => [t.value, t.phrase]));

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
 * Three independent gates, all hard. A required column that is not
 * mapped means the engine literally cannot read the file. A
 * low-confidence classification the operator has not confirmed means
 * Accord does not know which side of the reconciliation the file belongs
 * on, and guessing that wrong produces a page of confident nonsense. An
 * unacknowledged duplicate means the same money could be counted twice.
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
        source.detected_source_type
          ? TYPE_PHRASE[source.detected_source_type] || TYPE_LABEL[source.detected_source_type]
          : "a known source type"
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

/** What is holding this one row back, in three words, for the table. */
function statusOf(source) {
  const unmapped = source.detection?.unmapped_required || [];
  if (unmapped.length > 0) {
    return { key: "block", glyph: "✕", word: "Column unmapped", tone: "wk-st-block" };
  }
  if (source.duplicate_of && !source.duplicate_ack) {
    return { key: "dupe", glyph: "▲", word: "Possible duplicate", tone: "wk-st-ask" };
  }
  if (source.needs_confirmation && !source.confirmed) {
    return { key: "ask", glyph: "▲", word: "Needs your answer", tone: "wk-st-ask" };
  }
  return { key: "ready", glyph: "✓", word: "Ready", tone: "wk-st-ready" };
}

function Confidence({ value, confirmedByYou }) {
  // "You confirmed" is claimed only where the operator actually answered
  // in this session. A source the server marked role-confirmed on the way
  // in — the prepared workspace does this for every file — keeps showing
  // its real confidence instead, because telling someone they confirmed
  // something they never saw is a small lie with a large blast radius.
  if (confirmedByYou) {
    return (
      <span className="wk-conf-ok">
        <span aria-hidden="true">✓ </span>You confirmed
      </span>
    );
  }
  if (value == null) return <span className="wk-conf-none">Not classified</span>;
  const pct = Math.round(Math.max(0, Math.min(1, value)) * 100);
  const low = value < CONFIDENCE_FLOOR;
  return (
    <span className={`wk-conf${low ? " wk-conf-low" : ""}`}>
      <span className="wk-conf-pct">{pct}%</span>
      <span className="wk-conf-bar" aria-hidden="true">
        <span className="wk-conf-fill" style={{ width: `${pct}%` }} />
      </span>
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
  const { min_minor: lo, max_minor: hi } = range || {};
  if (lo != null && hi != null) {
    return `${money(lo, { currency: currency || "INR", decimals: 0 })} → ${money(hi, {
      currency: currency || "INR",
      decimals: 0,
    })}`;
  }
  return formatRange(range);
}

/**
 * The workspace inventory.
 *
 * A table, not a stack of cards. Twenty uploaded files is the normal
 * case, not the edge case, and twenty bordered panels is a scroll, not a
 * document. Every row is one file and one line; anything that needs more
 * room — the column mapping, the classifier's reasoning — opens beneath
 * the row that owns it and closes again.
 *
 * Rows are plain `<tr>` rather than animated ones on purpose: at fifty
 * files, per-row entrance animation is the difference between a table
 * appearing and a table arriving.
 */
export default function FileInventory({ sources, onConfirm, onRemove, onRemap, busy }) {
  const [open, setOpen] = useState(null);

  return (
    <div className="wk-tablewrap">
      <table className="wk-table wk-table-dense">
        <caption className="sr-only">
          Files in this workspace: what Accord read from each one, how confident it is about what
          the file is, and what is still needed before the run can start.
        </caption>
        <thead>
          <tr>
            <th scope="col">File</th>
            <th scope="col">Detected role</th>
            <th scope="col">Provider</th>
            <th scope="col" className="wk-col-num">Rows</th>
            <th scope="col">Confidence</th>
            <th scope="col">Status</th>
            <th scope="col">
              <span className="sr-only">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {sources.map((source) => {
            const expandedRow = open === source.source_id;
            const unmapped = source.detection?.unmapped_required || [];
            const status = statusOf(source);
            const askRole = source.needs_confirmation && !source.confirmed;
            const askDupe = !!source.duplicate_of && !source.duplicate_ack;
            const dateRange = formatRange(source.date_range);
            const amountRange = source.amount_range;
            const typeLabel = TYPE_LABEL[source.source_type] || source.source_type || "Unclassified";

            return (
              <Fragmented key={source.source_id}>
                <tr className={`wk-row-data${expandedRow ? " wk-row-open" : ""}`}>
                  <th scope="row" style={{ fontWeight: 400, textAlign: "left", padding: "7px 12px" }}>
                    <span className="wk-inv-name">{source.filename}</span>
                    <span className="wk-inv-side">
                      {source.role === "SETTLEMENT" ? "Settlement side" : "Ledger side"}
                      {source.currency ? ` · ${source.currency}` : ""}
                    </span>
                  </th>
                  <td>
                    <span className="wk-inv-type">{typeLabel}</span>
                    {source.detected_source_type &&
                      source.source_type !== source.detected_source_type && (
                        <span className="wk-inv-typenote">
                          read as {TYPE_LABEL[source.detected_source_type]}
                        </span>
                      )}
                  </td>
                  <td>
                    {source.provider ? (
                      <span className="wk-inv-provider">{source.provider}</span>
                    ) : (
                      <span className="wk-inv-provider wk-inv-provider-none">Not identified</span>
                    )}
                  </td>
                  <td className="wk-col-num">{count(source.row_count)}</td>
                  <td>
                    <Confidence
                      value={source.detection_confidence}
                      confirmedByYou={source.confirmed_by_you}
                    />
                  </td>
                  <td>
                    <span className={`wk-inv-status ${status.tone}`}>
                      <span className="wk-inv-status-glyph" aria-hidden="true">
                        {status.glyph}
                      </span>
                      {status.word}
                    </span>
                  </td>
                  <td>
                    <div className="wk-inv-actions">
                      <button
                        type="button"
                        className="wk-inv-rowlink"
                        aria-expanded={expandedRow}
                        aria-controls={`wk-cols-${source.source_id}`}
                        onClick={() => setOpen(expandedRow ? null : source.source_id)}
                      >
                        {expandedRow ? "Hide columns" : "Columns"}
                        {unmapped.length > 0 ? ` (${unmapped.length} unmapped)` : ""}
                      </button>
                      <button
                        type="button"
                        className="wk-inv-rowlink"
                        onClick={() => onRemove(source.source_id)}
                        disabled={busy}
                      >
                        Remove
                      </button>
                    </div>
                  </td>
                </tr>

                {(askRole || askDupe) && (
                  <tr>
                    <td colSpan={7} style={{ padding: "0 12px 12px" }}>
                      <div className="wk-decide">
                        {askDupe ? (
                          <p className="wk-decide-q">
                            <em>{source.filename}</em> looks like a duplicate of{" "}
                            <em>{source.duplicate_of_filename || source.duplicate_of}</em>. Keep both,
                            or remove this one?
                          </p>
                        ) : source.detected_source_type ? (
                          <p className="wk-decide-q">
                            We think <em>{source.filename}</em> is{" "}
                            {TYPE_PHRASE[source.detected_source_type] ||
                              TYPE_LABEL[source.detected_source_type]}
                            .
                          </p>
                        ) : (
                          <p className="wk-decide-q">
                            Accord could not work out what <em>{source.filename}</em> is. Which is it?
                          </p>
                        )}

                        <p className="wk-decide-why">
                          {askDupe
                            ? "The two files have identical contents — they hash the same. Reconciling the same rows twice would double-count the money in them, so keep both only if that is genuinely what you want."
                            : source.detection_confidence != null
                            ? `The evidence for that reading is weak (${Math.round(
                                source.detection_confidence * 100
                              )}% confidence). Reconciling a file against the wrong side of the ledger produces confident nonsense, so Accord asks rather than guessing.`
                            : "Accord will not put a file on a side of the reconciliation it cannot justify, so it asks rather than guessing."}
                        </p>

                        <div className="wk-decide-row">
                          {askDupe ? (
                            <>
                              <button
                                type="button"
                                className="wk-decide-yes"
                                disabled={busy || !source.source_type}
                                onClick={() => onConfirm(source, source.source_type, { confirm: true })}
                              >
                                Keep both
                              </button>
                              <button
                                type="button"
                                className="btn-ghost"
                                disabled={busy}
                                onClick={() => onRemove(source.source_id)}
                              >
                                Remove this copy
                              </button>
                            </>
                          ) : (
                            <>
                              <button
                                type="button"
                                className="wk-decide-yes"
                                disabled={busy || !source.source_type}
                                onClick={() => onConfirm(source, source.source_type, { confirm: true })}
                              >
                                {source.detected_source_type ? "That's right" : "Confirm"}
                              </button>
                              <span className="wk-decide-or">or change it to</span>
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
                            </>
                          )}
                        </div>
                      </div>
                    </td>
                  </tr>
                )}

                {expandedRow && (
                    <tr>
                      <td colSpan={7} style={{ padding: 0, borderBottom: "1px solid var(--wk-line)" }}>
                        <motion.div
                          id={`wk-cols-${source.source_id}`}
                          initial={expand.initial}
                          animate={expand.animate}
                          transition={expand.transition}
                          style={{ overflow: "hidden" }}
                        >
                          <div className="wk-expand">
                            {(dateRange || amountRange) && (
                              <div className="wk-facts" style={{ marginBottom: 12 }}>
                                {dateRange && (
                                  <span className="wk-fact">
                                    Dates <strong>{dateRange}</strong>
                                  </span>
                                )}
                                {amountRange &&
                                  (amountRange.min_minor != null || amountRange.min != null) && (
                                    <span className="wk-fact">
                                      Amounts{" "}
                                      <strong>{formatAmountRange(amountRange, source.currency)}</strong>
                                    </span>
                                  )}
                                <span className="wk-fact">
                                  Amount scale{" "}
                                  <strong>
                                    {source.detection?.amount_scale === "minor"
                                      ? "minor units (paise)"
                                      : "major units (rupees)"}
                                  </strong>
                                </span>
                                {source.detection?.debit_column && (
                                  <span className="wk-fact">
                                    Debit / credit{" "}
                                    <strong>
                                      {source.detection.debit_column} /{" "}
                                      {source.detection.credit_column}
                                    </strong>
                                  </span>
                                )}
                              </div>
                            )}
                            <p className="wk-sub" style={{ marginBottom: 12 }}>
                              Every column below was matched by header and by the values underneath
                              it. Where the evidence is weak, Accord asks rather than guessing — a
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
                          </div>
                        </motion.div>
                      </td>
                    </tr>
                  )}
              </Fragmented>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/**
 * A keyed fragment. `<>` cannot take a key, and a table body needs sibling
 * `<tr>`s rather than a wrapper element, so the grouping has to be a
 * fragment with an explicit key.
 */
function Fragmented({ children }) {
  return <>{children}</>;
}
