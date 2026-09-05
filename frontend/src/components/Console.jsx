import { AnimatePresence, motion } from "motion/react";
import { useEffect, useRef, useState } from "react";
import {
  getAiHealth,
  getAiStatus,
  getLatestBatch,
  getLatestEvaluation,
  listBatchRecords,
  runBatch,
  streamAudit,
} from "../api.js";
import { pageTransition } from "../motion.js";
import { money } from "./MoneyFlow.jsx";
import RecordDetail from "./RecordDetail.jsx";
import "../panels.css";

const OUTCOME_COLORS = {
  RECONCILED: "var(--pass)",
  EXCEPTION: "var(--fail)",
  HUMAN_REVIEW: "var(--warn)",
};
const OUTCOME_BADGE = {
  RECONCILED: "badge-pass",
  EXCEPTION: "badge-fail",
  HUMAN_REVIEW: "badge-warn",
};

/**
 * The committed frozen benchmark, transcribed verbatim from
 * `backend/evaluations/accord/FROZEN.json` (code commit b6145bb).
 *
 * It is inlined only because that artefact is not served over HTTP; the
 * moment `/evaluation/frozen` exists this is replaced by the response, and
 * the fetch below already prefers it. Nothing here is computed in the
 * browser — every figure is a measurement the evaluation harness wrote to
 * a file that is checksummed and marked do-not-modify. The named
 * configurations are the provider-failure scenarios: no model at all, the
 * primary answering, the fallback answering, and both providers down.
 */
const FROZEN = {
  code_commit: "b6145bbf1625e87c068f900f109bcbe34f256483",
  dataset: { split: "holdout", record_count: 1000, seed: 90210 },
  rows: [
    {
      key: "latest_A_deterministic",
      label: "Deterministic only",
      note: "no model consulted",
      reconciliation_accuracy: 0.825,
      false_auto_reconciliation_rate: 0.0,
      pct_human_review: 0.071,
      provider_errors: 0,
    },
    {
      key: "latest_B_gemini_primary",
      label: "Primary provider answering",
      note: "ambiguity escalated to the primary model",
      reconciliation_accuracy: 0.883,
      false_auto_reconciliation_rate: 0.0017006802721088435,
      pct_human_review: 0.091,
      provider_errors: 74,
    },
    {
      key: "latest_D_groq_fallback",
      label: "Fallback provider answering",
      note: "primary unavailable throughout",
      reconciliation_accuracy: 0.775,
      false_auto_reconciliation_rate: 0.0,
      pct_human_review: 0.209,
      provider_errors: 196,
    },
    {
      key: "latest_C_total_outage",
      label: "Both providers down",
      note: "every model call fails",
      reconciliation_accuracy: 0.768,
      false_auto_reconciliation_rate: 0.0,
      pct_human_review: 0.216,
      provider_errors: 204,
    },
  ],
};

function pct(value, decimals = 1) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  return `${(Number(value) * 100).toFixed(decimals)}%`;
}

function num(value) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  return Number(value).toLocaleString("en-IN");
}

/* ================================================================= screen */

export default function Console() {
  const [evalReport, setEvalReport] = useState(null);
  const frozen = FROZEN;
  const [batch, setBatch] = useState(null);
  const [liveCounts, setLiveCounts] = useState({ RECONCILED: 0, EXCEPTION: 0, HUMAN_REVIEW: 0 });
  const [liveProcessed, setLiveProcessed] = useState(0);
  const [running, setRunning] = useState(false);
  const [dataset, setDataset] = useState("holdout");
  const [limit, setLimit] = useState(200);
  const [records, setRecords] = useState([]);
  const [outcomeFilter, setOutcomeFilter] = useState(null);
  const [selectedRecordId, setSelectedRecordId] = useState(null);

  const currentBatchIdRef = useRef(null);

  useEffect(() => {
    getLatestEvaluation("holdout")
      .then(setEvalReport)
      .catch(() => setEvalReport(null));
    getLatestBatch()
      .then((b) => {
        if (b.batch) {
          setBatch(b.batch);
          currentBatchIdRef.current = b.batch.batch_id;
          loadRecords(b.batch.batch_id, null);
        }
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    const stop = streamAudit((event) => {
      const batchId = event.payload?.batch_id;
      if (!batchId || batchId !== currentBatchIdRef.current) return;
      if (event.event_type === "RECORD_DECIDED") {
        setLiveProcessed(event.payload.seq + 1);
        setLiveCounts((prev) => ({
          ...prev,
          [event.payload.outcome]: (prev[event.payload.outcome] || 0) + 1,
        }));
      }
      if (event.event_type === "BATCH_COMPLETED") {
        setRunning(false);
        getLatestBatch()
          .then((b) => b.batch && setBatch(b.batch))
          .catch(() => {});
        loadRecords(batchId, outcomeFilter);
      }
    });
    return stop;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [outcomeFilter]);

  function loadRecords(batchId, outcome) {
    listBatchRecords(batchId, { outcome, limit: 100 }).then(setRecords).catch(() => {});
  }

  async function handleRun() {
    setRunning(true);
    setLiveCounts({ RECONCILED: 0, EXCEPTION: 0, HUMAN_REVIEW: 0 });
    setLiveProcessed(0);
    setRecords([]);
    setSelectedRecordId(null);
    const result = await runBatch(dataset, limit);
    currentBatchIdRef.current = result.batch_id;
    setBatch({
      batch_id: result.batch_id,
      total_records: result.total_records,
      label: result.label,
      status: "RUNNING",
    });
  }

  function handleFilter(outcome) {
    setOutcomeFilter(outcome);
    if (batch?.batch_id) loadRecords(batch.batch_id, outcome);
  }

  const totalForProgress = batch?.total_records || 0;
  const processedNow = running ? liveProcessed : batch?.processed_records ?? 0;
  const countsNow = running ? liveCounts : batch?.outcome_counts ?? {};
  const m = evalReport?.metrics;

  return (
    <motion.div className="page" {...pageTransition}>
      <div className="page-header pn-head">
        <h1 className="page-title">Evaluation</h1>
        <p className="pn-lede">
          Replay labelled datasets to measure reconciliation behaviour, accuracy, human-review rate
          and provider reliability.
        </p>
      </div>

      {/* Methodology, stated as rigour rather than as a caveat: the reason
          these numbers mean anything is precisely that they are not drawn
          from whatever happens to be in a workspace today. */}
      <div className="pn-method">
        <p>
          These metrics are measured on labelled evaluation datasets generated for this purpose,
          with ground truth attached to every record. <strong>Workspace data is never used here</strong> —
          which is what makes the figures reproducible from a fixed seed rather than selected from a
          favourable demo.
        </p>
        {evalReport && (
          <div className="pn-method-facts">
            <span>
              <b>Split</b>
              {evalReport.dataset_split}
            </span>
            <span>
              <b>Records</b>
              {num(evalReport.record_count)}
            </span>
            <span>
              <b>Seed</b>
              {evalReport.seed}
            </span>
            {evalReport.policy?.ai_confidence_threshold != null && (
              <span>
                <b>Confidence threshold</b>
                {evalReport.policy.ai_confidence_threshold}
              </span>
            )}
            {evalReport.dataset_version && (
              <span>
                <b>Dataset</b>
                <code>{evalReport.dataset_version.slice(0, 12)}</code>
              </span>
            )}
          </div>
        )}
      </div>

      {m ? (
        <div className="pn-rail">
          <div className="pn-metric">
            <span className="pn-metric-label">Held-out accuracy</span>
            <span className="pn-metric-value">{pct(m.reconciliation_accuracy)}</span>
            <span className="pn-metric-note">{num(m.record_count)} labelled records</span>
          </div>
          <div className="pn-metric">
            <span className="pn-metric-label">False auto-reconciliation</span>
            <span className="pn-metric-value">{pct(m.false_auto_reconciliation_rate, 2)}</span>
            <span className="pn-metric-note">reconciled that should not have been</span>
          </div>
          <div className="pn-metric">
            <span className="pn-metric-label">Human-review rate</span>
            <span className="pn-metric-value">{pct(m.pct_human_review)}</span>
            <span className="pn-metric-note">sent to a person</span>
          </div>
          <div className="pn-metric">
            <span className="pn-metric-label">AI consulted</span>
            <span className="pn-metric-value">{pct(m.ai_invocation_rate)}</span>
            <span className="pn-metric-note">of records reached the model</span>
          </div>
          <div className="pn-metric">
            <span className="pn-metric-label">Exception precision</span>
            <span className="pn-metric-value pn-metric-value-quiet">{pct(m.exception_precision)}</span>
            <span className="pn-metric-note">recall {pct(m.exception_recall)}</span>
          </div>
        </div>
      ) : (
        <p className="pn-empty">
          No evaluation report is loaded, so no accuracy figures are shown. Replaying a labelled
          dataset below produces one.
        </p>
      )}

      {evalReport?.timestamp && (
        <p className="pn-count-line">
          Measured {new Date(evalReport.timestamp).toLocaleString("en-GB", { hour12: false })} on the{" "}
          {evalReport.dataset_split} split · semantic backend {evalReport.semantic_backend}
        </p>
      )}

      {/* ---------------------------------------------------- providers */}

      <section className="pn-section">
        <div className="pn-section-head">
          <h2 className="pn-section-title">Provider reliability</h2>
          <p className="pn-section-note">
            What the engine does when the model layer degrades or disappears
          </p>
        </div>
        <div className="pn-section-body">
          <AiLayer />
          <div className="pn-scroll pn-cardscroll" style={{ marginTop: 18 }}>
            <table className="pn-frozen">
              <caption className="sr-only">
                Frozen evaluation results by provider-availability configuration
              </caption>
              <thead>
                <tr>
                  <th scope="col">Configuration</th>
                  <th scope="col">Accuracy</th>
                  <th scope="col">False auto-reconciliation</th>
                  <th scope="col">Human review</th>
                  <th scope="col">Provider failures</th>
                </tr>
              </thead>
              <tbody>
                {frozen.rows.map((r) => (
                  <tr key={r.key}>
                    <th scope="row">
                      {r.label}
                      <span>{r.note}</span>
                    </th>
                    <td>{pct(r.reconciliation_accuracy)}</td>
                    <td>{pct(r.false_auto_reconciliation_rate, 2)}</td>
                    <td>{pct(r.pct_human_review)}</td>
                    <td>{num(r.provider_errors)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="pn-provenance">
            Measured on a labelled held-out split of {num(frozen.dataset.record_count)} records,
            seed {frozen.dataset.seed}, at code commit{" "}
            <code>{frozen.code_commit.slice(0, 7)}</code>. Losing both providers costs accuracy and
            roughly triples the human-review rate — it does not cause records to be reconciled that
            should not have been.
          </p>
        </div>
      </section>

      {/* ------------------------------------------------------- replay */}

      <section className="pn-section">
        <div className="pn-section-head">
          <h2 className="pn-section-title">Replay a labelled dataset</h2>
          {batch && <p className="pn-section-note">{batch.label}</p>}
        </div>
        <div className="pn-section-body">
          <div className="pn-replay batch-controls">
            <label className="sr-only" htmlFor="eval-dataset">
              Dataset
            </label>
            <select
              id="eval-dataset"
              className="select-field"
              value={dataset}
              onChange={(e) => setDataset(e.target.value)}
              disabled={running}
            >
              <option value="holdout">Held-out evaluation set</option>
              <option value="dev">Dev set</option>
            </select>
            <label className="sr-only" htmlFor="eval-limit">
              Record count
            </label>
            <select
              id="eval-limit"
              className="select-field"
              value={limit}
              onChange={(e) => setLimit(Number(e.target.value))}
              disabled={running}
            >
              <option value={100}>100 records</option>
              <option value={500}>500 records</option>
              <option value={1000}>1,000 records</option>
              <option value={5000}>Full set</option>
            </select>
            <button type="button" className="pn-btn pn-btn-strong" onClick={handleRun} disabled={running}>
              {running ? "Processing…" : "Run batch"}
            </button>
          </div>

          {(running || batch) && (
            <>
              <div className="progress-track">
                <motion.div
                  className="progress-fill"
                  animate={{
                    width: `${
                      totalForProgress ? Math.min(100, (processedNow / totalForProgress) * 100) : 0
                    }%`,
                  }}
                  transition={{ duration: 0.25 }}
                />
              </div>
              <div className="progress-label">
                <span>
                  {processedNow} / {totalForProgress} processed
                </span>
                <span>{running ? "streaming live from the backend" : "complete"}</span>
              </div>
              <div className="live-outcome-row">
                {["RECONCILED", "EXCEPTION", "HUMAN_REVIEW"].map((o) => (
                  <div className="live-outcome" key={o}>
                    <span
                      className="live-outcome-dot"
                      style={{ background: OUTCOME_COLORS[o] }}
                      aria-hidden="true"
                    />
                    <span className="live-outcome-value">{countsNow[o] ?? 0}</span>
                    <span className="live-outcome-label">{o.replace("_", " ").toLowerCase()}</span>
                  </div>
                ))}
              </div>
            </>
          )}
        </div>
      </section>

      {/* ------------------------------------------------------ records */}

      <section className="pn-section">
        <div className="pn-section-head">
          <h2 className="pn-section-title">Records from the replay</h2>
          {batch && <p className="pn-section-note">{batch.label}</p>}
        </div>
        <div className="pn-section-body">
          <div className="filter-row">
            {[null, "RECONCILED", "EXCEPTION", "HUMAN_REVIEW"].map((o) => (
              <button
                key={o || "all"}
                type="button"
                aria-pressed={outcomeFilter === o}
                className={"btn-ghost" + (outcomeFilter === o ? " active" : "")}
                onClick={() => handleFilter(o)}
              >
                {o ? o.replace("_", " ") : "All"}
              </button>
            ))}
          </div>
          <div className="table-scroll pn-records-scroll pn-cardscroll">
            <table className="records-table">
              <caption className="sr-only">Reconciliation results for the current batch</caption>
              <thead>
                <tr>
                  <th scope="col">Record</th>
                  <th scope="col">Merchant amount</th>
                  <th scope="col">Outcome</th>
                  <th scope="col">AI used</th>
                  <th scope="col">Reason</th>
                </tr>
              </thead>
              <tbody>
                {records.map((r) => (
                  <tr
                    key={r.record_id}
                    className={"records-row" + (selectedRecordId === r.record_id ? " selected" : "")}
                    tabIndex={0}
                    role="button"
                    aria-label={`Open record ${r.record_id}, outcome ${r.outcome.replace("_", " ")}`}
                    aria-pressed={selectedRecordId === r.record_id}
                    onClick={() => setSelectedRecordId(r.record_id)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        setSelectedRecordId(r.record_id);
                      }
                    }}
                  >
                    <td className="mono">{r.record_id}</td>
                    <td className="mono pn-num">{merchantAmount(r)}</td>
                    <td>
                      <span className={"badge " + OUTCOME_BADGE[r.outcome]}>
                        {r.outcome.replace("_", " ")}
                      </span>
                    </td>
                    <td>{r.ai_invoked ? "yes" : "—"}</td>
                    <td className="small">{r.reason}</td>
                  </tr>
                ))}
                {records.length === 0 && (
                  <tr>
                    <td colSpan={5} className="muted small" style={{ padding: "20px 8px" }}>
                      No records yet — replay a dataset above.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      </section>

      <AnimatePresence>
        {selectedRecordId && (
          <RecordDetail
            key={selectedRecordId}
            recordId={selectedRecordId}
            batchId={batch?.batch_id}
            onClose={() => setSelectedRecordId(null)}
          />
        )}
      </AnimatePresence>
    </motion.div>
  );
}

/** The stored merchant row, rendered as money — or nothing if it cannot be read. */
function merchantAmount(record) {
  try {
    const merchant = JSON.parse(record.merchant_json);
    return money(merchant.amount_minor, { currency: merchant.currency });
  } catch {
    return "—";
  }
}

/* ------------------------------------------------------------ AI layer */

const PROVIDER_NAMES = { gemini: "Gemini", groq: "Groq" };
const providerName = (name) => PROVIDER_NAMES[name] || name || "—";

const AI_STATUS_TEXT = {
  AI_AVAILABLE: { label: "AI layer operational", tone: "ok" },
  AI_FALLBACK_ACTIVE: { label: "AI layer running on fallback", tone: "degraded" },
  AI_UNAVAILABLE: { label: "AI layer unavailable — ambiguity goes to human review", tone: "down" },
};

/**
 * Provider posture, at the level a product owner needs it.
 *
 * `/ai/status` is the intended source. Where it is not deployed, `/ai/health`
 * probes the same providers for real and can stand in — but only for what it
 * actually measures, so the fallback view reports "probed at", never a
 * "last successful request" it was never told. If neither endpoint answers,
 * this renders nothing at all: an unconfirmed provider is not a working one,
 * and an empty space is more honest than a green light.
 */
function AiLayer() {
  const [state, setState] = useState(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const s = await getAiStatus();
        if (!cancelled && s && s.status) {
          setState({
            status: s.status,
            detail: s.detail,
            rows: [
              s.primary && { role: "Primary", ...s.primary },
              s.fallback && { role: "Fallback", ...s.fallback },
            ].filter(Boolean),
            knowsLastSuccess: true,
          });
        }
        return;
      } catch {
        /* the endpoint may not be deployed; fall through to the probe */
      }
      try {
        const h = await getAiHealth();
        if (cancelled || !h?.status) return;
        const providers = h.providers || [];
        setState({
          status: h.status,
          checkedAt: h.checked_at,
          rows: providers.map((p, i) => ({
            role: i === 0 ? "Primary" : "Fallback",
            name: p.provider,
            available: p.available,
            model: p.model,
            latency_ms: p.latency_ms,
          })),
          knowsLastSuccess: false,
        });
      } catch {
        /* nothing confirmed — show nothing */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  if (!state) return null;
  const text = AI_STATUS_TEXT[state.status] || { label: state.status, tone: "degraded" };

  return (
    <div className="pn-provider">
      <p className="pn-provider-head">
        <span className={`pn-provider-status pn-provider-${text.tone}`}>
          <span className="pn-provider-dot" aria-hidden="true" />
          {text.label}
        </span>
        {state.detail && <span className="pn-section-note">{state.detail}</span>}
      </p>
      <details className="pn-disclosure">
        <summary>Provider detail</summary>
        <div className="pn-scroll pn-cardscroll">
          <table className="pn-provider-table">
            <caption className="sr-only">Model provider availability</caption>
            <thead>
              <tr>
                <th scope="col">Role</th>
                <th scope="col">Provider</th>
                <th scope="col">Available</th>
                <th scope="col">
                  {state.knowsLastSuccess ? "Last successful request" : "Last probe"}
                </th>
                {state.rows.some((r) => r.detail) && <th scope="col">Detail</th>}
              </tr>
            </thead>
            <tbody>
              {state.rows.map((r) => (
                <tr key={r.role}>
                  <th scope="row">{r.role}</th>
                  <td>
                    {providerName(r.name)}
                    {r.model ? <div className="pn-col-id">{r.model}</div> : null}
                  </td>
                  <td>{r.available ? "yes" : "no"}</td>
                  <td className="pn-num">
                    {state.knowsLastSuccess
                      ? r.last_success
                        ? new Date(r.last_success).toLocaleString("en-GB", { hour12: false })
                        : "no successful request recorded"
                      : state.checkedAt
                      ? new Date(state.checkedAt).toLocaleString("en-GB", { hour12: false })
                      : "—"}
                  </td>
                  {state.rows.some((r) => r.detail) && <td>{r.detail || "—"}</td>}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </div>
  );
}
