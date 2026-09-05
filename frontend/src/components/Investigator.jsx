import { AnimatePresence, motion } from "motion/react";
import { useCallback, useEffect, useRef, useState } from "react";
import { investigateRecord } from "../api.js";
import { DURATION, EASE, expand, listIndexDelay, riseIn } from "../motion.js";
import BreakpointTrace from "./BreakpointTrace.jsx";
import { money } from "./MoneyFlow.jsx";
import "../workspace.css";

/**
 * Deterministic amount breakdown for an aggregated settlement.
 *
 * Built only from values the record actually carries. Every total shown
 * is arithmetic over rows that are themselves on screen, so the reader
 * can check it; where a component (a fee, a net) is absent from the data,
 * the total that would depend on it is not shown at all rather than
 * quietly treated as zero. That rule is the whole point of this panel —
 * a plausible-looking number nobody can trace is worse than a gap.
 */
export function AmountLedger({ record }) {
  if (!record) return null;
  const currency = record.merchant?.currency || "INR";
  const booked = record.merchant?.amount_minor;

  const considered = candidatesOf(record);
  const admitted = considered.filter((c) => c.admissible !== false);
  // Only admitted candidates are summed. Adding up settlements the engine
  // has already ruled out would produce a total that is arithmetically
  // correct and financially meaningless, and it would be the number the
  // reader remembers. Where nothing is admitted, the candidates are still
  // listed — as candidates — and no total is drawn at all.
  const rows = admitted.length > 0 ? admitted : considered;
  const summable = admitted.length > 0;
  if (rows.length === 0) return null;

  const sum = (key) => {
    if (!summable) return null;
    const values = rows.map((r) => r[key]);
    if (values.some((v) => v == null)) return null;
    return values.reduce((a, b) => a + Number(b), 0);
  };

  const gross = sum("gross_amount_minor");
  const fee = sum("fee_minor");
  const tax = sum("tax_minor");
  const net = sum("net_amount_minor");
  const settled = net != null ? net : gross != null && fee != null && tax != null ? gross - fee - tax : null;
  const difference = settled != null && booked != null ? settled - booked : null;

  return (
    <div className="wk-ledger">
      <div className="wk-ledger-head">
        <span className="wk-ledger-head-label">
          {summable
            ? rows.length === 1
              ? "Settlement credit"
              : `Settlement credit · ${rows.length} payments`
            : `Candidates considered · ${rows.length} none admitted`}
        </span>
        <span className="wk-ledger-head-value">
          {summable ? (settled != null ? money(settled, { currency }) : "not derivable") : "—"}
        </span>
      </div>
      <ul className="wk-ledger-rows">
        {rows.map((c) => (
          <li className="wk-ledger-row" key={c.payment_id}>
            <span className="wk-ledger-row-desc">
              <b className="mono">{c.payment_id}</b>
              {c.order_reference ? <> · ref {c.order_reference}</> : null}
              {c.admissible === false && c.admissibility_reason ? <> · not admitted: {c.admissibility_reason}</> : null}
            </span>
            <span className="wk-ledger-amount">{money(c.gross_amount_minor, { currency })}</span>
          </li>
        ))}
        {summable && rows.length > 1 && (
          <li className="wk-ledger-row wk-ledger-sub">
            <span className="wk-ledger-row-desc"><b>Gross total</b></span>
            <span className="wk-ledger-amount">{gross != null ? money(gross, { currency }) : "—"}</span>
          </li>
        )}
        {summable && (
          <>
        <li className="wk-ledger-row">
          <span className="wk-ledger-row-desc">Less gateway fee</span>
          <span className="wk-ledger-amount">{fee != null ? `−${money(fee, { currency })}` : "—"}</span>
        </li>
        <li className="wk-ledger-row">
          <span className="wk-ledger-row-desc">Less tax on fee</span>
          <span className="wk-ledger-amount">{tax != null ? `−${money(tax, { currency })}` : "—"}</span>
        </li>
        <li className="wk-ledger-row wk-ledger-total">
          <span className="wk-ledger-row-desc">Net settled</span>
          <span className="wk-ledger-amount">{settled != null ? money(settled, { currency }) : "—"}</span>
        </li>
          </>
        )}
        <li className="wk-ledger-row">
          <span className="wk-ledger-row-desc">Booked in the ledger</span>
          <span className="wk-ledger-amount">{money(booked, { currency })}</span>
        </li>
        {summable && (
        <li className="wk-ledger-row wk-ledger-total">
          <span className="wk-ledger-row-desc">Difference</span>
          <span
            className={`wk-ledger-amount ${
              difference == null ? "" : difference === 0 ? "wk-ledger-delta-ok" : "wk-ledger-delta-bad"
            }`}
          >
            {difference == null
              ? "not derivable"
              : difference === 0
              ? `${money(0, { currency })} · matches`
              : `${difference > 0 ? "+" : "−"}${money(Math.abs(difference), { currency })}`}
          </span>
        </li>
        )}
        {!summable && (
          <li className="wk-ledger-row">
            <span className="wk-ledger-row-desc">
              None of these settlements was admitted against this order, so there is nothing to
              total. The reasons are on each row.
            </span>
          </li>
        )}
      </ul>
      {(record.explanation || record.reason) && (
        <p className="wk-ledger-finding">
          <strong>Finding · </strong>
          {record.explanation || record.reason}
        </p>
      )}
    </div>
  );
}

function candidatesOf(record) {
  const considered = readConsidered(record);
  if (considered.length === 0) return Array.isArray(record.candidates) ? record.candidates : [];

  // The considered-candidates list carries the admissibility reasoning but
  // only a gross amount; the raw settlement list carries the fee, tax and
  // net. They describe the same payments, so joining them on payment_id
  // gives a breakdown that can actually be totalled. Both sides come from
  // the same API response — nothing is filled in from elsewhere.
  const settlements = new Map(
    (Array.isArray(record.candidates) ? record.candidates : []).map((c) => [c.payment_id, c])
  );
  return considered.map((c) => {
    const s = settlements.get(c.payment_id);
    if (!s) return c;
    return {
      ...c,
      gross_amount_minor: c.gross_amount_minor ?? s.gross_amount_minor,
      fee_minor: c.fee_minor ?? s.fee_minor,
      tax_minor: c.tax_minor ?? s.tax_minor,
      net_amount_minor: c.net_amount_minor ?? s.net_amount_minor,
    };
  });
}

/**
 * `GET /records/{id}` returns this column unparsed; the review queue
 * hydrates it. Read both, so the breakdown appears wherever it is opened.
 */
/**
 * Resolve a hypothesis's evidence keys against the investigation's own
 * index, so a reader sees the sentence rather than the token. A key with
 * no entry is dropped: showing "CANDIDATE_1" to an operator is noise, and
 * inventing a sentence for it would be worse.
 */
function hypothesisEvidence(hypothesis, index) {
  if (Array.isArray(hypothesis.evidence) && hypothesis.evidence.length > 0) return hypothesis.evidence;
  if (!Array.isArray(hypothesis.evidence_keys) || !index) return [];
  return hypothesis.evidence_keys.map((k) => index[k]).filter(Boolean);
}

function readConsidered(record) {
  if (Array.isArray(record.considered_candidates) && record.considered_candidates.length > 0) {
    return record.considered_candidates;
  }
  if (typeof record.considered_json === "string") {
    try {
      const parsed = JSON.parse(record.considered_json);
      if (Array.isArray(parsed)) return parsed;
    } catch {
      /* unparseable column — show the raw settlement list instead */
    }
  }
  return [];
}

/**
 * How the provider behaved on this record.
 *
 * Four distinct outcomes, and the distinction that matters most is
 * between "the model was not needed" and "the model could not be
 * reached". The first is the product working as designed — deterministic
 * evidence settled the record and no call was made — and reporting it as
 * a degradation would be a false statement about system health. Only
 * AI_UNAVAILABLE is an actual problem.
 *
 * An unrecognised value returns null and nothing is rendered, rather than
 * a guess dressed up as a status.
 */
const AI_STATUS = {
  AI_AVAILABLE: { text: "Provider healthy.", tone: "on" },
  AI_FALLBACK_ACTIVE: { text: "AI fallback active — the secondary provider answered.", tone: "warn" },
  AI_UNAVAILABLE: { text: "AI unavailable — the model was needed and could not be reached.", tone: "warn" },
  AI_NOT_CONSULTED: { text: "Not needed — resolved deterministically.", tone: "on" },
};

/** Engine labels arrive as constants; operators read sentences. */
function humaniseLabel(label) {
  const text = String(label || "").replace(/_/g, " ").toLowerCase();
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/**
 * Explicit AI investigation of one exception.
 *
 * Not automatic and not a chat. An operator asks for it on a record they
 * are looking at, the request is made once, and the answer is rendered in
 * three separated registers so that a deterministic fact is never read as
 * a model's opinion:
 *
 *   CONFIRMED EVIDENCE — computed from the uploaded data
 *   LIKELY EXPLANATION — ranked hypotheses, each with its confidence
 *   UNRESOLVED         — what the data cannot settle either way
 *
 * The progress state is tied to the real request: elapsed time from the
 * moment fetch was called, and an indeterminate bar that makes no claim
 * about how far along the work is, because the backend does not tell us.
 */
export default function Investigator({ recordId, batchId, record, autoFocus = false }) {
  const [state, setState] = useState("idle"); // idle | running | done | error
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [elapsed, setElapsed] = useState(0);
  const startedRef = useRef(0);
  const buttonRef = useRef(null);
  const resultRef = useRef(null);

  useEffect(() => {
    setState("idle");
    setResult(null);
    setError(null);
  }, [recordId, batchId]);

  useEffect(() => {
    if (state !== "running") return undefined;
    const id = setInterval(() => setElapsed((Date.now() - startedRef.current) / 1000), 100);
    return () => clearInterval(id);
  }, [state]);

  useEffect(() => {
    if (autoFocus && buttonRef.current) buttonRef.current.focus();
  }, [autoFocus]);

  const run = useCallback(async () => {
    startedRef.current = Date.now();
    setElapsed(0);
    setState("running");
    setError(null);
    try {
      const data = await investigateRecord(recordId, batchId);
      setResult(data);
      setState("done");
    } catch (e) {
      setError(
        e.status === 404
          ? "The investigation endpoint is not available on this backend build."
          : e.message
      );
      setState("error");
    }
  }, [recordId, batchId]);

  useEffect(() => {
    if (state === "done" && resultRef.current) resultRef.current.focus();
  }, [state]);

  const hypotheses = Array.isArray(result?.hypotheses) ? result.hypotheses : [];
  const confirmed = Array.isArray(result?.confirmed_evidence) ? result.confirmed_evidence : [];
  const unresolved = Array.isArray(result?.unresolved) ? result.unresolved : [];

  return (
    <section className="wk-section" aria-labelledby="wk-investigator-heading">
      <div className="wk-section-head">
        <div>
          <h3 className="wk-h2" id="wk-investigator-heading">Investigation</h3>
          <p className="wk-inv-blurb">
            Trace this record stage by stage, separate what the data proves from what a model can
            only suggest, and record what remains unresolved.
          </p>
        </div>
        <button
          ref={buttonRef}
          type="button"
          className="btn-small"
          onClick={run}
          disabled={state === "running"}
        >
          {state === "running"
            ? "Investigating…"
            : state === "done" || state === "error"
            ? "Investigate again"
            : "Investigate with AI"}
        </button>
      </div>

      <AmountLedger record={record} />

      <div className="sr-only" role="status" aria-live="polite">
        {state === "running" ? "Investigation in progress." : ""}
        {state === "done" ? "Investigation complete." : ""}
        {state === "error" ? `Investigation failed: ${error}` : ""}
      </div>

      <AnimatePresence initial={false}>
        {state === "running" && (
          <motion.div className="wk-progress" {...expand} style={{ overflow: "hidden" }}>
            <div
              className="wk-progress-track"
              role="progressbar"
              aria-label="Investigation request in flight"
            >
              <motion.span
                className="wk-progress-fill"
                style={{ display: "block", width: "38%" }}
                animate={{ x: ["-100%", "265%"] }}
                transition={{ duration: 1.15, repeat: Infinity, ease: "easeInOut" }}
              />
            </div>
            <p className="wk-progress-label">
              Request in flight · {elapsed.toFixed(1)}s elapsed. The backend reports no intermediate
              progress, so this bar shows that the call is open, not how far it has got.
            </p>
          </motion.div>
        )}
      </AnimatePresence>

      {state === "error" && (
        <p className="wk-note wk-note-bad" role="alert" style={{ marginTop: 12 }}>
          {error}
        </p>
      )}

      <AnimatePresence initial={false}>
        {state === "done" && result && (
          <motion.div {...riseIn} tabIndex={-1} ref={resultRef} style={{ outline: "none" }}>
            {result.explanation && (
              <p className="wk-note" style={{ marginTop: 16 }}>
                {String(result.explanation).replace(/^"|"$/g, "")}
              </p>
            )}

            <div className="wk-section" style={{ marginTop: 18 }}>
              <h4 className="wk-finding-title" style={{ marginBottom: 10 }}>Money-flow trace</h4>
              <BreakpointTrace
                trace={result.trace}
                breakpointStage={result.breakpoint_stage}
                breakpointKind={result.breakpoint_kind}
                currency={record?.merchant?.currency || "INR"}
              />
            </div>

            <div className="wk-finding">
              <h4 className="wk-finding-title">
                Confirmed evidence
                <span className="wk-finding-count">{confirmed.length}</span>
              </h4>
              {confirmed.length === 0 ? (
                <p className="wk-sub" style={{ marginTop: 8 }}>
                  Nothing at this stage could be established deterministically.
                </p>
              ) : (
                <ul className="wk-evlist">
                  {confirmed.map((line, i) => (
                    <motion.li
                      key={i}
                      initial={{ opacity: 0, y: 4 }}
                      animate={{ opacity: 1, y: 0 }}
                      transition={{ duration: DURATION.fast, ease: EASE, delay: listIndexDelay(i, 0.03, 6) }}
                    >
                      {line}
                    </motion.li>
                  ))}
                </ul>
              )}
            </div>

            <div className="wk-finding">
              <h4 className="wk-finding-title">
                Likely explanation
                <span className="wk-finding-count">{hypotheses.length} ranked</span>
              </h4>
              {hypotheses.length === 0 ? (
                <p className="wk-sub" style={{ marginTop: 8 }}>
                  No explanation was proposed for this record.
                </p>
              ) : (
                hypotheses.map((h, i) => (
                  <motion.div
                    className="wk-hyp"
                    key={i}
                    initial={{ opacity: 0, y: 6 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ duration: DURATION.fast, ease: EASE, delay: listIndexDelay(i, 0.05, 5) }}
                  >
                    <div className="wk-hyp-top">
                      <span className="wk-hyp-rank">{i + 1}</span>
                      <span className="wk-hyp-label">{humaniseLabel(h.label)}</span>
                      <span className="wk-hyp-conf">
                        {h.confidence == null ? "no confidence given" : `${Math.round(h.confidence * 100)}% confidence`}
                      </span>
                    </div>
                    {h.source && (
                      <div className="wk-hyp-source">
                        {h.source === "DETERMINISTIC"
                          ? "Derived from the data, not from a model."
                          : `Proposed by ${String(h.source).toLowerCase()}.`}
                      </div>
                    )}
                    {h.confidence != null && (
                      <div className="wk-hyp-meter" aria-hidden="true">
                        <motion.span
                          initial={{ width: 0 }}
                          animate={{ width: `${Math.max(0, Math.min(1, h.confidence)) * 100}%` }}
                          transition={{ duration: DURATION.slow, ease: EASE }}
                        />
                      </div>
                    )}
                    {h.rationale && <p className="wk-hyp-rationale">{h.rationale}</p>}
                    {hypothesisEvidence(h, result.evidence_index).length > 0 && (
                      <ul className="wk-hyp-evidence">
                        {hypothesisEvidence(h, result.evidence_index).map((e, j) => (
                          <li key={j}>{e}</li>
                        ))}
                      </ul>
                    )}
                  </motion.div>
                ))
              )}
            </div>

            <div className="wk-finding">
              <h4 className="wk-finding-title">
                Unresolved
                <span className="wk-finding-count">{unresolved.length}</span>
              </h4>
              {unresolved.length === 0 ? (
                <p className="wk-sub" style={{ marginTop: 8 }}>
                  Nothing was left open by this investigation.
                </p>
              ) : (
                <ul className="wk-evlist wk-evlist-unresolved">
                  {unresolved.map((line, i) => (
                    <li key={i}>{line}</li>
                  ))}
                </ul>
              )}
            </div>

            {result.recommended_action && (
              <div className="wk-action">
                <div className="wk-action-label">Recommended action</div>
                <div className="wk-action-value">
                  {String(result.recommended_action).replace(/_/g, " ").toLowerCase()}
                </div>
              </div>
            )}

            <p className="wk-provenance">
              <span
                className={`wk-prov-dot ${
                  result.ai_used || AI_STATUS[result.ai_status]?.tone === "on"
                    ? "wk-prov-on"
                    : "wk-prov-off"
                }`}
                aria-hidden="true"
              />
              {result.ai_used ? (
                <>
                  <strong>AI was used.</strong> The ranked explanations above were produced by{" "}
                  <strong>{result.ai_provider || "an unnamed provider"}</strong>. The confirmed
                  evidence and the trace are deterministic and were not written by a model.
                </>
              ) : (
                <>
                  <strong>AI was not used.</strong> This result is entirely deterministic — nothing
                  here was generated by a model.
                </>
              )}
              {AI_STATUS[result.ai_status] && <> {AI_STATUS[result.ai_status].text}</>}
              {result.ai_claims_dropped > 0 && (
                <>
                  {" "}
                  <strong>{result.ai_claims_dropped}</strong> model claim
                  {result.ai_claims_dropped === 1 ? " was" : "s were"} discarded for not being
                  supported by the evidence.
                </>
              )}
            </p>
          </motion.div>
        )}
      </AnimatePresence>
    </section>
  );
}
