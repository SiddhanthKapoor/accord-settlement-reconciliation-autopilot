import { AnimatePresence, motion } from "motion/react";
import { useEffect, useRef, useState } from "react";
import { getDataSources, getLatestBatch, getLatestEvaluation, listBatchRecords, runBatch, streamAudit } from "../api.js";
import RecordDetail from "./RecordDetail.jsx";

const OUTCOME_COLORS = { RECONCILED: "var(--pass)", EXCEPTION: "var(--fail)", HUMAN_REVIEW: "var(--warn)" };
const OUTCOME_BADGE = { RECONCILED: "badge-pass", EXCEPTION: "badge-fail", HUMAN_REVIEW: "badge-warn" };

export default function Console() {
  const [evalReport, setEvalReport] = useState(null);
  const [evalError, setEvalError] = useState(false);
  const [batch, setBatch] = useState(null);
  const [liveCounts, setLiveCounts] = useState({ RECONCILED: 0, EXCEPTION: 0, HUMAN_REVIEW: 0 });
  const [liveProcessed, setLiveProcessed] = useState(0);
  const [running, setRunning] = useState(false);
  const [dataset, setDataset] = useState("holdout");
  const [limit, setLimit] = useState(200);
  const [records, setRecords] = useState([]);
  const [outcomeFilter, setOutcomeFilter] = useState(null);
  const [selectedRecordId, setSelectedRecordId] = useState(null);
  const [dataSources, setDataSources] = useState(null);

  const currentBatchIdRef = useRef(null);

  useEffect(() => {
    getLatestEvaluation("holdout").then(setEvalReport).catch(() => setEvalError(true));
    getDataSources().then(setDataSources).catch(() => {});
    getLatestBatch().then((b) => {
      if (b.batch) {
        setBatch(b.batch);
        currentBatchIdRef.current = b.batch.batch_id;
        loadRecords(b.batch.batch_id, null);
      }
    }).catch(() => {});
  }, []);

  useEffect(() => {
    const stop = streamAudit((event) => {
      const batchId = event.payload?.batch_id;
      if (!batchId || batchId !== currentBatchIdRef.current) return;
      if (event.event_type === "RECORD_DECIDED") {
        setLiveProcessed(event.payload.seq + 1);
        setLiveCounts((prev) => ({ ...prev, [event.payload.outcome]: (prev[event.payload.outcome] || 0) + 1 }));
      }
      if (event.event_type === "BATCH_COMPLETED") {
        setRunning(false);
        getLatestBatch().then((b) => b.batch && setBatch(b.batch)).catch(() => {});
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
    setBatch({ batch_id: result.batch_id, total_records: result.total_records, label: result.label, status: "RUNNING" });
  }

  function handleFilter(outcome) {
    setOutcomeFilter(outcome);
    if (batch?.batch_id) loadRecords(batch.batch_id, outcome);
  }

  const totalForProgress = batch?.total_records || 0;
  const processedNow = running ? liveProcessed : batch?.processed_records ?? 0;
  const countsNow = running ? liveCounts : batch?.outcome_counts ?? {};
  const accuracy = evalReport?.metrics?.reconciliation_accuracy;

  return (
    <div className="page">
      <div className="page-header">
        <h1 className="page-title">Reconciliation Console</h1>
        <p className="page-subtitle">
          Deterministic matching for everything that can be resolved mathematically; Gemini only for
          genuinely ambiguous reference matching, gated by a confidence threshold it can never override.
        </p>
      </div>

      {dataSources && (
        <div className="provenance-banner">
          <span className="provenance-tag">{dataSources.active_source === "SYNTHETIC" ? "Synthetic data" : "Live Razorpay data"}</span>
          <span className="provenance-detail">
            {dataSources.sources.find((s) => s.provenance === "LIVE_RAZORPAY")?.record_count === 0
              ? "Razorpay's live settlements API is connected and returns zero records for this test account, so reconciliation runs on generated data. Accuracy figures are measured against that synthetic set, not real merchant data."
              : dataSources.active_source_reason}
          </span>
        </div>
      )}

      <div className="stats-grid" style={{ marginBottom: 22 }}>
        <div className="stat-tile">
          <div className="stat-label">Records processed</div>
          <div className="stat-value">{batch ? batch.total_records ?? totalForProgress : "—"}</div>
        </div>
        <div className="stat-tile">
          <div className="stat-label">Reconciled</div>
          <div className="stat-value stat-value-pass">{countsNow.RECONCILED ?? 0}</div>
        </div>
        <div className="stat-tile">
          <div className="stat-label">Exceptions</div>
          <div className="stat-value stat-value-fail">{countsNow.EXCEPTION ?? 0}</div>
        </div>
        <div className="stat-tile">
          <div className="stat-label">Human review queue</div>
          <div className="stat-value stat-value-warn">{countsNow.HUMAN_REVIEW ?? 0}</div>
        </div>
        <div className="stat-tile">
          <div className="stat-label">Accuracy (held-out eval)</div>
          <div className="stat-value">{evalError ? "n/a" : accuracy != null ? `${(accuracy * 100).toFixed(1)}%` : "—"}</div>
          <div className="stat-note">{evalError ? "run evaluate.py first" : evalReport ? `${evalReport.record_count} records` : ""}</div>
        </div>
      </div>

      <div className="card" style={{ marginBottom: 22 }}>
        <h2 className="card-title">Batch processing</h2>
        <div className="batch-controls">
          <select className="select-field" value={dataset} onChange={(e) => setDataset(e.target.value)} disabled={running}>
            <option value="holdout">Held-out evaluation set</option>
            <option value="dev">Dev set</option>
          </select>
          <select className="select-field" value={limit} onChange={(e) => setLimit(Number(e.target.value))} disabled={running}>
            <option value={100}>100 records</option>
            <option value={500}>500 records</option>
            <option value={1000}>1,000 records</option>
            <option value={5000}>Full set</option>
          </select>
          <button className="btn-small" onClick={handleRun} disabled={running}>
            {running ? "Processing…" : "Run batch"}
          </button>
          {batch && <span className="tiny muted">{batch.label}</span>}
        </div>

        {(running || batch) && (
          <>
            <div className="progress-track">
              <motion.div
                className="progress-fill"
                animate={{ width: `${totalForProgress ? Math.min(100, (processedNow / totalForProgress) * 100) : 0}%` }}
                transition={{ duration: 0.25 }}
              />
            </div>
            <div className="progress-label">
              <span>{processedNow} / {totalForProgress} processed</span>
              <span>{running ? "streaming live from backend via SSE" : "complete"}</span>
            </div>
            <div className="live-outcome-row">
              {["RECONCILED", "EXCEPTION", "HUMAN_REVIEW"].map((o) => (
                <div className="live-outcome" key={o}>
                  <span className="live-outcome-dot" style={{ background: OUTCOME_COLORS[o] }} />
                  <span className="live-outcome-value">{countsNow[o] ?? 0}</span>
                  <span className="live-outcome-label">{o.replace("_", " ").toLowerCase()}</span>
                </div>
              ))}
            </div>
          </>
        )}
      </div>

      <div className="card">
        <h2 className="card-title">Records {batch ? `(${batch.label})` : ""}</h2>
        <div className="filter-row">
          {[null, "RECONCILED", "EXCEPTION", "HUMAN_REVIEW"].map((o) => (
            <button key={o || "all"} className={"btn-ghost" + (outcomeFilter === o ? " active" : "")} onClick={() => handleFilter(o)}>
              {o ? o.replace("_", " ") : "All"}
            </button>
          ))}
        </div>
        <div className="table-scroll">
        <table className="records-table">
          <caption className="sr-only">Reconciliation results for the current batch</caption>
          <thead>
            <tr>
              <th scope="col">Record</th><th scope="col">Merchant amount</th>
              <th scope="col">Outcome</th><th scope="col">AI used</th><th scope="col">Reason</th>
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
                <td className="mono">₹{(JSON.parse(r.merchant_json).amount_minor / 100).toLocaleString("en-IN")}</td>
                <td><span className={"badge " + OUTCOME_BADGE[r.outcome]}>{r.outcome.replace("_", " ")}</span></td>
                <td>{r.ai_invoked ? "yes" : "—"}</td>
                <td className="small">{r.reason}</td>
              </tr>
            ))}
            {records.length === 0 && (
              <tr><td colSpan={5} className="muted small" style={{ padding: "20px 8px" }}>No records yet — run a batch above.</td></tr>
            )}
          </tbody>
        </table>
        </div>
      </div>

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
    </div>
  );
}
