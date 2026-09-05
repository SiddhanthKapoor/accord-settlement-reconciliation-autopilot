import { AnimatePresence, motion } from "motion/react";
import { useEffect, useRef, useState } from "react";
import { getRecord } from "../api.js";
import { backdrop, expand, slideOverWide } from "../motion.js";
import BreakpointTrace from "./BreakpointTrace.jsx";
import {
  AmountLedger,
  ConfirmedEvidence,
  Hypotheses,
  InvestigationInFlight,
  InvestigationProvenance,
  InvestigationStatus,
  Unresolved,
  isHeuristicBackend,
  readConsidered,
  useInvestigation,
} from "./Investigator.jsx";
import { money } from "./MoneyFlow.jsx";
import "../workspace.css";

const OUTCOME_TONE = { RECONCILED: "allow", EXCEPTION: "block", HUMAN_REVIEW: "warn" };
const OUTCOME_BADGE = { RECONCILED: "badge-pass", EXCEPTION: "badge-fail", HUMAN_REVIEW: "badge-warn" };
const CHECK_TONE = { PASS: "wk-checkline-pass", WARN: "wk-checkline-warn", FAIL: "wk-checkline-fail" };
const CHECK_GLYPH = { PASS: "✓", WARN: "▲", FAIL: "✕" };

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * Focus containment for a slide-over.
 *
 * A panel that covers the page has to behave like a dialog: Escape closes
 * it, Tab cycles inside it rather than wandering into the table
 * underneath, and focus goes back to whatever opened it. Without this a
 * keyboard user tabs into content they cannot see.
 */
export function useDialogFocus(ref, onClose) {
  const returnTo = useRef(null);

  useEffect(() => {
    returnTo.current = document.activeElement;
    const node = ref.current;
    if (node) {
      const first = node.querySelector(FOCUSABLE);
      (first || node).focus({ preventScroll: true });
    }
    return () => {
      const target = returnTo.current;
      if (target && typeof target.focus === "function" && document.contains(target)) {
        target.focus({ preventScroll: true });
      }
    };
  }, [ref]);

  useEffect(() => {
    function onKeyDown(event) {
      if (event.key === "Escape") {
        event.stopPropagation();
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const node = ref.current;
      if (!node) return;
      const items = Array.from(node.querySelectorAll(FOCUSABLE)).filter(
        (el) => el.offsetParent !== null || el === document.activeElement
      );
      if (items.length === 0) {
        event.preventDefault();
        node.focus({ preventScroll: true });
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
    document.addEventListener("keydown", onKeyDown, true);
    return () => document.removeEventListener("keydown", onKeyDown, true);
  }, [ref, onClose]);
}

/**
 * "What happened here?"
 *
 * Four questions, in the order a controller asks them:
 *
 *   1  Evidence            — which rows, in which files, on which lines
 *   2  Deterministic checks — the arithmetic, and what it settled
 *   3  AI involvement      — only where a model was actually consulted
 *   4  Final outcome       — the decision and what to do about it
 *
 * The order is the argument. A reader who stops after (2) has already
 * seen everything that decided most records, which is the point: the
 * model is a late, narrow, gated participant, not the protagonist.
 *
 * Everything on screen is either a field of the stored record or a field
 * of `POST /records/{id}/investigate`. Nothing is computed here except
 * comparisons between two numbers that both came from the API.
 */
export default function RecordDetail({ recordId, batchId, onClose }) {
  const [record, setRecord] = useState(null);
  const [error, setError] = useState(null);
  const panelRef = useRef(null);

  useDialogFocus(panelRef, onClose);

  useEffect(() => {
    setRecord(null);
    setError(null);
    getRecord(recordId, batchId)
      .then(setRecord)
      .catch((e) => setError(e.message));
  }, [recordId, batchId]);

  // The trace, its file-and-row provenance and the ranked explanations are
  // the answer to the question this panel asks, so the request goes out as
  // the panel opens rather than waiting behind a button. The endpoint is
  // the thing that decides whether a model is needed at all — most records
  // come back entirely deterministic.
  const investigation = useInvestigation({ recordId, batchId });
  const inv = investigation.result;

  const considered = readConsidered(record);
  const matched = record?.candidates?.find((c) => c.payment_id === record.matched_payment_id);
  const currency = record?.merchant?.currency || "INR";
  const provenance = parseProvenance(record);
  const tone = record ? OUTCOME_TONE[record.outcome] || "warn" : "warn";

  const aiInvoked = !!record?.ai_invoked;
  // The semantic tier is served either by a model provider or, where none
  // is configured, by Accord's offline verifier. They are not the same
  // claim and must never be worded as if they were.
  const heuristicTier = aiInvoked && isHeuristicBackend(record?.ai_backend);
  const aiCheck = (record?.checks || []).find((c) => fromClassifier(c, record));

  return (
    <>
      <motion.div className="wk-scrim" {...backdrop} onClick={onClose} aria-hidden="true" />
      <motion.div
        className="wk-panel wk-panel-wide"
        role="dialog"
        aria-modal="true"
        aria-labelledby="wk-record-title"
        ref={panelRef}
        tabIndex={-1}
        {...slideOverWide}
      >
        <div className="wk-panel-head">
          <div style={{ minWidth: 0 }}>
            <div className="wk-panel-eyebrow">
              {record ? (
                <span className={"badge " + (OUTCOME_BADGE[record.outcome] || "badge-neutral")}>
                  {record.outcome.replace("_", " ")}
                </span>
              ) : (
                "Record"
              )}
            </div>
            <h2 className="wk-panel-title" id="wk-record-title">
              {record?.merchant?.order_id || recordId}
            </h2>
            <div className="wk-panel-sub">
              {record?.merchant?.reference_id ? `ref ${record.merchant.reference_id} · ` : ""}
              {recordId}
              {batchId ? ` · ${batchId}` : ""}
            </div>
          </div>
          <button type="button" className="wk-panel-close" onClick={onClose} aria-label="Close record">
            <span aria-hidden="true">✕</span>
          </button>
        </div>

        <div className="wk-panel-body">
          {error && (
            <p className="wk-note wk-note-bad" role="alert">
              {error}
            </p>
          )}
          {!record && !error && <p className="wk-sub">Loading the record…</p>}

          {record && (
            <>
              {/* ---------------------------------------------- 1 evidence */}
              <section className="wk-rec-step wk-panelcard" aria-labelledby="wk-step-evidence">
                <div className="wk-rec-step-head">
                  <span className="wk-rec-step-n">01</span>
                  <h3 className="wk-rec-step-title" id="wk-step-evidence">
                    Evidence
                  </h3>
                  <span className="wk-rec-step-aside">
                    <InvestigationStatus
                      state={investigation.state}
                      result={inv}
                      error={investigation.error}
                      elapsed={investigation.elapsed}
                      finishedAt={investigation.finishedAt}
                      onRetry={investigation.run}
                    />
                  </span>
                </div>

                <div className="wk-prov-pair">
                  <div className="wk-prov-card">
                    <div className="wk-prov-side">Ledger side</div>
                    {provenance.ledger ? (
                      <>
                        <div className="wk-prov-file">{provenance.ledger.filename}</div>
                        <div className="wk-prov-row">
                          row {provenance.ledger.file_row ?? provenance.ledger.row}
                          {provenance.ledger.source_type
                            ? ` · ${provenance.ledger.source_type.replace(/_/g, " ").toLowerCase()}`
                            : ""}
                        </div>
                      </>
                    ) : (
                      <p className="wk-prov-missing">
                        This record carries no ledger-side file provenance.
                      </p>
                    )}
                    <div className="wk-prov-lines">
                      <Line label="Order" value={record.merchant.order_id} mono />
                      <Line label="Reference" value={record.merchant.reference_id || "—"} mono />
                      <Line label="Amount" value={money(record.merchant.amount_minor, { currency })} mono />
                      <Line label="Status" value={record.merchant.status} />
                      {record.merchant.description && (
                        <Line label="Description" value={record.merchant.description} />
                      )}
                    </div>
                  </div>

                  <div className="wk-prov-card">
                    <div className="wk-prov-side">
                      Settlement side
                      {record.candidate_count > 1 ? ` · 1 of ${record.candidate_count}` : ""}
                    </div>
                    {provenance.settlement ? (
                      <>
                        <div className="wk-prov-file">{provenance.settlement.filename}</div>
                        <div className="wk-prov-row">
                          row {provenance.settlement.file_row ?? provenance.settlement.row}
                          {provenance.settlement.source_type
                            ? ` · ${provenance.settlement.source_type.replace(/_/g, " ").toLowerCase()}`
                            : ""}
                        </div>
                      </>
                    ) : (
                      <p className="wk-prov-missing">
                        No settlement row was attributed to this record, so there is no
                        settlement-side file or line to cite.
                      </p>
                    )}
                    <div className="wk-prov-lines">
                      {matched ? (
                        <>
                          <Line label="Payment" value={matched.payment_id} mono />
                          <Line label="Gross" value={money(matched.gross_amount_minor, { currency })} mono />
                          <Line
                            label="Fee + tax"
                            value={money((matched.fee_minor || 0) + (matched.tax_minor || 0), { currency })}
                            mono
                          />
                          <Line label="Net" value={money(matched.net_amount_minor, { currency })} mono />
                          <Line
                            label="Settled"
                            value={
                              matched.settlement_date
                                ? new Date(matched.settlement_date).toLocaleDateString()
                                : "—"
                            }
                          />
                        </>
                      ) : record.matched_payment_id ? (
                        <Line label="Matched" value={record.matched_payment_id} mono />
                      ) : (
                        <p className="wk-prov-missing">No settlement record was matched.</p>
                      )}
                    </div>
                  </div>
                </div>

                {/* The money path, straight from the investigator. */}
                <div className="wk-finding">
                  <h4 className="wk-finding-title">Money-flow trace</h4>
                  <div style={{ marginTop: 12 }}>
                    {investigation.state === "running" && (
                      <InvestigationInFlight elapsed={investigation.elapsed} />
                    )}
                    {investigation.state === "error" && (
                      <p className="wk-note wk-note-bad">{investigation.error}</p>
                    )}
                    {inv && (
                      <BreakpointTrace
                        trace={inv.trace}
                        breakpointStage={inv.breakpoint_stage}
                        breakpointKind={inv.breakpoint_kind}
                        currency={currency}
                      />
                    )}
                  </div>
                </div>

                {inv && (
                  <div className="wk-finding">
                    <h4 className="wk-finding-title">
                      Confirmed by the data
                      <span className="wk-finding-count">
                        {(inv.confirmed_evidence || []).length}
                      </span>
                    </h4>
                    <div style={{ marginTop: 10 }}>
                      <ConfirmedEvidence items={inv.confirmed_evidence} />
                    </div>
                  </div>
                )}

                {considered.length > 0 && (
                  <div className="wk-finding">
                    <h4 className="wk-finding-title">
                      Settlements considered
                      <span className="wk-finding-count">{considered.length}</span>
                    </h4>
                    <div className="wk-tablewrap" style={{ marginTop: 10 }}>
                      <table className="wk-table wk-table-dense">
                        <caption className="sr-only">
                          Settlement records considered for {recordId}, and why each was admitted or
                          not.
                        </caption>
                        <thead>
                          <tr>
                            <th scope="col">Payment</th>
                            <th scope="col" className="wk-col-num">Amount</th>
                            <th scope="col">Admitted</th>
                            <th scope="col">Signals</th>
                          </tr>
                        </thead>
                        <tbody>
                          {considered.map((c) => (
                            <tr key={c.payment_id}>
                              <th scope="row" style={{ textAlign: "left", fontWeight: 400, padding: "7px 12px" }}>
                                <span className="wk-inv-name">{c.payment_id}</span>
                                {c.order_reference && (
                                  <span className="wk-inv-side">ref {c.order_reference}</span>
                                )}
                              </th>
                              <td className="wk-col-num">
                                {money(c.gross_amount_minor, { currency })}
                              </td>
                              <td style={{ fontSize: 11.5 }}>
                                <span
                                  className={`wk-checkline ${
                                    c.admissible === false ? "wk-checkline-fail" : "wk-checkline-pass"
                                  }`}
                                >
                                  <span aria-hidden="true">
                                    {c.admissible === false ? "✕" : "✓"}
                                  </span>
                                  {c.admissible === false ? "No" : "Yes"}
                                </span>
                                {c.admissibility_reason && (
                                  <div className="wk-check-detail" style={{ marginTop: 3 }}>
                                    {c.admissibility_reason}
                                  </div>
                                )}
                              </td>
                              <td>
                                <div className="wk-signals">
                                  {(c.supporting_signals || []).map((s, i) => (
                                    <span className="wk-signal wk-signal-for" key={`s${i}`}>
                                      {s}
                                    </span>
                                  ))}
                                  {(c.contradicting_signals || []).map((s, i) => (
                                    <span className="wk-signal wk-signal-against" key={`c${i}`}>
                                      {s}
                                    </span>
                                  ))}
                                  {(c.supporting_signals || []).length === 0 &&
                                    (c.contradicting_signals || []).length === 0 && (
                                      <span className="wk-check-detail">No signals recorded.</span>
                                    )}
                                </div>
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                )}
              </section>

              {/* -------------------------------------- 2 deterministic */}
              <section className="wk-rec-step wk-panelcard" aria-labelledby="wk-step-checks">
                <div className="wk-rec-step-head">
                  <span className="wk-rec-step-n">02</span>
                  <h3 className="wk-rec-step-title" id="wk-step-checks">
                    Deterministic checks
                  </h3>
                  <span className="wk-rec-step-aside">
                    {passFailSummary(record.checks)}
                  </span>
                </div>

                <AmountLedger record={record} />

                <div className="wk-tablewrap" style={{ marginTop: 14 }}>
                  <table className="wk-table wk-table-dense">
                    <caption className="sr-only">Checks run against {recordId}</caption>
                    <thead>
                      <tr>
                        <th scope="col">Check</th>
                        <th scope="col">Result</th>
                        <th scope="col">Expected</th>
                        <th scope="col">Observed</th>
                        <th scope="col">Detail</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(record.checks || []).map((c, i) => {
                        const semantic = fromClassifier(c, record);
                        return (
                          <tr key={i}>
                            <th scope="row" style={{ textAlign: "left", fontWeight: 400, padding: "7px 12px" }}>
                              <span className="wk-check-name">{c.name}</span>
                              {semantic && (
                                <span className="wk-check-from">Semantic classifier</span>
                              )}
                            </th>
                            <td>
                              <span className={`wk-checkline ${CHECK_TONE[c.status] || ""}`}>
                                <span aria-hidden="true">{CHECK_GLYPH[c.status] || "·"}</span>
                                {c.status}
                              </span>
                            </td>
                            <td style={{ fontSize: 11.5, color: "var(--wk-muted)" }}>
                              {c.expected ?? "—"}
                            </td>
                            <td style={{ fontSize: 11.5, color: "var(--wk-muted)" }}>
                              {c.observed ?? "—"}
                            </td>
                            <td className="wk-check-detail">{stripProviderTag(c.detail)}</td>
                          </tr>
                        );
                      })}
                      {(record.checks || []).length === 0 && (
                        <tr>
                          <td colSpan={5} className="wk-table-empty">
                            No checks were recorded against this record.
                          </td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>
              </section>

              {/* ---------------------------------------------- 3 AI */}
              <section className="wk-rec-step wk-panelcard" aria-labelledby="wk-step-ai">
                <div className="wk-rec-step-head">
                  <span className="wk-rec-step-n">03</span>
                  <h3 className="wk-rec-step-title" id="wk-step-ai">
                    AI involvement
                  </h3>
                  <span className="wk-rec-step-aside">
                    {!aiInvoked
                      ? "no model was consulted during the run"
                      : heuristicTier
                      ? "offline verifier during the run · no model call"
                      : "model consulted during the run"}
                  </span>
                </div>

                {aiInvoked ? (
                  <AiPanel
                    record={record}
                    aiCheck={aiCheck}
                    considered={considered}
                    heuristic={heuristicTier}
                  />
                ) : (
                  <div className="wk-ai wk-ai-none">
                    <p className="wk-ai-line">
                      <strong>Resolved deterministically. No model was consulted.</strong> The
                      checks above settled this record on their own, which is the outcome Accord is
                      built to reach — the semantic tier is reached only where the arithmetic leaves
                      a match genuinely undecided.
                    </p>
                  </div>
                )}

                {/* The on-demand investigation is a second, separate question:
                    it may reach for a model even where the run did not, and
                    it may not reach for one even where the run did. */}
                {inv && (inv.ai_used || (inv.hypotheses || []).length > 0) && (
                  <div className="wk-finding">
                    <h4 className="wk-finding-title">
                      Ranked explanation
                      <span className="wk-finding-count">
                        {(inv.hypotheses || []).length} ranked
                      </span>
                    </h4>
                    <div style={{ marginTop: 4 }}>
                      <Hypotheses hypotheses={inv.hypotheses} evidenceIndex={inv.evidence_index} />
                    </div>
                  </div>
                )}
                {inv && <InvestigationProvenance result={inv} />}
              </section>

              {/* ---------------------------------------- 4 final outcome */}
              <section className="wk-rec-step wk-panelcard" aria-labelledby="wk-step-outcome">
                <div className="wk-rec-step-head">
                  <span className="wk-rec-step-n">04</span>
                  <h3 className="wk-rec-step-title" id="wk-step-outcome">
                    Final outcome
                  </h3>
                </div>

                <div className={`wk-final wk-final-${tone}`}>
                  <div className="wk-final-status">
                    <span>{record.outcome.replace("_", " ")}</span>
                    {record.exception_type && (
                      <span>· {record.exception_type.replace(/_/g, " ").toLowerCase()}</span>
                    )}
                    {record.severity && (
                      <span className={`severity severity-${record.severity.toLowerCase()}`}>
                        <span aria-hidden="true" className="severity-dot" />
                        {record.severity.toLowerCase()} priority
                      </span>
                    )}
                  </div>
                  <p className="wk-final-why">{record.explanation || record.reason}</p>
                  {record.recommended_action && (
                    <p className="wk-final-next">
                      <b>Next</b> — {record.recommended_action}
                    </p>
                  )}
                </div>

                {inv && (inv.unresolved || []).length > 0 && (
                  <div className="wk-finding">
                    <h4 className="wk-finding-title">
                      Still unresolved
                      <span className="wk-finding-count">{inv.unresolved.length}</span>
                    </h4>
                    <div style={{ marginTop: 10 }}>
                      <Unresolved items={inv.unresolved} />
                    </div>
                  </div>
                )}

                <TechnicalDetail record={record} batchId={batchId} />
              </section>
            </>
          )}
        </div>
      </motion.div>
    </>
  );
}

/* ------------------------------------------------------------------ AI */

/**
 * The model's part in this record, and only this record.
 *
 * Every figure is a stored field: the provider that answered, the
 * confidence it returned, the policy threshold it was held to, and the
 * classification the engine recorded once it had applied that threshold.
 * The only arithmetic is the comparison between the two numbers, both of
 * which are printed beside it so the reader can do it themselves.
 */
function AiPanel({ record, aiCheck, considered, heuristic = false }) {
  const conf = typeof record.ai_confidence === "number" ? record.ai_confidence : null;
  const threshold = typeof record.policy_threshold === "number" ? record.policy_threshold : null;
  const cls = String(record.classification || "");
  const passedByClass = cls === "SEMANTIC_CONFIRMED" ? true : cls.startsWith("SEMANTIC_") ? false : null;
  const passedByNumber = conf != null && threshold != null ? conf >= threshold : null;
  const passed = passedByClass != null ? passedByClass : passedByNumber;

  const verdicts = considered.filter((c) => c.semantic_verdict);

  return (
    <div className="wk-ai">
      <p className="wk-ai-line">
        The deterministic checks left this match undecided, so it was escalated to the semantic
        tier
        {heuristic
          ? ", which was served by Accord's offline verifier — no model provider was called."
          : ", and a model was asked."}{" "}
        The answer was held to the policy threshold before it was allowed to affect the outcome.
      </p>

      <div className="wk-ai-grid">
        <div>
          <div className="wk-ai-k">{heuristic ? "Answered by" : "Provider that answered"}</div>
          <div className="wk-ai-v wk-ai-v-text">
            {heuristic
              ? "Offline verifier · no model call"
              : record.ai_backend || "not recorded"}
          </div>
        </div>
        <div>
          <div className="wk-ai-k">Its confidence</div>
          <div className="wk-ai-v">{conf != null ? conf.toFixed(2) : "not recorded"}</div>
        </div>
        <div>
          <div className="wk-ai-k">Policy threshold</div>
          <div className="wk-ai-v">{threshold != null ? threshold.toFixed(2) : "not recorded"}</div>
        </div>
        <div>
          <div className="wk-ai-k">Gate</div>
          <div className="wk-ai-v wk-ai-v-text">
            {passed == null ? (
              "not recorded"
            ) : (
              <span className={`wk-ai-gate ${passed ? "wk-ai-gate-pass" : "wk-ai-gate-fail"}`}>
                <span aria-hidden="true">{passed ? "✓" : "▲"}</span>
                {passed ? "Cleared" : "Not cleared"}
              </span>
            )}
          </div>
        </div>
      </div>

      {conf != null && threshold != null && (
        <p className="wk-ai-guard">
          {conf >= threshold
            ? `${conf.toFixed(2)} is at or above the ${threshold.toFixed(
                2
              )} threshold, so the classifier's answer was allowed to stand.`
            : `${conf.toFixed(2)} is below the ${threshold.toFixed(
                2
              )} threshold, so the classifier's answer was not allowed to decide the record — it went to a person instead.`}
          {cls ? ` The engine recorded this as ${cls.replace(/_/g, " ").toLowerCase()}.` : ""}
        </p>
      )}

      {aiCheck && (
        <div className="wk-ai-said">
          <div className="wk-ai-said-label">What it concluded</div>
          <p className="wk-ai-said-body">{stripProviderTag(aiCheck.detail)}</p>
        </div>
      )}

      {verdicts.length > 0 && (
        <div className="wk-ai-said">
          <div className="wk-ai-said-label">Pairings it was asked about</div>
          <ul className="wk-evlist" style={{ marginTop: 8 }}>
            {verdicts.map((c) => (
              <li key={c.payment_id}>
                <b className="mono">{record.merchant?.order_id || record.record_id}</b> against{" "}
                <b className="mono">{c.payment_id}</b> —{" "}
                {String(c.semantic_verdict).toLowerCase()}
                {typeof c.semantic_confidence === "number"
                  ? ` at ${c.semantic_confidence.toFixed(2)} confidence`
                  : ""}
                .
              </li>
            ))}
          </ul>
        </div>
      )}

      <p className="wk-ai-guard">
        The semantic tier can only confirm or reject a pairing the deterministic stage already put
        in front of it. It cannot introduce a settlement that is not in the uploaded files, and it
        cannot book money.
      </p>
    </div>
  );
}

/* ------------------------------------------------------------- details */

function TechnicalDetail({ record, batchId }) {
  const [open, setOpen] = useState(false);
  // An audit chain is keyed on the transaction, not the run, so the same
  // order id processed in an earlier workspace brings its events along.
  // Showing those under this run's record would misattribute them.
  const events = Array.isArray(record.audit_trail) ? record.audit_trail : [];
  const thisRun = events.filter((e) => !e.payload?.batch_id || e.payload.batch_id === batchId);
  const elsewhere = events.length - thisRun.length;

  return (
    <div className="wk-block wk-block-sub" style={{ marginTop: 26 }}>
      <div className="wk-block-head">
        <div className="wk-block-titles">
          <h4 className="wk-h3">Technical detail</h4>
        </div>
        <button
          type="button"
          className="wk-inv-rowlink"
          aria-expanded={open}
          aria-controls="wk-tech-detail"
          onClick={() => setOpen((v) => !v)}
        >
          {open ? "Hide" : "Show"}
        </button>
      </div>

      <AnimatePresence initial={false}>
        {open && (
          <motion.div id="wk-tech-detail" {...expand} style={{ overflow: "hidden" }}>
            <dl className="kv-list">
              <dt>Match classification</dt>
              <dd className="mono tiny">{record.classification || "—"}</dd>
              <dt>Policy threshold</dt>
              <dd className="mono tiny">{record.policy_threshold ?? "—"}</dd>
              <dt>Semantic classifier</dt>
              <dd className="mono tiny">
                {record.ai_invoked
                  ? `${record.ai_backend} · confidence ${
                      record.ai_confidence?.toFixed(2) ?? "n/a"
                    }`
                  : "not invoked — resolved deterministically"}
              </dd>
              <dt>Processing time</dt>
              <dd className="mono tiny">
                {typeof record.latency_ms === "number" ? `${record.latency_ms.toFixed(2)} ms` : "—"}
              </dd>
            </dl>

            <h5 className="wk-eyebrow" style={{ marginTop: 18 }}>
              Audit events in this run
            </h5>
            <ul className="wk-rel" style={{ marginTop: 8 }}>
              {thisRun.map((e) => (
                <li key={e.seq}>
                  <span className="wk-rel-key">#{e.seq}</span>
                  <span style={{ fontWeight: 600 }}>{e.event_type}</span>
                  <span className="wk-rel-key">
                    {new Date(e.timestamp).toLocaleTimeString()}
                  </span>
                </li>
              ))}
              {thisRun.length === 0 && (
                <li>
                  <span className="wk-check-detail">No audit event in this run cites this record.</span>
                </li>
              )}
            </ul>
            {elsewhere > 0 && (
              <p className="wk-note" style={{ marginTop: 10 }}>
                {elsewhere} further audit event{elsewhere === 1 ? "" : "s"} carry this record id but
                belong to other runs, so they are not listed here.
              </p>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

function Line({ label, value, mono }) {
  return (
    <div className="wk-prov-line">
      <span className="wk-prov-line-k">{label}</span>
      <span className={`wk-prov-line-v${mono ? " wk-mono" : ""}`}>{value}</span>
    </div>
  );
}

/* ------------------------------------------------------------ helpers */

/**
 * `provenance_json` names the file and line each side of the match came
 * from. It is the most checkable thing in the record — an operator can
 * open the file and look — so it is read carefully and never faked in.
 */
function parseProvenance(record) {
  if (!record) return {};
  const raw = record.provenance ?? record.provenance_json;
  if (!raw) return {};
  if (typeof raw === "object") return raw;
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

/**
 * A check whose detail is stamped with the provider id came from the
 * classifier, not from arithmetic. The distinction is load-bearing: the
 * checks table must not let a model's opinion pass as a computation.
 */
function fromClassifier(check, record) {
  if (!check || !record?.ai_invoked) return false;
  const detail = String(check.detail || "");
  if (record.ai_backend && detail.startsWith(`[${record.ai_backend}]`)) return true;
  return /^\[[^\]]+]/.test(detail) && typeof check.confidence === "number";
}

/** The `[provider:model]` stamp is metadata; it is shown as a label instead. */
function stripProviderTag(detail) {
  return String(detail || "").replace(/^\[[^\]]+]\s*/, "");
}

function passFailSummary(checks) {
  const list = Array.isArray(checks) ? checks : [];
  if (list.length === 0) return "";
  const pass = list.filter((c) => c.status === "PASS").length;
  const fail = list.filter((c) => c.status === "FAIL").length;
  const warn = list.filter((c) => c.status === "WARN").length;
  const parts = [`${pass} passed`];
  if (warn > 0) parts.push(`${warn} warned`);
  if (fail > 0) parts.push(`${fail} failed`);
  return parts.join(" · ");
}
