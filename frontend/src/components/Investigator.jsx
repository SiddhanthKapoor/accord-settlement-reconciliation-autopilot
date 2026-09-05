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
export function readConsidered(record) {
  if (!record) return [];
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
 * An unrecognised value returns undefined and nothing is rendered, rather
 * than a guess dressed up as a status.
 */
export const AI_STATUS = {
  AI_AVAILABLE: { text: "Provider healthy.", tone: "on" },
  AI_FALLBACK_ACTIVE: { text: "AI fallback active — the secondary provider answered.", tone: "warn" },
  AI_UNAVAILABLE: { text: "AI unavailable — the model was needed and could not be reached.", tone: "warn" },
  AI_NOT_CONSULTED: { text: "The arithmetic settled it, so no model was called.", tone: "on" },
};

/** Engine labels arrive as constants; operators read sentences. */
export function humaniseLabel(label) {
  const text = String(label || "").replace(/_/g, " ").toLowerCase();
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/* --------------------------------------------------------------- the call */

/**
 * Results already fetched in this session, keyed by record and run.
 *
 * The review queue mounts the investigation inside a disclosure, so
 * collapsing and reopening it would otherwise fire a second request — and
 * on an ambiguous record that is a second model call for an answer we
 * already have. Bounded so a long session cannot grow it without limit.
 */
const CACHE = new Map();
const CACHE_MAX = 60;

function remember(key, value) {
  if (CACHE.has(key)) CACHE.delete(key);
  CACHE.set(key, value);
  while (CACHE.size > CACHE_MAX) CACHE.delete(CACHE.keys().next().value);
}

/** A backend string that is the offline verifier, not a model provider. */
export function isHeuristicBackend(name) {
  return String(name || "").toLowerCase().includes("heuristic");
}

/**
 * One investigation, run automatically, exactly once per record.
 *
 * There is no manual trigger anywhere in the product. The trace, the
 * provenance and the confirmed evidence are deterministic — they cost a
 * database read, not a model call — so making an operator ask for them was
 * asking them to press a button to find out what already happened. The
 * request goes out as the record appears; the only control that survives
 * is a retry, which exists because a failed request is the one case where
 * clicking again does something.
 */
export function useInvestigation({ recordId, batchId }) {
  const key = `${recordId}|${batchId || ""}`;
  const [state, setState] = useState("idle"); // idle | running | done | error
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [elapsed, setElapsed] = useState(0);
  const [finishedAt, setFinishedAt] = useState(null);
  const startedRef = useRef(0);
  const keyRef = useRef(key);

  const run = useCallback(
    async ({ force = false } = {}) => {
      const hit = force ? null : CACHE.get(key);
      if (hit) {
        setResult(hit.result);
        setFinishedAt(hit.finishedAt);
        setState("done");
        return;
      }
      startedRef.current = Date.now();
      setElapsed(0);
      setState("running");
      setError(null);
      try {
        const data = await investigateRecord(recordId, batchId);
        if (keyRef.current !== key) return; // the panel moved on
        const at = new Date();
        remember(key, { result: data, finishedAt: at });
        setResult(data);
        setFinishedAt(at);
        setState("done");
      } catch (e) {
        if (keyRef.current !== key) return;
        setError(
          e.status === 404
            ? "The investigation endpoint is not available on this backend build."
            : e.message
        );
        setState("error");
      }
    },
    [key, recordId, batchId]
  );

  // One effect owns the whole lifecycle: reset on record change, serve the
  // cache if it has the answer, otherwise fetch. No second effect racing it.
  useEffect(() => {
    keyRef.current = key;
    if (!recordId) return;
    setError(null);
    const hit = CACHE.get(key);
    if (hit) {
      setResult(hit.result);
      setFinishedAt(hit.finishedAt);
      setState("done");
      return;
    }
    setResult(null);
    setFinishedAt(null);
    run();
  }, [key, recordId, run]);

  useEffect(() => {
    if (state !== "running") return undefined;
    const id = setInterval(() => setElapsed((Date.now() - startedRef.current) / 1000), 100);
    return () => clearInterval(id);
  }, [state]);

  return { state, result, error, elapsed, finishedAt, run };
}

/* -------------------------------------------------------- presentational */

/**
 * The one investigation affordance in the product.
 *
 * It is a *status*, not a control, because by the time anyone reads it the
 * work is already under way or finished. Three states, each saying what
 * actually happened, and a button only in the state where pressing one
 * would change something.
 *
 * The distinction the copy has to protect: a record the arithmetic settled
 * never says "AI", and a record the offline verifier settled never says a
 * model was called. `ai_used` plus the provider name carry both facts.
 */
export function InvestigationStatus({ state, result, error, elapsed, finishedAt, onRetry }) {
  if (state === "running") {
    return (
      <span className="wk-runstate wk-runstate-live">
        <span className="wk-runstate-dot" aria-hidden="true" />
        Investigating… {elapsed.toFixed(1)}s
      </span>
    );
  }

  if (state === "error") {
    return (
      <span className="wk-runstate-group">
        <span className="wk-runstate wk-runstate-bad">
          <span aria-hidden="true">✕</span> Investigation failed
        </span>
        {onRetry && (
          <button type="button" className="wk-inv-rowlink" onClick={() => onRetry({ force: true })}>
            Try again
          </button>
        )}
      </span>
    );
  }

  if (state !== "done" || !result) return null;

  // Deliberately no attribution here. A record can be escalated to the
  // semantic tier *during the run* and still have its on-demand
  // investigation answered deterministically — and the reverse. Putting
  // "no model call" on this chip beside a step that says "model consulted
  // during the run" reads as a contradiction, so the chip reports state
  // only. Who answered what is stated, correctly scoped, in the two places
  // that own each question: "…for this investigation" below the ranked
  // explanation, and "…during the run" in the AI involvement step.
  const at = finishedAt
    ? finishedAt.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : null;

  return (
    <span className="wk-runstate wk-runstate-done">
      <span aria-hidden="true">✓</span>
      Investigated{at ? ` · ${at}` : ""}
    </span>
  );
}

/** An in-flight request. Says what it knows, which is only that it is open. */
export function InvestigationInFlight({ elapsed }) {
  return (
    <div className="wk-progress">
      <div className="wk-progress-track" role="progressbar" aria-label="Investigation request in flight">
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
    </div>
  );
}

export function ConfirmedEvidence({ items }) {
  if (!Array.isArray(items) || items.length === 0) {
    return (
      <p className="wk-sub">Nothing at this stage could be established deterministically.</p>
    );
  }
  return (
    <ul className="wk-evlist">
      {items.map((line, i) => (
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
  );
}

export function Hypotheses({ hypotheses, evidenceIndex }) {
  if (!Array.isArray(hypotheses) || hypotheses.length === 0) {
    return <p className="wk-sub">No explanation was proposed for this record.</p>;
  }
  return (
    <>
      {hypotheses.map((h, i) => (
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
          {hypothesisEvidence(h, evidenceIndex).length > 0 && (
            <ul className="wk-hyp-evidence">
              {hypothesisEvidence(h, evidenceIndex).map((e, j) => (
                <li key={j}>{e}</li>
              ))}
            </ul>
          )}
        </motion.div>
      ))}
    </>
  );
}

export function Unresolved({ items }) {
  if (!Array.isArray(items) || items.length === 0) {
    return <p className="wk-sub">Nothing was left open by this investigation.</p>;
  }
  return (
    <ul className="wk-evlist wk-evlist-unresolved">
      {items.map((line, i) => (
        <li key={i}>{line}</li>
      ))}
    </ul>
  );
}

/** Who wrote what is on screen. Never implied — always stated. */
export function InvestigationProvenance({ result }) {
  if (!result) return null;
  const status = AI_STATUS[result.ai_status];
  return (
    <p className="wk-provenance">
      <span
        className={`wk-prov-dot ${result.ai_used ? "wk-prov-on" : "wk-prov-off"}`}
        aria-hidden="true"
      />
      <span>
        {result.ai_used ? (
          <>
            <strong>AI was used for this investigation.</strong> The ranked explanations were
            produced by <strong>{result.ai_provider || "an unnamed provider"}</strong>. The
            confirmed evidence and the trace are deterministic and were not written by a model.
          </>
        ) : (
          <>
            <strong>AI was not used for this investigation.</strong> Everything above is
            deterministic — nothing here was generated by a model.
          </>
        )}
        {status && <> {status.text}</>}
        {result.ai_claims_dropped > 0 && (
          <>
            {" "}
            <strong>{result.ai_claims_dropped}</strong> model claim
            {result.ai_claims_dropped === 1 ? " was" : "s were"} discarded for not being supported
            by the evidence.
          </>
        )}
      </span>
    </p>
  );
}

/* ------------------------------------------------------------ standalone */

/**
 * One record's investigation, self-contained.
 *
 * Runs the moment it appears — there is no "Investigate" button here or
 * anywhere else. The trace, the provenance and the confirmed evidence are
 * deterministic reads, so gating them behind a click asked the operator to
 * request facts that already existed; and a button still reading
 * "Investigate" beside a finished investigation is the specific thing that
 * made this screen unreadable.
 *
 * The answer is rendered in three separated registers so a deterministic
 * fact is never read as a model's opinion:
 *
 *   CONFIRMED EVIDENCE — computed from the uploaded data
 *   LIKELY EXPLANATION — ranked hypotheses, each with its confidence
 *   UNRESOLVED         — what the data cannot settle either way
 *
 * The record view composes the same pieces in its own order and shows the
 * identical status line, so the affordance reads the same in both places.
 */
export default function Investigator({ recordId, batchId, record, autoFocus = false }) {
  const { state, result, error, elapsed, finishedAt, run } = useInvestigation({ recordId, batchId });
  const headingRef = useRef(null);
  const resultRef = useRef(null);

  useEffect(() => {
    if (autoFocus && headingRef.current) headingRef.current.focus();
  }, [autoFocus]);

  useEffect(() => {
    if (state === "done" && resultRef.current) resultRef.current.focus();
  }, [state]);

  return (
    <section className="wk-block wk-panelcard" aria-labelledby="wk-investigator-heading">
      <div className="wk-block-head">
        <div className="wk-block-titles">
          <h3
            className="wk-h2"
            id="wk-investigator-heading"
            ref={headingRef}
            tabIndex={-1}
            style={{ outline: "none" }}
          >
            Investigation
          </h3>
          <p className="wk-inv-blurb">
            This record traced stage by stage, with what the data proves kept apart from what a
            model can only suggest.
          </p>
        </div>
        <InvestigationStatus
          state={state}
          result={result}
          error={error}
          elapsed={elapsed}
          finishedAt={finishedAt}
          onRetry={run}
        />
      </div>

      <AmountLedger record={record} />

      <div className="sr-only" role="status" aria-live="polite">
        {state === "running" ? "Investigation in progress." : ""}
        {state === "done" ? "Investigation complete." : ""}
        {state === "error" ? `Investigation failed: ${error}` : ""}
      </div>

      <AnimatePresence initial={false}>
        {state === "running" && (
          <motion.div {...expand} style={{ overflow: "hidden" }}>
            <InvestigationInFlight elapsed={elapsed} />
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

            <div className="wk-finding">
              <h4 className="wk-finding-title">Money-flow trace</h4>
              <div style={{ marginTop: 12 }}>
                <BreakpointTrace
                  trace={result.trace}
                  breakpointStage={result.breakpoint_stage}
                  breakpointKind={result.breakpoint_kind}
                  currency={record?.merchant?.currency || "INR"}
                />
              </div>
            </div>

            <div className="wk-finding">
              <h4 className="wk-finding-title">
                Confirmed evidence
                <span className="wk-finding-count">
                  {(result.confirmed_evidence || []).length}
                </span>
              </h4>
              <div style={{ marginTop: 10 }}>
                <ConfirmedEvidence items={result.confirmed_evidence} />
              </div>
            </div>

            <div className="wk-finding">
              <h4 className="wk-finding-title">
                Likely explanation
                <span className="wk-finding-count">{(result.hypotheses || []).length} ranked</span>
              </h4>
              <div style={{ marginTop: 4 }}>
                <Hypotheses hypotheses={result.hypotheses} evidenceIndex={result.evidence_index} />
              </div>
            </div>

            <div className="wk-finding">
              <h4 className="wk-finding-title">
                Unresolved
                <span className="wk-finding-count">{(result.unresolved || []).length}</span>
              </h4>
              <div style={{ marginTop: 10 }}>
                <Unresolved items={result.unresolved} />
              </div>
            </div>

            {result.recommended_action && (
              <div className="wk-action">
                <div className="wk-action-label">Recommended action</div>
                <div className="wk-action-value">
                  {String(result.recommended_action).replace(/_/g, " ").toLowerCase()}
                </div>
              </div>
            )}

            <InvestigationProvenance result={result} />
          </motion.div>
        )}
      </AnimatePresence>
    </section>
  );
}
