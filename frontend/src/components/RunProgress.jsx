import { useEffect, useRef, useState } from "react";
import { getRunProgress } from "../api.js";
import { count } from "./MoneyFlow.jsx";
import "../workspace.css";

/*
 * Pipeline state for a run in flight.
 *
 * The single rule this component exists to enforce: **nothing on screen
 * may be invented.** Every label, every count, every state comes out of
 * `GET /runs/{run_id}/progress`, which derives them from the store. A
 * stage whose `count` is null renders as in progress and nothing more —
 * no spinner shaped like a number, no interpolation between the last two
 * polls, no bar easing toward a total the backend has not reported.
 *
 * The five steps are not hard-coded here either. If the backend sends
 * four, four are drawn. If it reports deterministic matching and AI
 * investigation as ACTIVE at the same time — which is what actually
 * happens, since ambiguity is escalated per record rather than in a
 * second pass — both are drawn active, because that is the truth and a
 * forced sequential animation would be a lie about the architecture.
 *
 * The endpoint is new. Where it is absent this degrades to the audit
 * stream's own processed count, and says so.
 */

const STATE_WORD = { DONE: "Done", ACTIVE: "Working", PENDING: "Waiting" };

/**
 * @param {string} runId
 * @param {boolean} active   run is still executing — poll while true
 * @param {number} sseProcessed  records seen on the audit stream (fallback only)
 * @param {number} sseTotal      the batch's own total (fallback only)
 * @param {(data:object)=>void} onData  hand the payload up; the run summary
 *        uses `ai_consulted`, which is a store count, not a page count.
 */
export default function RunProgress({ runId, active, sseProcessed = 0, sseTotal = 0, onData }) {
  const [data, setData] = useState(null);
  const [available, setAvailable] = useState(null); // null = unknown yet
  const onDataRef = useRef(onData);
  onDataRef.current = onData;

  useEffect(() => {
    if (!runId) return undefined;
    let cancelled = false;
    let timer = 0;
    let seenOk = false;

    async function poll() {
      try {
        const body = await getRunProgress(runId);
        if (cancelled) return;
        seenOk = true;
        setAvailable(true);
        setData(body);
        onDataRef.current?.(body);
        if (body.stage !== "COMPLETE" && active) timer = setTimeout(poll, 1000);
      } catch (e) {
        // A missing route is permanent; a transient failure is not, but it
        // is also not a reason to keep hammering. Either way: stop, keep
        // the last real payload on screen, and let the next status change
        // re-mount this.
        if (cancelled) return;
        if (e.status === 404 || e.status === 405 || e.status === 501 || !seenOk) {
          setAvailable(false);
        }
      }
    }

    poll();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
    // `active` flipping to false ends the poll loop on the next tick.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId, active]);

  const stages = Array.isArray(data?.stages) ? data.stages : null;

  // ---- degraded path: the audit stream, and an honest label on it ------
  if (available === false || !stages) {
    if (!active) return null;
    const pct = sseTotal > 0 ? Math.min(100, (sseProcessed / sseTotal) * 100) : null;
    return (
      <section className="wk-prog" aria-labelledby="wk-prog-heading">
        <div className="wk-prog-top">
          <div>
            <div className="wk-eyebrow" id="wk-prog-heading">
              Reconciling
            </div>
            <p className="wk-sub" style={{ marginTop: 6 }}>
              {available === false
                ? "This backend build does not publish per-stage pipeline state, so the only progress shown is the record count the audit stream has reported."
                : "Reading pipeline state…"}
            </p>
          </div>
          {sseTotal > 0 && (
            <div className="wk-prog-counter">
              {count(sseProcessed)} <span>/ {count(sseTotal)} records</span>
            </div>
          )}
        </div>
        {pct != null && (
          <div className="wk-progress-track" role="progressbar" aria-valuenow={sseProcessed} aria-valuemin={0} aria-valuemax={sseTotal}>
            <span className="wk-progress-fill" style={{ display: "block", width: `${pct}%`, transition: "width .3s linear" }} />
          </div>
        )}
      </section>
    );
  }

  const complete = data.stage === "COMPLETE";
  if (complete && !active) return null;

  const processed = typeof data.processed === "number" ? data.processed : null;
  const total = typeof data.total === "number" && data.total > 0 ? data.total : null;
  const pct = processed != null && total != null ? Math.min(100, (processed / total) * 100) : null;

  return (
    <section className="wk-prog" aria-labelledby="wk-prog-heading">
      <div className="wk-prog-top">
        <div>
          <div className="wk-eyebrow" id="wk-prog-heading">
            {complete ? "Run complete" : "Reconciling"}
          </div>
          <p className="wk-sub" style={{ marginTop: 6 }}>
            Deterministic matching and the ambiguity tier interleave — a record is escalated the
            moment its evidence turns out to be ambiguous, not in a second pass afterwards.
            {data.semantic_backend === "heuristic"
              ? " On this run the tier is being served by Accord's offline verifier; no model is being called."
              : ""}
          </p>
        </div>
        {processed != null && (
          <div className="wk-prog-counter">
            {count(processed)}
            {total != null && <span> / {count(total)} records</span>}
          </div>
        )}
      </div>

      {pct != null && (
        <div
          className="wk-progress-track"
          role="progressbar"
          aria-valuenow={processed}
          aria-valuemin={0}
          aria-valuemax={total}
          aria-label="Records processed"
          style={{ marginBottom: 16 }}
        >
          <span
            className="wk-progress-fill"
            style={{ display: "block", width: `${pct}%`, transition: "width .3s linear" }}
          />
        </div>
      )}

      <ol className="wk-steps">
        {stages.map((s) => {
          const state = String(s.state || "PENDING").toUpperCase();
          const cls =
            state === "DONE" ? "wk-step-done" : state === "ACTIVE" ? "wk-step-active" : "";
          // Only what came back. `count == null` means the backend does not
          // know yet, and the honest rendering of "does not know yet" is
          // the state word on its own.
          const hasCount = typeof s.count === "number";
          const hasTotal = typeof s.total === "number";
          return (
            <li className={`wk-step ${cls}`} key={s.key || s.label}>
              <span className="wk-step-mark" aria-hidden="true">
                {state === "DONE" ? "✓" : state === "ACTIVE" ? "·" : ""}
              </span>
              <div>
                <div className="wk-step-label">{s.label}</div>
                {s.detail && <div className="wk-step-detail">{s.detail}</div>}
              </div>
              <div>
                {hasCount ? (
                  <div className="wk-step-count">
                    {count(s.count)}
                    {hasTotal && <span className="wk-step-count-total"> / {count(s.total)}</span>}
                  </div>
                ) : null}
                <div className="wk-step-state">{STATE_WORD[state] || state}</div>
              </div>
            </li>
          );
        })}
      </ol>

      <p className="wk-prog-note">
        Every figure here is read from the run itself. A step with no number beside it is one the
        backend has not counted yet — Accord shows the gap rather than filling it in.
      </p>

      <div className="sr-only" role="status" aria-live="polite">
        {processed != null && total != null
          ? `${processed} of ${total} records processed.`
          : "Reconciliation in progress."}
      </div>
    </section>
  );
}
