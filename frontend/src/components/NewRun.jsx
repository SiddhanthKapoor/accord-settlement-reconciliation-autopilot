import { AnimatePresence, motion } from "motion/react";
import { useCallback, useRef, useState } from "react";
import {
  createRun, executeRun, removeSource, updateMapping, uploadSource,
} from "../api.js";
import { DURATION, EASE, expand, listIndexDelay, riseIn } from "../motion.js";

const SOURCE_TYPES = [
  { value: "ORDERS", label: "Orders / Invoices", role: "Ledger", hint: "What the business booked" },
  { value: "ACCOUNTING", label: "Accounting / ERP", role: "Ledger", hint: "General ledger export" },
  { value: "PAYMENT_GATEWAY", label: "Payment Gateway", role: "Settlement", hint: "Payout or settlement file" },
  { value: "BANK_STATEMENT", label: "Bank Statement", role: "Settlement", hint: "Account statement" },
  { value: "OTHER", label: "Other CSV", role: "Ledger", hint: "Anything else" },
];

const CANONICAL_LABELS = {
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

export default function NewRun({ onRunStarted }) {
  const [run, setRun] = useState(null);
  const [sources, setSources] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState("");
  const [dragging, setDragging] = useState(null);
  const [expanded, setExpanded] = useState(null);
  const fileInputs = useRef({});

  const ensureRun = useCallback(async () => {
    if (run) return run;
    const created = await createRun(null);
    setRun(created);
    return created;
  }, [run]);

  async function addFile(file, sourceType) {
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      const current = await ensureRun();
      const source = await uploadSource(current.run_id, file, sourceType);
      setSources((prev) => [...prev, source]);
      setNotice(
        source.needs_user_input
          ? `${file.name} uploaded — some columns need mapping.`
          : `${file.name} uploaded, ${source.detection.row_count} rows detected.`
      );
      if (source.needs_user_input) setExpanded(source.source_id);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function drop(event, sourceType) {
    event.preventDefault();
    setDragging(null);
    const file = event.dataTransfer?.files?.[0];
    if (file) await addFile(file, sourceType);
  }

  async function remap(source, canonical, column) {
    const mapping = { ...source.mapping };
    if (column) mapping[canonical] = column;
    else delete mapping[canonical];
    try {
      const updated = await updateMapping(run.run_id, source.source_id, { mapping });
      setSources((prev) =>
        prev.map((s) =>
          s.source_id === source.source_id
            ? {
                ...s,
                mapping: updated.mapping,
                needs_user_input: updated.unmapped_required.length > 0,
                detection: { ...s.detection, unmapped_required: updated.unmapped_required },
              }
            : s
        )
      );
    } catch (e) {
      setError(e.message);
    }
  }

  async function drop_(sourceId) {
    try {
      await removeSource(run.run_id, sourceId);
      setSources((prev) => prev.filter((s) => s.source_id !== sourceId));
    } catch (e) {
      setError(e.message);
    }
  }

  async function start() {
    setBusy(true);
    setError(null);
    try {
      const result = await executeRun(run.run_id, null);
      onRunStarted?.(run.run_id, result);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  const hasLedger = sources.some((s) => s.role === "LEDGER");
  const hasSettlement = sources.some((s) => s.role === "SETTLEMENT");
  const blocked = sources.filter((s) => s.needs_user_input);
  const canRun = hasLedger && hasSettlement && blocked.length === 0 && !busy;

  return (
    <div className="page">
      <div className="page-header">
        <h1 className="page-title">New reconciliation run</h1>
        <p className="page-subtitle">
          Upload what you have. At least one ledger source (orders, invoices, an accounting export)
          and one settlement source (a gateway payout file or a bank statement). Columns are detected
          automatically, and anything uncertain is asked rather than guessed.
        </p>
      </div>

      <div className="sr-only" role="status" aria-live="polite">{notice}</div>

      {error && (
        <motion.div className="card alert-error" role="alert" {...riseIn}>
          <p className="small">{error}</p>
        </motion.div>
      )}

      <div className="upload-grid">
        {SOURCE_TYPES.slice(0, 4).map((type, i) => (
          <motion.div
            key={type.value}
            {...riseIn}
            transition={{ ...riseIn.transition, delay: listIndexDelay(i) }}
            className={`dropzone${dragging === type.value ? " dropzone-active" : ""}`}
            onDragOver={(e) => {
              e.preventDefault();
              setDragging(type.value);
            }}
            onDragLeave={() => setDragging(null)}
            onDrop={(e) => drop(e, type.value)}
          >
            <div className="dropzone-role">{type.role}</div>
            <div className="dropzone-title">{type.label}</div>
            <div className="dropzone-hint">{type.hint}</div>
            <input
              ref={(el) => (fileInputs.current[type.value] = el)}
              id={`file-${type.value}`}
              type="file"
              accept=".csv,text/csv"
              className="sr-only"
              onChange={(e) => {
                addFile(e.target.files?.[0], type.value);
                e.target.value = "";
              }}
            />
            <label htmlFor={`file-${type.value}`} className="btn-ghost dropzone-button">
              Choose CSV
            </label>
            <div className="dropzone-drop">or drop a file here</div>
          </motion.div>
        ))}
      </div>

      <AnimatePresence>
        {sources.length > 0 && (
          <motion.div {...expand} style={{ overflow: "hidden" }}>
            <div className="card" style={{ marginTop: 22 }}>
              <h2 className="card-title">Sources ({sources.length})</h2>
              <ul className="source-list">
                {sources.map((source) => {
                  const open = expanded === source.source_id;
                  const unmapped = source.detection.unmapped_required || [];
                  return (
                    <li key={source.source_id} className="source-item">
                      <div className="source-head">
                        <span className={`role-chip role-${source.role.toLowerCase()}`}>
                          {source.role === "LEDGER" ? "Ledger" : "Settlement"}
                        </span>
                        <span className="source-name mono">{source.filename}</span>
                        <span className="tiny muted">{source.detection.row_count} rows</span>
                        {unmapped.length > 0 && (
                          <span className="badge badge-warn">
                            needs mapping: {unmapped.join(", ")}
                          </span>
                        )}
                        <div className="source-actions">
                          <button
                            type="button"
                            className="btn-ghost"
                            aria-expanded={open}
                            aria-controls={`map-${source.source_id}`}
                            onClick={() => setExpanded(open ? null : source.source_id)}
                          >
                            {open ? "Hide columns" : "Review columns"}
                          </button>
                          <button type="button" className="btn-ghost" onClick={() => drop_(source.source_id)}>
                            Remove
                          </button>
                        </div>
                      </div>

                      <AnimatePresence initial={false}>
                        {open && (
                          <motion.div
                            id={`map-${source.source_id}`}
                            {...expand}
                            style={{ overflow: "hidden" }}
                          >
                            <div className="mapping-grid">
                              {Object.keys(CANONICAL_LABELS).map((canonical) => {
                                const guess = source.detection.guesses.find(
                                  (g) => g.canonical === canonical
                                );
                                const required = REQUIRED.includes(canonical);
                                const missing = required && !source.mapping[canonical];
                                return (
                                  <div className="mapping-row" key={canonical}>
                                    <label htmlFor={`${source.source_id}-${canonical}`}>
                                      {CANONICAL_LABELS[canonical]}
                                      {required && <span className="required-mark" aria-hidden="true"> *</span>}
                                      {required && <span className="sr-only"> (required)</span>}
                                    </label>
                                    <select
                                      id={`${source.source_id}-${canonical}`}
                                      className={`select-field${missing ? " select-missing" : ""}`}
                                      value={source.mapping[canonical] || ""}
                                      onChange={(e) => remap(source, canonical, e.target.value)}
                                    >
                                      <option value="">— not mapped —</option>
                                      {source.detection.columns.map((c) => (
                                        <option key={c} value={c}>{c}</option>
                                      ))}
                                    </select>
                                    <span className="tiny muted mapping-reason">
                                      {guess
                                        ? `${Math.round(guess.confidence * 100)}% · ${guess.reason}`
                                        : "not detected"}
                                    </span>
                                  </div>
                                );
                              })}
                            </div>
                            <p className="tiny muted" style={{ marginTop: 10 }}>
                              Amounts read as{" "}
                              <strong>
                                {source.detection.amount_scale === "minor"
                                  ? "minor units (paise)"
                                  : "major units (rupees)"}
                              </strong>
                              {source.detection.debit_column &&
                                ` · split debit/credit columns detected (${source.detection.debit_column} / ${source.detection.credit_column})`}
                            </p>
                          </motion.div>
                        )}
                      </AnimatePresence>
                    </li>
                  );
                })}
              </ul>

              <div className="run-bar">
                <div className="run-readiness">
                  <Requirement met={hasLedger} label="Ledger source" />
                  <Requirement met={hasSettlement} label="Settlement source" />
                  <Requirement met={blocked.length === 0} label="Columns mapped" />
                </div>
                <motion.button
                  type="button"
                  className="btn-small"
                  disabled={!canRun}
                  onClick={start}
                  whileHover={canRun ? { y: -1 } : undefined}
                  whileTap={canRun ? { scale: 0.985 } : undefined}
                  transition={{ duration: DURATION.instant, ease: EASE }}
                >
                  {busy ? "Starting…" : "Run reconciliation"}
                </motion.button>
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

function Requirement({ met, label }) {
  return (
    <span className={`requirement${met ? " requirement-met" : ""}`}>
      <span aria-hidden="true" className="requirement-mark">{met ? "✓" : "○"}</span>
      {label}
      <span className="sr-only">{met ? " satisfied" : " not yet satisfied"}</span>
    </span>
  );
}
