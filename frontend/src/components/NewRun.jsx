import { AnimatePresence, motion } from "motion/react";
import { useCallback, useRef, useState } from "react";
import {
  createRun, createSampleRun, executeRun, getRunPlan, putRunPlan, removeSource, updateMapping,
  uploadSources,
} from "../api.js";
import { DURATION, EASE, expand, riseIn } from "../motion.js";
import FileInventory, { SOURCE_TYPES, TYPE_LABEL, blockersFor } from "./FileInventory.jsx";
import MoneyFlow, { STAGE_LABEL, STAGE_ORDER, TYPE_STAGES, count } from "./MoneyFlow.jsx";
import "../workspace.css";

/**
 * Normalise an upload response.
 *
 * The ingestion endpoint is being widened while this screen is in use, so
 * both the classified shape and the older unclassified one arrive here.
 * Missing classification is represented as missing — `null` confidence
 * and `needs_confirmation: true` — so the inventory asks the operator.
 * It is never filled in with a plausible default.
 */
function normalise(raw) {
  const detected = raw.detected_source_type ?? raw.detection?.classification?.detected_source_type ?? null;
  const confidence =
    raw.detection_confidence ?? raw.detection?.classification?.detection_confidence ?? null;
  const sourceType = raw.source_type || detected || "";
  const role =
    raw.role ||
    SOURCE_TYPES.find((t) => t.value === sourceType)?.role ||
    "LEDGER";
  const needsConfirmation =
    raw.needs_confirmation ?? (confidence == null ? !raw.source_type : confidence < 0.75);

  return {
    source_id: raw.source_id,
    filename: raw.filename || "upload",
    source_type: sourceType,
    detected_source_type: detected,
    detection_confidence: confidence,
    provider: raw.provider ?? raw.detection?.classification?.provider ?? null,
    row_count: raw.row_count ?? raw.detection?.row_count ?? 0,
    currency: raw.currency ?? null,
    date_range: raw.date_range ?? raw.detection?.classification?.date_range ?? null,
    amount_range: raw.amount_range ?? raw.detection?.classification?.amount_range ?? null,
    suggested_role: raw.suggested_role ?? null,
    duplicate_of: raw.duplicate_of ?? null,
    duplicate_of_filename: raw.duplicate_of_filename ?? null,
    duplicate_ack: false,
    needs_confirmation: !!needsConfirmation,
    confirmed: !!raw.role_confirmed,
    confirmed_by_you: false,
    role,
    mapping: raw.mapping || {},
    detection: raw.detection || { columns: [], guesses: [], unmapped_required: [] },
  };
}

export default function NewRun({ onCreated, onRunStarted }) {
  const [sources, setSources] = useState([]);
  const [busy, setBusy] = useState(false);
  const [sampling, setSampling] = useState(false);
  const [sampleError, setSampleError] = useState(null);
  const [error, setError] = useState(null);
  // Files the backend refused. They are not sources — nothing was stored —
  // so they cannot appear in the inventory, and a joined-up error string
  // buried them. Held per file, with the reason, until dismissed.
  const [rejected, setRejected] = useState([]);
  const [notice, setNotice] = useState("");
  const [dragging, setDragging] = useState(false);
  const [plan, setPlan] = useState(null);
  const [planState, setPlanState] = useState("idle"); // idle | ok | unavailable
  const [planConfirmed, setPlanConfirmed] = useState(false);

  // A ref, not state: two files dropped in quick succession would both
  // read the pre-update state and each create their own run, leaving the
  // sources split across two runs that can never reconcile. Multi-file
  // upload does not change that — every file in a drop, and every later
  // drop, goes against this one run.
  const runRef = useRef(null);
  const dragDepth = useRef(0);
  const inputRef = useRef(null);

  const ensureRun = useCallback(async () => {
    if (runRef.current) return runRef.current;
    const created = await createRun(null);
    runRef.current = created;
    return created;
  }, []);

  const refreshPlan = useCallback(async () => {
    const run = runRef.current;
    if (!run) return;
    try {
      const p = await getRunPlan(run.run_id);
      setPlan(p);
      setPlanState("ok");
      setPlanConfirmed(!!p.confirmed);
      // The plan carries the server's current view of every file — its
      // confidence, whether the role has been confirmed, which required
      // columns are still unmapped. Fold it back in so the inventory can
      // never disagree with the thing that decides whether a run may go.
      const byId = new Map();
      for (const stage of p.stages || []) {
        for (const view of stage.sources || []) byId.set(view.source_id, view);
      }
      const dupes = new Map((p.duplicates || []).map((d) => [d.source_id, d]));
      if (byId.size > 0) {
        setSources((prev) =>
          prev.map((s) => {
            const v = byId.get(s.source_id);
            if (!v) return s;
            const dupe = dupes.get(s.source_id);
            return {
              ...s,
              source_type: v.source_type ?? s.source_type,
              role: v.role ?? s.role,
              row_count: v.row_count ?? s.row_count,
              provider: v.provider ?? s.provider,
              currency: v.currency ?? s.currency,
              date_range: v.date_range ?? s.date_range,
              amount_range: v.amount_range ?? s.amount_range,
              detection_confidence: v.confidence ?? s.detection_confidence,
              needs_confirmation: v.needs_confirmation ?? s.needs_confirmation,
              confirmed: v.role_confirmed ?? s.confirmed,
              duplicate_of: dupe ? dupe.duplicate_of : v.duplicate_of ?? null,
              duplicate_of_filename:
                dupe?.duplicate_of_filename ?? v.duplicate_of_filename ?? s.duplicate_of_filename,
              detection: {
                ...s.detection,
                unmapped_required: v.unmapped_required ?? s.detection.unmapped_required,
              },
            };
          })
        );
      }
    } catch {
      // The plan endpoint is not on every backend build. The workspace
      // still works; the map is simply drawn from the uploaded file types
      // instead, and says so.
      setPlan(null);
      setPlanState("unavailable");
    }
  }, []);

  const addFiles = useCallback(
    async (fileList) => {
      const files = Array.from(fileList || []).filter(Boolean);
      if (files.length === 0) return;
      setBusy(true);
      setError(null);
      setRejected((prev) => prev.filter((r) => !files.some((f) => f.name === r.filename)));
      try {
        const run = await ensureRun();
        const { sources: uploaded, errors } = await uploadSources(run.run_id, files);
        const normalised = uploaded.map(normalise);
        setSources((prev) => [...prev, ...normalised]);
        const needing = normalised.filter((s) => s.needs_confirmation).length;
        setNotice(
          `${normalised.length} file${normalised.length === 1 ? "" : "s"} added, ` +
            `${count(normalised.reduce((a, s) => a + (s.row_count || 0), 0))} rows read` +
            (needing > 0 ? `. ${needing} need${needing === 1 ? "s" : ""} confirmation.` : ".")
        );
        if (errors.length > 0) {
          setRejected((prev) => [
            ...prev.filter((r) => !errors.some((e) => e.filename === r.filename)),
            ...errors.map((e) => ({
              filename: e.filename,
              // `detail` is normalised in api.js from every shape the
              // upload path can report. It is never allowed to be blank:
              // a rejection with no stated reason is the defect.
              detail: e.detail || "could not be read",
            })),
          ]);
        }
        await refreshPlan();
      } catch (e) {
        setError(e.message);
      } finally {
        setBusy(false);
      }
    },
    [ensureRun, refreshPlan]
  );

  /**
   * Load the prepared workspace.
   *
   * One request. The backend reads the demo workspace off its own disk,
   * classifies it there, and hands back the run with its sources — so the
   * files never travel through the browser and nothing about them is
   * decided here. If the route is not on this build, that is reported as
   * what it is; no local substitute is assembled.
   */
  async function loadSample() {
    if (sampling || busy) return;
    setSampling(true);
    setSampleError(null);
    setError(null);
    try {
      let body;
      try {
        body = await createSampleRun();
      } catch (e) {
        if (e.status === 404 || e.status === 405 || e.status === 501) {
          setSampleError(
            "The prepared workspace is not available on this backend build. Upload files to start a run."
          );
          return;
        }
        throw e;
      }

      runRef.current = { run_id: body.run_id || body.batch_id, ...body };
      const list = Array.isArray(body.sources) ? body.sources : [];
      setSources(list.map(normalise));
      setNotice(
        `Prepared workspace loaded: ${count(body.source_count ?? list.length)} sources, ` +
          `${count(body.record_count ?? 0)} rows.`
      );
      await refreshPlan();
    } catch (e) {
      setSampleError(e.message);
    } finally {
      setSampling(false);
    }
  }

  function onDrop(event) {
    event.preventDefault();
    dragDepth.current = 0;
    setDragging(false);
    addFiles(event.dataTransfer?.files);
  }

  /**
   * Answer "what is this file?".
   *
   * The plan endpoint owns `role_confirmed` — the flag the classifier
   * defers to — so a confirmation goes there. Where that endpoint is not
   * present, the older mapping endpoint still records the type, and the
   * confirmation is held locally instead; either way the operator's
   * answer is what the run proceeds on, never a guess.
   */
  async function confirmType(source, sourceType, { confirm }) {
    if (!sourceType) return;
    const role = SOURCE_TYPES.find((t) => t.value === sourceType)?.role || source.role;
    setBusy(true);
    try {
      if (planState === "ok") {
        await putRunPlan(runRef.current.run_id, {
          sources: [{ source_id: source.source_id, source_type: sourceType, role, confirmed: !!confirm }],
        });
      } else {
        await updateMapping(runRef.current.run_id, source.source_id, {
          mapping: source.mapping,
          source_type: sourceType,
        });
      }
      setSources((prev) =>
        prev.map((s) =>
          s.source_id === source.source_id
            ? {
                ...s,
                source_type: sourceType,
                role,
                confirmed: confirm ? true : s.confirmed,
                // Distinct from `confirmed`: the server sets that flag on
                // sources it classified itself, and the inventory must not
                // report those back as the operator's own answer.
                confirmed_by_you: confirm ? true : s.confirmed_by_you,
                duplicate_ack: confirm ? true : s.duplicate_ack,
              }
            : s
        )
      );
      if (confirm) setNotice(`${source.filename} confirmed as ${TYPE_LABEL[sourceType] || sourceType}.`);
      await refreshPlan();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function remap(source, canonical, column) {
    const mapping = { ...source.mapping };
    if (column) mapping[canonical] = column;
    else delete mapping[canonical];
    try {
      const updated = await updateMapping(runRef.current.run_id, source.source_id, {
        mapping,
        source_type: source.source_type || undefined,
      });
      setSources((prev) =>
        prev.map((s) =>
          s.source_id === source.source_id
            ? {
                ...s,
                mapping: updated.mapping,
                detection: { ...s.detection, unmapped_required: updated.unmapped_required },
              }
            : s
        )
      );
      await refreshPlan();
    } catch (e) {
      setError(e.message);
    }
  }

  async function drop(sourceId) {
    const gone = sources.find((s) => s.source_id === sourceId);
    try {
      await removeSource(runRef.current.run_id, sourceId);
      setSources((prev) => prev.filter((s) => s.source_id !== sourceId));
      setNotice(`${gone?.filename || "File"} removed from the workspace.`);
      await refreshPlan();
    } catch (e) {
      setError(e.message);
    }
  }

  async function confirmPlan() {
    try {
      await putRunPlan(runRef.current.run_id, { confirmed: true });
      setPlanConfirmed(true);
      setNotice("Money-flow map confirmed.");
    } catch (e) {
      setError(e.message);
    }
  }

  async function start() {
    const run = runRef.current;
    if (!run) return;
    setBusy(true);
    setError(null);
    try {
      const result = await executeRun(run.run_id, null);
      (onCreated || onRunStarted)?.(run.run_id, result);
    } catch (e) {
      setError(e.message);
      setBusy(false);
    }
  }

  // ---- readiness -------------------------------------------------------
  // The backend decides whether a run may proceed. Where it publishes that
  // decision, this screen reports it verbatim rather than re-deriving a
  // second opinion that could disagree with the thing actually enforcing
  // the gate. The local rules below are the fallback for builds that do
  // not publish a plan.
  const planBlockers = Array.isArray(plan?.blocking)
    ? plan.blocking.map((b) => (b.filename ? `${b.filename}: ${b.detail}` : capitalise(b.detail)))
    : null;

  let blockers;
  if (planBlockers) {
    blockers = planBlockers;
    if (sources.length === 0) blockers = ["Upload at least one file."];
  } else {
    blockers = sources.flatMap(blockersFor);
    const hasLedger = sources.some((s) => s.role === "LEDGER");
    const hasSettlement = sources.some((s) => s.role === "SETTLEMENT");
    if (sources.length === 0) blockers.push("Upload at least one file.");
    else if (!hasLedger) blockers.push("No ledger-side source yet — add orders, invoices or an accounting export.");
    else if (!hasSettlement) blockers.push("No settlement-side source yet — add a gateway payout file or a bank statement.");
  }

  // Which side of the reconciliation is missing, preferring the backend's
  // own answer. This is the single most likely first-minute failure — one
  // bank statement, nothing to compare it to — and it has to be explained
  // before the run button, not raised as an HTTP 400 after it.
  const planKinds = Array.isArray(plan?.blocking) ? plan.blocking.map((b) => b.kind) : null;
  const hasLedgerSide = sources.some((s) => s.role === "LEDGER");
  const hasSettlementSide = sources.some((s) => s.role === "SETTLEMENT");
  const missingSide = sources.length === 0
    ? null
    : planKinds
    ? planKinds.includes("NO_LEDGER_SIDE")
      ? "LEDGER"
      : planKinds.includes("NO_SETTLEMENT_SIDE")
      ? "SETTLEMENT"
      : null
    : !hasLedgerSide
    ? "LEDGER"
    : !hasSettlementSide
    ? "SETTLEMENT"
    : null;

  const canRun = blockers.length === 0 && !busy && (planState !== "ok" || plan?.can_execute !== false);
  const totalRows = plan?.total_records ?? sources.reduce((a, s) => a + (s.row_count || 0), 0);
  const categories = plan?.source_type_counts
    ? Object.keys(plan.source_type_counts).length
    : new Set(sources.map((s) => s.source_type).filter(Boolean)).size;
  const ledgerFiles = plan?.role_counts?.LEDGER ?? sources.filter((s) => s.role === "LEDGER").length;
  const settlementFiles =
    plan?.role_counts?.SETTLEMENT ?? sources.filter((s) => s.role === "SETTLEMENT").length;

  const stages = buildStages(plan, sources);
  const hasFiles = sources.length > 0;

  return (
    <div className="page">
      <header className="wk-hd">
        <h1 className="wk-hd-title">Add your financial sources</h1>
        <p className="wk-hd-sub">
          Upload bank statements, gateway settlements, UPI reports, ledgers, orders, invoices and
          other financial exports. Accord will inspect each source and reconcile the workspace as a
          whole.
        </p>
      </header>

      <div className="sr-only" role="status" aria-live="polite">{notice}</div>

      {error && (
        <motion.p className="wk-note wk-note-bad" role="alert" {...riseIn} style={{ marginBottom: 16 }}>
          {error}
        </motion.p>
      )}

      <div className={hasFiles ? undefined : "wk-entry"}>
        <motion.div
          {...riseIn}
          className={`wk-dropzone${dragging ? " wk-dropzone-active" : ""}${
            hasFiles ? " wk-dropzone-compact" : ""
          }`}
          onDragEnter={(e) => {
            e.preventDefault();
            dragDepth.current += 1;
            setDragging(true);
          }}
          onDragOver={(e) => e.preventDefault()}
          onDragLeave={() => {
            dragDepth.current = Math.max(0, dragDepth.current - 1);
            if (dragDepth.current === 0) setDragging(false);
          }}
          onDrop={onDrop}
        >
          <svg className="wk-dropzone-icon" viewBox="0 0 32 32" fill="none" aria-hidden="true">
            <path d="M16 22V7m0 0l-6 6m6-6l6 6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
            <path d="M4 21v3a3 3 0 003 3h18a3 3 0 003-3v-3" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
          </svg>
          <div>
            <div className="wk-dropzone-title">
              {hasFiles ? "Add more sources" : "Drop your files here"}
            </div>
            <div className="wk-dropzone-sub">
              CSV and XLSX · one file or fifty · you do not have to say what any of them are
            </div>
          </div>
          <div className="wk-dropzone-cta">
            <input
              ref={inputRef}
              id="wk-file-input"
              className="wk-file-input"
              type="file"
              multiple
              accept=".csv,.xlsx,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
              onChange={(e) => {
                addFiles(e.target.files);
                e.target.value = "";
              }}
            />
            <label htmlFor="wk-file-input" className="wk-file-label">
              <span aria-hidden="true">+</span> Choose files
            </label>
          </div>
          <p className="wk-dropzone-note">
            Accord reads every file, classifies it, and asks you only where the evidence is thin.
          </p>
        </motion.div>

        {!hasFiles && (
          <motion.button
            type="button"
            className="wk-sample"
            onClick={loadSample}
            disabled={sampling}
            {...riseIn}
          >
            <span className="wk-sample-eyebrow">No files to hand?</span>
            <span className="wk-sample-title">Load sample workspace</span>
            {/* Deliberately no file or record counts: the prepared
                workspace is generated and its size changes. A number here
                would be a claim this button cannot check. */}
            <span className="wk-sample-sub">
              Explore a realistic reconciliation scenario — orders, invoices, bank statements and
              gateway payout exports from several providers, reconciled together.
            </span>
            <span className="wk-sample-go">
              {sampling ? "Loading…" : "Load it"} <span aria-hidden="true">→</span>
            </span>
            {sampleError && <span className="wk-sample-unavailable">{sampleError}</span>}
          </motion.button>
        )}
      </div>

      {!hasFiles && sampleError && (
        <p className="sr-only" role="alert">
          {sampleError}
        </p>
      )}

      {rejected.length > 0 && (
        <div className="wk-onesided wk-rejected" role="alert" style={{ marginTop: 20 }}>
          <p className="wk-onesided-title">
            {rejected.length === 1
              ? "One file was not added to this workspace"
              : `${rejected.length} files were not added to this workspace`}
          </p>
          <ul className="wk-onesided-list">
            {rejected.map((r) => (
              <li key={r.filename}>
                <strong>{r.filename}</strong> — {r.detail}
              </li>
            ))}
          </ul>
          <p className="wk-onesided-have">
            Nothing from {rejected.length === 1 ? "this file" : "these files"} was stored, so
            {rejected.length === 1 ? " it is" : " they are"} not in the inventory below. Accord reads
            CSV and XLSX exports with a header row and at least one row of data.{" "}
            <button type="button" className="wk-inv-rowlink" onClick={() => setRejected([])}>
              Dismiss
            </button>
          </p>
        </div>
      )}

      <AnimatePresence initial={false}>
        {hasFiles && (
          <motion.div {...expand} style={{ overflow: "hidden" }}>
            <section className="wk-block" aria-labelledby="wk-inventory-heading">
              <div className="wk-block-head">
                <div className="wk-block-titles">
                  <h2 className="wk-h2" id="wk-inventory-heading">
                    Sources in this workspace
                  </h2>
                  <p className="wk-sub">
                    What Accord read from each file, and how confident it is about what the file is.
                  </p>
                </div>
                <div className="wk-summary">
                  <div className="wk-summary-item">
                    <div className="wk-summary-value">{count(sources.length)}</div>
                    <div className="wk-summary-label">Files</div>
                  </div>
                  <div className="wk-summary-item">
                    <div className="wk-summary-value">{count(categories)}</div>
                    <div className="wk-summary-label">Kinds</div>
                  </div>
                  <div className="wk-summary-item">
                    <div className="wk-summary-value">{count(totalRows)}</div>
                    <div className="wk-summary-label">Rows read</div>
                  </div>
                </div>
              </div>

              <div className="wk-surface">
                <FileInventory
                sources={sources}
                onConfirm={confirmType}
                onRemove={drop}
                onRemap={remap}
                busy={busy}
                />
              </div>
            </section>

            <section className="wk-block" aria-labelledby="wk-plan-heading">
              <div className="wk-block-head">
                <div className="wk-block-titles">
                  <h2 className="wk-h2" id="wk-plan-heading">
                    Proposed money-flow map
                  </h2>
                  <p className="wk-sub">
                    {planState === "ok"
                      ? `Accord will reconcile ${count(ledgerFiles)} ledger-side file${
                          ledgerFiles === 1 ? "" : "s"
                        } against ${count(settlementFiles)} settlement-side file${
                          settlementFiles === 1 ? "" : "s"
                        } as two pooled populations. Confirm the map, or add a file for a stage nothing covers.`
                      : "Derived from the file types in this workspace. This backend build does not return a stored money-flow plan, so nothing here has been confirmed server-side."}
                  </p>
                </div>
                {planState === "ok" && (
                  <div className="wk-block-actions">
                    <button type="button" className="btn-small" onClick={confirmPlan} disabled={planConfirmed}>
                      {planConfirmed ? "Map confirmed" : "Confirm map"}
                    </button>
                  </div>
                )}
              </div>

              <MoneyFlow stages={stages} ariaLabel="Proposed money flow across stages" />

              {plan?.engine_note && (
                <p className="wk-note" style={{ marginTop: 14 }}>{plan.engine_note}</p>
              )}

              {Array.isArray(plan?.relationships) && plan.relationships.length > 0 && (
                <Relationships
                  relationships={plan.relationships}
                  truncated={plan.relationships_truncated}
                />
              )}
            </section>

            {missingSide && <OneSided side={missingSide} sources={sources} />}

            <div className="wk-runbar">
              <div style={{ minWidth: 0 }}>
                {blockers.length > 0 ? (
                  <div className="wk-blockers" id="wk-run-blockers">
                    {blockers.slice(0, 4).map((b, i) => (
                      <span className="wk-blocker" key={i}>
                        <span className="wk-blocker-glyph" aria-hidden="true">▲</span>
                        {b}
                      </span>
                    ))}
                    {blockers.length > 4 && (
                      <span className="wk-blocker">
                        <span className="wk-blocker-glyph" aria-hidden="true">▲</span>
                        and {blockers.length - 4} more.
                      </span>
                    )}
                  </div>
                ) : (
                  <p className="wk-run-reason">
                    <strong>{count(sources.length)}</strong> files ·{" "}
                    <strong>{count(categories)}</strong> kinds ·{" "}
                    <strong>{count(totalRows)}</strong> rows ready to reconcile.
                  </p>
                )}
              </div>
              <motion.button
                type="button"
                className="wk-btn-run"
                disabled={!canRun}
                onClick={start}
                aria-describedby={blockers.length > 0 ? "wk-run-blockers" : undefined}
                whileHover={canRun ? { y: -1 } : undefined}
                whileTap={canRun ? { scale: 0.985 } : undefined}
                transition={{ duration: DURATION.instant, ease: EASE }}
              >
                {busy
                  ? "Working…"
                  : blockers.length > 0
                  ? blockedLabel(blockers)
                  : "Run reconciliation"}
              </motion.button>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}


/**
 * The two sides of a reconciliation, in the words a finance person uses.
 *
 * Named examples matter more than the abstraction: "add a ledger-side
 * source" tells someone holding one bank statement nothing, and "an
 * orders export, an invoice ledger or an accounting export" tells them
 * exactly what to go and find.
 */
const SIDE_GUIDANCE = {
  LEDGER: {
    missingLabel: "ledger side",
    presentLabel: "settlement side",
    missingMeans: "what your books say should have happened",
    presentMeans: "what actually settled",
    examples: [
      "an orders export — Shopify, Amazon, your own storefront",
      "an invoice ledger",
      "an accounting or ERP export — Tally, Zoho Books, QuickBooks",
    ],
  },
  SETTLEMENT: {
    missingLabel: "settlement side",
    presentLabel: "ledger side",
    missingMeans: "what actually settled",
    presentMeans: "what your books say should have happened",
    examples: [
      "a payment gateway settlement or payout report",
      "a bank statement",
      "a UPI or card settlement report",
    ],
  },
};

/**
 * A workspace with only one side.
 *
 * Reconciliation is a comparison, so one side is not a small version of a
 * run — it is not a run at all. The backend refuses it with a 400, which
 * reaches an operator as a raw error after they have already clicked. So
 * the refusal is explained here, before the button: what reconciliation
 * needs, what is actually in the workspace, and the kind of file to add.
 */
function OneSided({ side, sources }) {
  const guide = SIDE_GUIDANCE[side];
  const present = sources.filter((s) => s.role !== side);
  return (
    <div className="wk-onesided" role="status">
      <p className="wk-onesided-title">
        Reconciliation needs two sides — this workspace has only one.
      </p>
      <p className="wk-onesided-body">
        Accord reconciles <strong>{guide.presentMeans}</strong> against{" "}
        <strong>{guide.missingMeans}</strong>. Everything here is on the{" "}
        <strong>{guide.presentLabel}</strong>, so there is nothing to compare it against and no
        record can be decided either way.
      </p>
      <p className="wk-onesided-body" style={{ marginTop: 10 }}>
        Add at least one file for the {guide.missingLabel}:
      </p>
      <ul className="wk-onesided-list">
        {guide.examples.map((e) => (
          <li key={e}>{e}</li>
        ))}
      </ul>
      {present.length > 0 && (
        <p className="wk-onesided-have">
          In this workspace now:{" "}
          {present
            .slice(0, 6)
            .map((s) => `${s.filename} (${TYPE_LABEL[s.source_type] || s.source_type || "unclassified"}, ${count(s.row_count || 0)} rows)`)
            .join(" · ")}
          {present.length > 6 ? ` and ${present.length - 6} more` : ""}.
        </p>
      )}
    </div>
  );
}

/**
 * Proposed links between files.
 *
 * Two kinds arrive, and conflating them buries the useful one. A pair
 * with shared identifier values is real evidence that the files describe
 * the same money. A pair that is merely adjacent in the money path is a
 * placeholder — the plan says so explicitly ("a sample, not proof they
 * are unrelated") — and there are O(n²) of those, enough to bury the
 * handful that matter. So the evidenced pairs are shown and the adjacent
 * ones are counted and kept one click away. Nothing is dropped.
 */
function Relationships({ relationships, truncated }) {
  const observed = relationships.filter((r) => r.strength !== "ADJACENCY_ONLY");
  const adjacent = relationships.filter((r) => r.strength === "ADJACENCY_ONLY");

  return (
    <div className="wk-block wk-block-sub" style={{ marginTop: 26 }}>
      <div className="wk-block-head">
        <div className="wk-block-titles">
          <h3 className="wk-h3">Relationships Accord proposes</h3>
        </div>
        <span className="wk-count-line">
          {count(observed.length)} with shared identifiers
        </span>
      </div>
      {observed.length === 0 ? (
        <p className="wk-sub">
          No pair of files shared an identifier value in the rows sampled. That is a sample, not
          proof they are unrelated.
        </p>
      ) : (
        <ul className="wk-rel">
          {observed.map((r) => (
            <RelationshipRow key={r.relationship_id} r={r} />
          ))}
        </ul>
      )}

      {adjacent.length > 0 && (
        <details className="disclosure" style={{ marginTop: 10 }}>
          <summary className="disclosure-summary">
            {count(adjacent.length)} further pairs are adjacent stages with no shared identifier
            observed
          </summary>
          <ul className="wk-rel" style={{ marginTop: 9 }}>
            {adjacent.map((r) => (
              <RelationshipRow key={r.relationship_id} r={r} />
            ))}
          </ul>
        </details>
      )}

      {truncated && (
        <p className="wk-sub" style={{ marginTop: 8 }}>
          The list of proposed pairs was truncated by the backend; more exist than are shown here.
        </p>
      )}
    </div>
  );
}

function RelationshipRow({ r }) {
  return (
    <li>
      <span style={{ fontWeight: 600 }}>{r.label}</span>
      <span className="wk-rel-key">
        {STAGE_LABEL[r.from_stage] || r.from_stage} → {STAGE_LABEL[r.to_stage] || r.to_stage}
      </span>
      {r.shared_identifier_count > 0 && (
        <span className="wk-rel-key">
          {count(r.shared_identifier_count)} shared identifier
          {r.shared_identifier_count === 1 ? "" : "s"}
        </span>
      )}
      {r.basis && (
        <span className="wk-sub" style={{ width: "100%", marginTop: 2 }}>
          {r.basis}
        </span>
      )}
    </li>
  );
}

/**
 * The button says what is stopping it.
 *
 * A disabled control with no stated reason is the most common way a
 * product loses an operator's trust — they conclude it is broken. The
 * refusal is the product working, so it goes on the button itself.
 */
function blockedLabel(blockers) {
  if (blockers.length === 1) {
    const only = blockers[0];
    if (only.includes("not mapped") || only.includes("unmapped")) return "Blocked · map required columns";
    if (only.includes("confirm")) return "Blocked · confirm the source type";
    if (only.includes("duplicate")) return "Blocked · resolve the duplicate";
    // The plan endpoint says "ledger side" and the local fallback says
    // "ledger-side". Matching only the hyphenated form meant the single
    // most common refusal — a one-sided workspace — fell through to the
    // useless "Blocked · 1 thing to resolve".
    if (/ledger.side/.test(only)) return "Blocked · add a ledger source";
    if (/settlement.side/.test(only)) return "Blocked · add a settlement source";
    if (/no rows/.test(only)) return "Blocked · a file has no rows";
    if (/no amount|no date/.test(only)) return "Blocked · a file has no amount or date";
    if (only.includes("at least one file")) return "Upload a file to begin";
    return "Blocked · 1 thing to resolve";
  }
  return `Blocked · ${blockers.length} things to resolve`;
}

/**
 * Stage descriptors for the flow strip.
 *
 * Prefers whatever the backend's plan says. Failing that, coverage is
 * read off the file types actually uploaded — which is a fact about the
 * workspace, not an estimate. Either way, a stage with nothing behind it
 * is marked not evaluated rather than scored.
 */
function buildStages(plan, sources) {
  // The plan speaks its own stage vocabulary (ORDERS, PAYMENT_GATEWAY,
  // SETTLEMENT, BANK, ACCOUNTING) and supplies the label for each. Use it
  // verbatim rather than translating into a second set of names — a stage
  // the operator sees here should be the stage the backend is reasoning
  // about.
  if (Array.isArray(plan?.stages) && plan.stages.length > 0) {
    return plan.stages.map((stage) => {
      const files = (stage.sources || []).map((v) => v.filename).filter(Boolean);
      return {
        key: stage.stage,
        label: stage.label || STAGE_LABEL[stage.stage] || stage.stage,
        evaluated: !!stage.present,
        headline: count(stage.record_count ?? 0),
        note:
          files.length > 0
            ? `rows from ${files.length === 1 ? files[0] : `${files.length} files`}`
            : "",
        selectable: false,
      };
    });
  }

  return STAGE_ORDER.map((key) => {
    const covering = sources.filter((s) => (TYPE_STAGES[s.source_type] || []).includes(key));
    const rows = covering.reduce((a, s) => a + (s.row_count || 0), 0);
    return {
      key,
      label: STAGE_LABEL[key],
      evaluated: covering.length > 0,
      headline: count(rows),
      note:
        covering.length > 0
          ? `rows from ${covering.length === 1 ? covering[0].filename : `${covering.length} files`}`
          : "",
    };
  });
}

function capitalise(text) {
  const t = String(text || "");
  return t.charAt(0).toUpperCase() + t.slice(1);
}
