import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { motion } from "motion/react";
import { pageTransition } from "../motion.js";
import { getAuditLog, streamAudit, verifyChain } from "../api.js";
import { useRoute } from "../router.jsx";
import { count } from "./MoneyFlow.jsx";
import "../panels.css";

/* ------------------------------------------------------------- rendering */

/**
 * Payload keys worth showing, in reading order, with the label an operator
 * would use. Anything not listed is left out rather than dumped: the audit
 * trail is evidence, and evidence that has to be parsed by eye out of a
 * JSON blob is not being presented, it is being hidden in plain sight.
 */
const PAYLOAD_FIELDS = [
  ["batch_id", "Run"],
  ["record_id", "Record"],
  ["label", "Label"],
  ["source_id", "Source id"],
  ["filename", "Source file"],
  ["source_type", "Source type"],
  ["role", "Side of the reconciliation"],
  ["rows", "Rows read"],
  ["unmapped_required", "Required columns still unmapped"],
  ["seq", "Position in run"],
  ["total", "Records in run"],
  ["outcome", "Outcome"],
  ["reason", "Reason"],
  ["ai_invoked", "AI consulted"],
  ["exception_type", "Exception type"],
  ["action", "Human action"],
  ["reviewer", "Reviewer"],
  ["note", "Reviewer note"],
  ["escalated_because", "Escalated because"],
  ["automated_outcome", "Automated outcome"],
  ["matched_payment_id", "Matched settlement"],
  ["breakpoint_stage", "Breakpoint stage"],
  ["breakpoint_kind", "Breakpoint kind"],
  ["hypothesis_labels", "Hypotheses considered"],
  ["ai_hypothesis_labels", "Hypotheses from the model"],
  ["recommended_action", "Recommended action"],
  ["ai_used", "Model consulted"],
  ["ai_provider", "Provider"],
  ["ai_status", "Provider status"],
  ["ai_claims_dropped", "Model claims discarded"],
  ["read_only", "Read-only investigation"],
  ["error", "Error"],
];

const MONO_KEYS = new Set([
  "batch_id",
  "record_id",
  "source_id",
  "matched_payment_id",
  "filename",
]);

function renderValue(key, value) {
  if (value === true) return "yes";
  if (value === false) return "no";
  if (Array.isArray(value)) return value.length === 0 ? null : value.join(" · ");
  if (typeof value === "number") return value.toLocaleString("en-IN");
  return String(value);
}

/* Event names as a person says them. Sentence-casing the enum turns
   AI_INVESTIGATION into "Ai investigation", which reads as a typo in the
   one view whose whole job is to look exact. */
const EVENT_LABELS = {
  RUN_CREATED: "Run created",
  SOURCE_UPLOADED: "Source uploaded",
  PLAN_UPDATED: "Plan updated",
  BATCH_STARTED: "Run started",
  BATCH_COMPLETED: "Run completed",
  BATCH_FAILED: "Run failed",
  RECORD_DECIDED: "Record decided",
  RECORD_REVISED: "Record revised",
  AI_INVESTIGATION: "AI investigation",
  HUMAN_REVIEW_ACTION: "Human review action",
};

function humanise(value) {
  if (EVENT_LABELS[value]) return EVENT_LABELS[value];
  const s = String(value || "").replace(/_/g, " ").toLowerCase();
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function shortTime(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso || "");
  return d.toLocaleTimeString("en-GB", { hour12: false });
}

function fullTime(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso || "");
  return d.toLocaleString("en-GB", { hour12: false });
}

const isAiEvent = (e) =>
  e.event_type === "AI_INVESTIGATION" || Boolean(e.payload && e.payload.ai_invoked);
const isHumanEvent = (e) => e.event_type === "HUMAN_REVIEW_ACTION";

const CLASSES = [
  { key: "ALL", label: "All events", match: () => true },
  { key: "AI", label: "AI decisions", match: isAiEvent },
  { key: "HUMAN", label: "Human actions", match: isHumanEvent },
];

const WINDOW = 500;

/**
 * The most recent `limit` events, not the first `limit`.
 *
 * `/audit/log` answers "everything after seq N, oldest first", so asking
 * from zero on a ledger with thousands of events returns its opening
 * pages and none of today's work — a human action written a minute ago
 * would not appear in the view that exists to prove it happened. The head
 * is read first so the window can be anchored to the end of the chain.
 *
 * Two calls: one to learn where the chain currently ends, one to take the
 * window that ends there.
 */
async function loadRecentEvents(limit = WINDOW) {
  const head = await getAuditLog(1);
  const since = Math.max(0, (head.head_seq || 0) - limit);
  return getAuditLog(limit, since);
}

/* ================================================================= screen */

export default function AuditTrail() {
  const route = useRoute();
  const [events, setEvents] = useState([]);
  const [chain, setChain] = useState(null);
  const [chainError, setChainError] = useState(null);
  const [checkedAt, setCheckedAt] = useState(null);
  const [verifying, setVerifying] = useState(true);
  const [live, setLive] = useState(false);
  const [expanded, setExpanded] = useState(null);

  const [query, setQuery] = useState(route.query?.record || "");
  const [runFilter, setRunFilter] = useState("");
  const [typeFilter, setTypeFilter] = useState("");
  const [classFilter, setClassFilter] = useState("ALL");

  const scrollRef = useRef(null);
  const pinnedRef = useRef(true);

  // Load the existing ledger first, then tail the stream from wherever
  // that history ended. The stream starts at the head by design, so it
  // carries new events only — subscribing without loading history first
  // leaves this view empty next to a chain reporting hundreds of events.
  useEffect(() => {
    let stop = () => {};
    let cancelled = false;
    loadRecentEvents(WINDOW)
      .then(({ events: history, head_seq }) => {
        if (cancelled) return;
        setEvents(history);
        setLive(true);
        stop = streamAudit((event) => {
          setEvents((prev) =>
            prev.some((e) => e.seq === event.seq) ? prev : [...prev.slice(-999), event]
          );
        }, head_seq);
      })
      .catch(() => {
        if (cancelled) return;
        setLive(true);
        stop = streamAudit((event) => setEvents((prev) => [...prev.slice(-999), event]));
      });
    return () => {
      cancelled = true;
      setLive(false);
      stop();
    };
  }, []);

  async function runVerify() {
    setVerifying(true);
    setChainError(null);
    try {
      const status = await verifyChain();
      setChain(status);
      setCheckedAt(new Date());
    } catch (e) {
      // Never leave a stale "verified" banner standing after a failed
      // check: an unanswered question must not look like a good answer.
      setChain(null);
      setChainError(e.message || "the verification request failed");
      setCheckedAt(new Date());
    } finally {
      setVerifying(false);
    }
  }

  useEffect(() => {
    runVerify();
  }, []);

  // Follow the live tail only when the reader is already at the tail.
  // Yanking the viewport while someone is reading an expanded event is
  // the fastest way to make a log feel untrustworthy.
  useEffect(() => {
    const el = scrollRef.current;
    if (el && pinnedRef.current) el.scrollTop = el.scrollHeight;
  }, [events]);

  // Most events carry the run on the payload; the run's own lifecycle
  // events carry it as the transaction id instead.
  const runs = useMemo(() => {
    const set = new Set();
    for (const e of events) {
      const id =
        e.payload?.batch_id ||
        (String(e.transaction_id || "").startsWith("run_") ? e.transaction_id : null);
      if (id) set.add(id);
    }
    return [...set].sort();
  }, [events]);

  const types = useMemo(
    () => [...new Set(events.map((e) => e.event_type))].sort(),
    [events]
  );

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    const cls = CLASSES.find((c) => c.key === classFilter) || CLASSES[0];
    return events.filter((e) => {
      if (!cls.match(e)) return false;
      if (typeFilter && e.event_type !== typeFilter) return false;
      if (runFilter) {
        const run = e.payload?.batch_id || e.transaction_id;
        if (run !== runFilter) return false;
      }
      if (q) {
        const hay = `${e.transaction_id || ""} ${e.event_type} ${e.payload?.record_id || ""} ${
          e.payload?.batch_id || ""
        }`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }, [events, query, runFilter, typeFilter, classFilter]);

  const filtered = Boolean(query || runFilter || typeFilter || classFilter !== "ALL");

  let chainClass = "pn-chain";
  let chainTitle = "Verifying the chain…";
  let chainMark = "";
  if (chain && chain.intact) {
    chainClass += " pn-chain-ok";
    chainTitle = "Chain integrity verified";
    chainMark = "✓";
  } else if (chain && !chain.intact) {
    chainClass += " pn-chain-bad";
    chainTitle = "Chain integrity check failed";
    chainMark = "✗";
  } else if (chainError) {
    chainClass += " pn-chain-unknown";
    chainTitle = "Chain integrity not confirmed";
    chainMark = "?";
  }

  return (
    <motion.div className="page" {...pageTransition}>
      <div className="page-header pn-head">
        <h1 className="page-title">Audit trail</h1>
        <p className="pn-lede">
          Every reconciliation decision and human action is recorded in an append-only event chain.
        </p>
      </div>

      <div className={chainClass}>
        <div className="pn-chain-main">
          <p className="pn-chain-status">
            <span className="pn-chain-mark" aria-hidden="true">
              {chainMark}
            </span>
            {chainTitle}
          </p>
          {chain && chain.intact && (
            <p className="pn-chain-line">
              Every event hashes the one before it. Recomputing the chain end to end reproduced the
              head exactly, so nothing has been inserted, removed or rewritten since it was written.
            </p>
          )}
          {chain && !chain.intact && (
            <p className="pn-chain-line">
              Recomputing the chain did not reproduce the recorded hashes. Until this is resolved
              the ledger cannot be relied on as evidence.
            </p>
          )}
          {chainError && (
            <p className="pn-chain-line">
              The verification endpoint could not be reached ({chainError}), so integrity is
              unknown — not confirmed, and not disproved.
            </p>
          )}
          {!chain && !chainError && verifying && (
            <p className="pn-chain-line">Recomputing every hash in the chain.</p>
          )}
          <div className="pn-chain-facts">
            {chain && <span>{count(chain.total_events)} events</span>}
            {chain && chain.breaks?.length > 0 && (
              <span>{count(chain.breaks.length)} break(s) found</span>
            )}
            {chain?.head_hash && (
              <span>
                head <code>{chain.head_hash.slice(0, 24)}…</code>
              </span>
            )}
            {checkedAt && <span>checked {checkedAt.toLocaleTimeString("en-GB", { hour12: false })}</span>}
          </div>
        </div>
        <div className="pn-chain-side">
          {live && (
            <span className="pn-live">
              <span className="pn-live-dot" aria-hidden="true" />
              Live feed
            </span>
          )}
          <button type="button" className="pn-btn" onClick={runVerify} disabled={verifying}>
            {verifying ? "Verifying…" : "Re-verify"}
          </button>
        </div>
      </div>

      <div className="pn-toolbar">
        <div className="pn-chips" role="group" aria-label="Filter by decision maker">
          {CLASSES.map((c) => (
            <button
              key={c.key}
              type="button"
              className="pn-chip"
              aria-pressed={classFilter === c.key}
              onClick={() => setClassFilter(c.key)}
            >
              {c.label}
              <span className="pn-chip-count">{count(events.filter(c.match).length)}</span>
            </button>
          ))}
        </div>
        <span className="pn-spacer" />
        <span className="pn-field">
          <label className="pn-field-label" htmlFor="pn-audit-run">
            Run
          </label>
          <select
            id="pn-audit-run"
            className="pn-select"
            value={runFilter}
            onChange={(e) => setRunFilter(e.target.value)}
          >
            <option value="">Any run</option>
            {runs.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </span>
        <span className="pn-field">
          <label className="pn-field-label" htmlFor="pn-audit-type">
            Event
          </label>
          <select
            id="pn-audit-type"
            className="pn-select"
            value={typeFilter}
            onChange={(e) => setTypeFilter(e.target.value)}
          >
            <option value="">Any event</option>
            {types.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </span>
        <span className="pn-field">
          <label className="pn-field-label" htmlFor="pn-audit-q">
            Record
          </label>
          <input
            id="pn-audit-q"
            className="pn-input"
            type="search"
            placeholder="Record or run id"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </span>
      </div>

      <div className="pn-ledger">
        <div
          className="pn-ledger-scroll"
          ref={scrollRef}
          onScroll={(e) => {
            const el = e.currentTarget;
            pinnedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
          }}
        >
          <table className="pn-events">
            <caption className="sr-only">
              Hash-chained audit events, oldest first. Select a row to reveal its provenance and
              hashes.
            </caption>
            <thead>
              <tr>
                <th scope="col">#</th>
                <th scope="col">Time</th>
                <th scope="col">Event</th>
                <th scope="col">Record / run</th>
                <th scope="col">State</th>
                <th scope="col">
                  <span className="sr-only">Detail</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {shown.map((e, i) => {
                const open = expanded === e.seq;
                return (
                  <Fragment key={e.seq}>
                    <tr
                      /* Striping by class, not :nth-child: the expansion
                         rows are interleaved into this tbody and would
                         otherwise take a stripe and shift every row after
                         them. */
                      className={"pn-event-row" + (i % 2 === 1 ? " pn-row-alt" : "")}
                      tabIndex={0}
                      role="button"
                      aria-expanded={open}
                      aria-label={`Event ${e.seq}, ${humanise(e.event_type)}, ${e.transaction_id}`}
                      onClick={() => setExpanded(open ? null : e.seq)}
                      onKeyDown={(ev) => {
                        if (ev.key === "Enter" || ev.key === " ") {
                          ev.preventDefault();
                          setExpanded(open ? null : e.seq);
                        }
                      }}
                    >
                      <td className="pn-seq">{e.seq}</td>
                      <td className="pn-time">{shortTime(e.timestamp)}</td>
                      <td className="pn-event-name">{humanise(e.event_type)}</td>
                      <td className="pn-event-tx">{e.transaction_id}</td>
                      <td className="pn-state">
                        {e.prior_state && e.new_state && e.prior_state !== e.new_state
                          ? `${e.prior_state} → ${e.new_state}`
                          : e.new_state || "—"}
                      </td>
                      <td className="pn-caret" aria-hidden="true">
                        {open ? "▾" : "▸"}
                      </td>
                    </tr>
                    {open && (
                      <tr>
                        <td className="pn-detail-cell" colSpan={6}>
                          <EventDetail event={e} />
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
              {shown.length === 0 && (
                <tr>
                  <td colSpan={6} style={{ padding: "22px 0" }}>
                    <span className="pn-empty">
                      {events.length === 0
                        ? "No events yet — the ledger fills as soon as a workspace reconciles."
                        : "No event matches those filters."}
                    </span>
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        <p className="pn-count-line">
          Showing {count(shown.length)} of {count(events.length)} loaded event
          {events.length === 1 ? "" : "s"}
          {filtered ? " (filtered)" : ""}
          {chain ? ` · the chain holds ${count(chain.total_events)}` : ""}
        </p>
      </div>
    </motion.div>
  );
}

/* --------------------------------------------------------------- detail */

function EventDetail({ event }) {
  const payload = event.payload || {};
  const fields = PAYLOAD_FIELDS.map(([key, label]) => {
    if (payload[key] == null) return null;
    const value = renderValue(key, payload[key]);
    if (value == null || value === "") return null;
    return { key, label, value };
  }).filter(Boolean);

  return (
    <div className="pn-detail">
      <dl className="pn-kv">
        <dt>Timestamp</dt>
        <dd>{fullTime(event.timestamp)}</dd>
        <dt>Event</dt>
        <dd>{humanise(event.event_type)}</dd>
        <dt>Record / run</dt>
        <dd className="pn-hash">{event.transaction_id}</dd>
        <dt>State</dt>
        <dd>
          {event.prior_state || "—"} → {event.new_state || "—"}
        </dd>
      </dl>

      {fields.length > 0 && (
        <>
          <p className="pn-kv-group">Provenance and decision</p>
          <dl className="pn-kv">
            {fields.map((f) => (
              <Fragment key={f.key}>
                <dt>{f.label}</dt>
                <dd className={MONO_KEYS.has(f.key) ? "pn-hash" : undefined}>{f.value}</dd>
              </Fragment>
            ))}
          </dl>
        </>
      )}

      <p className="pn-kv-group">Chain</p>
      <dl className="pn-kv">
        <dt>Previous hash</dt>
        <dd className="pn-hash">{event.prev_hash}</dd>
        <dt>Event hash</dt>
        <dd className="pn-hash">{event.hash}</dd>
        {event.evidence_ref && (
          <>
            <dt>Evidence reference</dt>
            <dd className="pn-hash">{event.evidence_ref}</dd>
          </>
        )}
      </dl>
    </div>
  );
}
