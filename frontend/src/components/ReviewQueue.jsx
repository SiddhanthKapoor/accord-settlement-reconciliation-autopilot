import { AnimatePresence, motion } from "motion/react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getReviewQueue, listRuns, reviewQueueExportUrl, submitReviewAction, verifyChain } from "../api.js";
import { DURATION, EASE, expand, listIndexDelay, pageTransition } from "../motion.js";
import { Link } from "../router.jsx";
import Investigator from "./Investigator.jsx";
import { count, money } from "./MoneyFlow.jsx";
import "../panels.css";

/* ------------------------------------------------------------- vocabulary */

const SEVERITY_LABEL = { HIGH: "High", MEDIUM: "Medium", LOW: "Low" };

/**
 * The exception types where the disagreement is about the *money* rather
 * than about which settlement a record belongs to. The backend already
 * withholds "approve match" for these — this set exists only so the
 * explanation shown to the operator is specific rather than generic. It is
 * never used to add or remove a button.
 */
const MONEY_DISPUTES = new Set([
  "AMOUNT_MISMATCH",
  "CURRENCY_MISMATCH",
  "FEE_TAX_INCONSISTENT",
  "REFUND_MISMATCH",
]);

function humanise(value) {
  const s = String(value || "").replace(/_/g, " ").toLowerCase();
  return s.charAt(0).toUpperCase() + s.slice(1);
}

function normaliseRef(value) {
  return String(value || "").toUpperCase().replace(/[^A-Z0-9]/g, "");
}

function dayKey(iso) {
  return iso ? String(iso).slice(0, 10) : null;
}

function day(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso).slice(0, 10);
  return d.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
}

function parseJson(text) {
  try {
    return JSON.parse(text || "{}");
  } catch {
    return {};
  }
}

/* --------------------------------------------------------- evidence model */

/**
 * The settlement records to stand next to the ledger record.
 *
 * Two backend shapes carry candidates. `candidates` is the full settlement
 * row — fees, tax, net — and exists once the engine has something concrete
 * in hand. `considered_candidates` is the thinner retrieval record, and it
 * is the only thing present when everything retrieved was refused. Where
 * only the thin shape exists the missing fields are rendered as gaps, not
 * as zeroes: a fee that was never read is not a fee of nothing.
 */
function evidenceColumns(item) {
  const considered = item.considered_candidates || [];
  const meta = new Map(considered.map((c) => [c.payment_id, c]));
  const rich = item.candidates || [];

  const pool = rich.length
    ? rich.map((c) => ({ ...c, meta: meta.get(c.payment_id) || null }))
    : [...considered]
        .sort((a, b) => (b.evidence_score || 0) - (a.evidence_score || 0))
        .map((c) => ({
          payment_id: c.payment_id,
          order_reference: c.order_reference,
          gross_amount_minor: c.gross_amount_minor,
          settlement_date: c.settlement_date,
          meta: c,
        }));

  // Whatever the engine actually settled on leads, so the operator reads
  // the proposed answer first and the alternatives after it.
  return pool.sort(
    (a, b) =>
      (b.payment_id === item.matched_payment_id ? 1 : 0) -
      (a.payment_id === item.matched_payment_id ? 1 : 0)
  );
}

/**
 * Field-by-field comparison rows.
 *
 * `same` compares two values the backend supplied and nothing more — it
 * never restates a verdict. Where the two sides are not comparable at all
 * (a settlement date has no ledger counterpart; free text always differs
 * literally) it returns null and no marker is drawn, because "differs" on
 * a description would be noise dressed as a finding. The engine's own
 * PASS/WARN/FAIL judgements are rendered separately, from `checks`.
 */
function comparisonRows(item) {
  const m = item.merchant || {};
  const cur = m.currency;
  const rows = [
    {
      label: "Reference",
      ledger: m.reference_id || null,
      cell: (c) => c.order_reference || null,
      same: (c) =>
        !c.order_reference || !m.reference_id
          ? null
          : normaliseRef(c.order_reference) === normaliseRef(m.reference_id),
    },
    {
      label: "Gross amount",
      ledger: m.amount_minor == null ? null : money(m.amount_minor, { currency: cur }),
      cell: (c) =>
        c.gross_amount_minor == null
          ? null
          : money(c.gross_amount_minor, { currency: c.currency || cur }),
      same: (c) =>
        c.gross_amount_minor == null || m.amount_minor == null
          ? null
          : Number(c.gross_amount_minor) === Number(m.amount_minor),
    },
    {
      label: "Currency",
      ledger: m.currency || null,
      cell: (c) => c.currency || null,
      same: (c) => (!c.currency || !m.currency ? null : c.currency === m.currency),
    },
    {
      label: "Order date",
      ledger: day(m.order_date),
      cell: (c) => day(c.order_date),
      same: (c) =>
        !c.order_date || !m.order_date ? null : dayKey(c.order_date) === dayKey(m.order_date),
    },
    {
      label: "Refund",
      ledger: m.refund_amount_minor == null ? null : money(m.refund_amount_minor, { currency: cur }),
      cell: (c) =>
        c.refund_amount_minor == null ? null : money(c.refund_amount_minor, { currency: c.currency || cur }),
      same: (c) =>
        c.refund_amount_minor == null || m.refund_amount_minor == null
          ? null
          : Number(c.refund_amount_minor) === Number(m.refund_amount_minor),
    },
    {
      label: "Settlement date",
      settlementOnly: true,
      ledger: null,
      cell: (c) => day(c.settlement_date),
      same: () => null,
    },
    {
      label: "Fee deducted",
      settlementOnly: true,
      ledger: null,
      cell: (c) => (c.fee_minor == null ? null : money(c.fee_minor, { currency: c.currency || cur })),
      same: () => null,
    },
    {
      label: "Tax deducted",
      settlementOnly: true,
      ledger: null,
      cell: (c) => (c.tax_minor == null ? null : money(c.tax_minor, { currency: c.currency || cur })),
      same: () => null,
    },
    {
      label: "Net settled",
      settlementOnly: true,
      ledger: null,
      cell: (c) =>
        c.net_amount_minor == null ? null : money(c.net_amount_minor, { currency: c.currency || cur }),
      same: () => null,
    },
    {
      label: "Status",
      ledger: m.status || null,
      cell: (c) => c.status || null,
      same: () => null,
    },
    {
      label: "Description",
      prose: true,
      ledger: m.description || null,
      cell: (c) => c.description || null,
      same: () => null,
    },
  ];
  return rows;
}

/* ------------------------------------------------------------- triage sets */

/**
 * The triage chips, named for what the group IS.
 *
 * These used to be labelled in the engine's vocabulary — "Ambiguous",
 * "Candidates, unmatched", "Missing evidence" — which tells an operator
 * nothing about what is wrong or what they are meant to do about it. Each
 * group is now a plain statement of the problem, backed by the record's
 * real `exception_type`, with a one-line explanation carried both as a
 * tooltip and as visible helper text under the row.
 *
 * `OTHER` is the catch-all and exists so no open record can be
 * unreachable through the chips: whatever the engine emits that is not
 * named above still has a chip to land in.
 */
const GROUPS = [
  {
    key: "ALL",
    label: "All open",
    help: "Every record still waiting on a person in this run.",
    match: () => true,
  },
  {
    key: "HIGH",
    label: "High priority",
    help: "Rated high severity by the engine — the largest and clearest problems. Start here.",
    match: (i) => i.severity === "HIGH",
  },
  {
    key: "AMOUNT",
    label: "Amount doesn't match",
    help:
      "The settlement was found, but the figures on it disagree with the ledger — gross amount, fee, tax or refund.",
    match: (i) =>
      i.exception_type === "AMOUNT_MISMATCH" ||
      i.exception_type === "FEE_TAX_INCONSISTENT" ||
      i.exception_type === "REFUND_MISMATCH",
  },
  {
    key: "CURRENCY",
    label: "Wrong currency",
    help: "Settled in a different currency from the one the order was booked in.",
    match: (i) => i.exception_type === "CURRENCY_MISMATCH",
  },
  {
    key: "MULTIPLE",
    label: "Several possible matches",
    help:
      "More than one settlement could be this order's. Accord refused to pick one rather than guess.",
    match: (i) =>
      i.exception_type === "AMBIGUOUS_MATCH" ||
      i.exception_type === "DUPLICATE_REFERENCE" ||
      i.exception_type === "DUPLICATE_CLAIM" ||
      i.classification === "SEMANTIC_UNRESOLVED" ||
      i.classification === "AMBIGUOUS_MULTIPLE",
  },
  {
    key: "NOTFOUND",
    label: "No settlement found",
    help:
      "Nothing in the settlement or bank files could be admitted as this order's money. Raise it with the provider.",
    match: (i) =>
      i.exception_type === "MISSING_SETTLEMENT" ||
      i.classification === "NO_ADMISSIBLE_CANDIDATE",
  },
  {
    key: "WAITING",
    label: "Waiting on settlement",
    help:
      "The payout window has not closed, or the payout is running late. Re-run once the money lands.",
    match: (i) =>
      i.exception_type === "PENDING_SETTLEMENT" ||
      i.exception_type === "SETTLEMENT_DELAYED" ||
      i.classification === "PENDING_SETTLEMENT_WINDOW",
  },
  {
    key: "BATCHED",
    label: "Paid in a lump sum",
    help:
      "Settled inside a batch payout covering several orders, so it cannot be matched one to one.",
    match: (i) => i.exception_type === "AGGREGATED_SETTLEMENT",
  },
  {
    key: "PROVIDER",
    label: "Provider reported an error",
    help: "The gateway or bank returned an error against this transaction rather than a settlement.",
    match: (i) => i.exception_type === "PROVIDER_ERROR",
  },
];

/** Anything the named groups above do not claim, so nothing is unreachable. */
const OTHER_GROUP = {
  key: "OTHER",
  label: "Everything else",
  help: "Open records that do not fall into any of the groups above.",
  match: (i) => !GROUPS.slice(1).some((g) => g.match(i)),
};

const ALL_GROUPS = [...GROUPS, OTHER_GROUP];

/* ================================================================= screen */

/**
 * Work the pipeline refused to decide on its own.
 *
 * The action buttons are whatever the backend says are available for this
 * record and nothing else. That is deliberate and load-bearing: where the
 * amounts or currencies themselves disagree, the backend withholds
 * "approve match", because the dispute is not about *which* settlement
 * this is — and reconciling a record whose amount is known to be wrong is
 * exactly the failure this product exists to prevent. Hard-coding a
 * button row here would quietly undo that at the last step, so this
 * component never invents an action.
 */
export default function ReviewQueue() {
  const [queue, setQueue] = useState(null);
  const [runs, setRuns] = useState([]);
  const [batchId, setBatchId] = useState(null);
  const [group, setGroup] = useState("ALL");
  const [error, setError] = useState(null);
  const [busyId, setBusyId] = useState(null);
  const [evidenceOpen, setEvidenceOpen] = useState(null);
  const [investigating, setInvestigating] = useState(null);
  const [receipt, setReceipt] = useState(null);
  const receiptRef = useRef(null);

  const [loadingAll, setLoadingAll] = useState(false);

  /**
   * Read the queue.
   *
   * The default limit is high enough that a normal run's whole open queue
   * arrives in one request. Where it does not, the response says so —
   * `total` against `returned` — and the screen states the gap rather than
   * showing a summary of 76 above a list of 50 and leaving the reader to
   * work out which number to believe.
   */
  const load = useCallback((id, limit) => {
    return getReviewQueue({ batchId: id || undefined, ...(limit ? { limit } : {}) })
      .then((data) => {
        setQueue(data);
        setError(null);
      })
      .catch((e) => setError(e.message));
  }, []);

  useEffect(() => {
    load(batchId);
  }, [load, batchId]);

  useEffect(() => {
    listRuns()
      .then((data) => setRuns(data.runs || []))
      .catch(() => setRuns([]));
  }, []);

  async function act(item, action) {
    setBusyId(item.record_id);
    setReceipt(null);
    try {
      await submitReviewAction(item.record_id, { batchId: queue.batch_id, action: action.action });
      // Re-verify rather than assert: the point of writing a human
      // decision into the same chain as the machine's is that it can be
      // checked, so the confirmation shown is the check's actual answer.
      let chain = null;
      let chainError = null;
      try {
        chain = await verifyChain();
      } catch (e) {
        chainError = e.message;
      }
      setReceipt({
        recordId: item.record_id,
        label: action.label,
        newState: action.new_state,
        chain,
        chainError,
      });
      // The queue is a view over pipeline decisions, so it is re-read
      // rather than patched locally — the screen should never show a
      // state the backend does not actually hold.
      load(batchId);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusyId(null);
    }
  }

  useEffect(() => {
    if (receipt && receiptRef.current) receiptRef.current.focus();
  }, [receipt]);

  const items = useMemo(() => queue?.items || [], [queue]);
  const counts = useMemo(() => {
    const out = {};
    for (const g of ALL_GROUPS) out[g.key] = items.filter(g.match).length;
    return out;
  }, [items]);
  const activeGroup = useMemo(
    () => ALL_GROUPS.find((x) => x.key === group) || ALL_GROUPS[0],
    [group]
  );
  const shown = useMemo(() => items.filter(activeGroup.match), [items, activeGroup]);
  // A chip with nothing behind it is noise; it is hidden rather than shown
  // as a zero. "All open" always stands, so the row is never empty.
  const visibleGroups = useMemo(
    () => ALL_GROUPS.filter((g) => g.key === "ALL" || (counts[g.key] || 0) > 0),
    [counts]
  );

  const summary = queue?.summary;
  // The one number the header and the list must agree on. The summary
  // counts every open record in the run; the list holds a page. Both are
  // true, and showing them side by side with no explanation is the bug —
  // so the gap is named, and closing it is one click.
  const openTotal =
    typeof queue?.total === "number" ? queue.total : summary ? summary.open_count : null;
  const loaded = items.length;
  const queueIsPartial = openTotal != null && loaded < openTotal;

  /**
   * The three biggest exception types, counted over the OPEN records.
   *
   * The backend's `by_exception_type` counts every escalated record in the
   * run, actioned ones included, so it stops moving the moment an operator
   * starts working — "Open items 74" beside "Amount mismatch 24" when only
   * 21 amount mismatches are still open is the same contradiction in a
   * quieter place. These are counted from the records the queue actually
   * returned, so they fall as the queue is worked.
   */
  const topExceptions = useMemo(() => {
    const tally = new Map();
    for (const item of items) {
      const key = item.exception_type || "UNCLASSIFIED";
      const entry = tally.get(key) || { count: 0, amount_minor: 0 };
      entry.count += 1;
      entry.amount_minor += Number(item.merchant?.amount_minor || 0);
      tally.set(key, entry);
    }
    return [...tally.entries()].sort((a, b) => b[1].count - a[1].count).slice(0, 3);
  }, [items]);

  // Actioning the last record in a group empties it, and the chip is then
  // hidden — leaving the screen filtered to a group that is no longer
  // offered. Fall back to the whole queue rather than to a blank list.
  // Declared after `counts`: reading it above the memo that defines it is
  // a temporal-dead-zone error, which React renders as a blank page.
  useEffect(() => {
    if (group !== "ALL" && (counts[group] || 0) === 0) setGroup("ALL");
  }, [group, counts]);
  // The run the queue actually answered for always appears, even where it
  // processed nothing: a select whose value is absent from its options
  // silently displays some other run, which is a lie about what is on
  // screen.
  const reviewable = useMemo(() => {
    const withWork = runs.filter((r) => (r.total_records || 0) > 0);
    const current = queue?.batch_id;
    if (current && !withWork.some((r) => r.batch_id === current)) {
      const known = runs.find((r) => r.batch_id === current);
      return [known || { batch_id: current, label: "current run" }, ...withWork];
    }
    return withWork;
  }, [runs, queue]);

  return (
    <motion.div className="page" {...pageTransition}>
      <div className="page-header pn-head">
        <h1 className="page-title">Review queue</h1>
        <p className="pn-lede">
          Records the engine would not decide on its own, worst first. Every action taken here is
          written to the same hash-chained ledger as the automated decisions, alongside the reason
          the automation escalated in the first place.
        </p>
      </div>

      {error && (
        <p className="pn-note pn-note-bad" role="alert">
          Could not load the queue: {error}
        </p>
      )}

      {receipt && (
        <div
          className={"pn-note " + (receipt.chain && receipt.chain.intact ? "pn-note-ok" : "pn-note-warn")}
          role="status"
          tabIndex={-1}
          ref={receiptRef}
        >
          <strong>
            {receipt.recordId} — {receipt.label.toLowerCase()} recorded.
          </strong>{" "}
          {receipt.chain && receipt.chain.intact ? (
            <>
              Written to the audit ledger as a human review action; the chain re-verified over{" "}
              {count(receipt.chain.total_events)} events with no breaks.
            </>
          ) : receipt.chain ? (
            <>
              The action was written, but chain verification reported{" "}
              {count(receipt.chain.breaks ? receipt.chain.breaks.length : 0)} break(s). Do not treat
              this ledger as evidence until that is resolved.
            </>
          ) : (
            <>
              The action was written. The chain could not be re-verified just now
              {receipt.chainError ? ` (${receipt.chainError})` : ""}.
            </>
          )}{" "}
          <Link to={`/app/audit?record=${encodeURIComponent(receipt.recordId)}`}>
            See it in the audit trail
          </Link>
        </div>
      )}

      {summary && (
        <div className="pn-rail">
          <div className="pn-metric">
            <span className="pn-metric-label">Open items</span>
            <span className="pn-metric-value">{count(summary.open_count)}</span>
          </div>
          <div className="pn-metric">
            <span className="pn-metric-label">Value awaiting review</span>
            <span className="pn-metric-value">
              {money(summary.open_amount_minor || 0, { decimals: 0 })}
            </span>
          </div>
          {topExceptions.map(([type, stats]) => (
            <div className="pn-metric" key={type}>
              <span className="pn-metric-label">{humanise(type)}</span>
              <span className="pn-metric-value pn-metric-value-quiet">{count(stats.count)}</span>
              <span className="pn-metric-note">
                {money(stats.amount_minor || 0, { decimals: 0 })} ·{" "}
                {queueIsPartial ? `of ${count(loaded)} loaded` : "open"}
              </span>
            </div>
          ))}
        </div>
      )}

      <div className="pn-toolbar">
        <div className="pn-chips" role="group" aria-label="Filter the queue">
          {visibleGroups.map((g) => (
            <button
              key={g.key}
              type="button"
              className="pn-chip"
              aria-pressed={group === g.key}
              title={g.help}
              onClick={() => setGroup(g.key)}
            >
              {g.label}
              <span className="pn-chip-count">{count(counts[g.key] || 0)}</span>
            </button>
          ))}
        </div>
        <span className="pn-spacer" />
        {reviewable.length > 1 && (
          <span className="pn-field">
            <label className="pn-field-label" htmlFor="pn-run">
              Run
            </label>
            <select
              id="pn-run"
              className="pn-select"
              value={batchId || queue?.batch_id || ""}
              onChange={(e) => {
                setQueue(null);
                setEvidenceOpen(null);
                setBatchId(e.target.value);
              }}
            >
              {reviewable.map((r) => (
                <option key={r.batch_id} value={r.batch_id}>
                  {r.label} · {r.batch_id}
                </option>
              ))}
            </select>
          </span>
        )}
      </div>

      {/* The chips are short by necessity; the sentence under them says
          what the selected group actually is, in the words an operator
          uses rather than the engine's exception vocabulary. */}
      <p className="pn-chip-help">{activeGroup.help}</p>

      {queue?.batch_id && (
        <div className="pn-export">
          <div className="pn-export-text">
            <span className="pn-export-title">Export this queue</span>
            <span className="pn-export-scope">
              All {openTotal == null ? "" : `${count(openTotal)} `}open records in this run, with
              the reason, the explanation, the source file and row, and the actions available on
              each. The download is the whole queue, not the chip selected above.
            </span>
          </div>
          <div className="pn-export-actions">
            <a
              className="pn-export-btn"
              href={reviewQueueExportUrl({ batchId: queue.batch_id, format: "csv" })}
              download
              title="Download the open review queue as a CSV file"
            >
              CSV
            </a>
            <a
              className="pn-export-btn pn-export-btn-strong"
              href={reviewQueueExportUrl({ batchId: queue.batch_id, format: "xlsx" })}
              download
              title="Download the open review queue as an Excel workbook"
            >
              Excel (XLSX)
            </a>
          </div>
        </div>
      )}

      <div className="pn-section">
        <div className="pn-section-head">
          <h2 className="pn-section-title">
            {queue === null
              ? "Loading"
              : group === "ALL"
              ? `${count(shown.length)} of ${count(openTotal ?? loaded)} open`
              : `${count(shown.length)} of ${count(openTotal ?? loaded)} open · ${activeGroup.label.toLowerCase()}`}
          </h2>
          {queue?.batch_id && <p className="pn-section-note">Run {queue.batch_id}</p>}
        </div>

        {/* Two true numbers that disagree read as a broken product. Where
            the list is short of the run, the scope is stated and closing
            the gap is one click — the header count and the list length are
            never left to contradict each other in silence. */}
        {queueIsPartial && (
          <p className="pn-note pn-note-warn" role="status">
            This list holds the first {count(loaded)} of {count(openTotal)} open records, worst
            first, so the chip counts above describe those {count(loaded)}.{" "}
            <button
              type="button"
              className="pn-linkbtn"
              disabled={loadingAll}
              onClick={() => {
                setLoadingAll(true);
                load(batchId, openTotal).finally(() => setLoadingAll(false));
              }}
            >
              {loadingAll ? "Loading…" : `Load all ${count(openTotal)}`}
            </button>
          </p>
        )}

        {queue !== null && items.length === 0 && (
          <p className="pn-empty">
            Nothing is waiting on a person in this run. Either the engine resolved everything
            deterministically, or every escalated record has already been actioned.
          </p>
        )}

        {queue !== null && items.length > 0 && shown.length === 0 && (
          <p className="pn-empty">No open item in this run matches that filter.</p>
        )}

        <ul className="pn-cases">
          <AnimatePresence initial={false}>
            {shown.map((item, i) => (
              <Case
                key={item.record_id}
                item={item}
                index={i}
                busy={busyId === item.record_id}
                evidenceOpen={evidenceOpen === item.record_id}
                investigating={investigating === item.record_id}
                batchId={queue.batch_id}
                onAct={act}
                onToggleEvidence={() =>
                  setEvidenceOpen(evidenceOpen === item.record_id ? null : item.record_id)
                }
                onToggleInvestigate={() =>
                  setInvestigating(investigating === item.record_id ? null : item.record_id)
                }
              />
            ))}
          </AnimatePresence>
        </ul>
      </div>
    </motion.div>
  );
}

/* ------------------------------------------------------------------ case */

function Case({
  item,
  index,
  busy,
  evidenceOpen,
  investigating,
  batchId,
  onAct,
  onToggleEvidence,
  onToggleInvestigate,
}) {
  const actions = item.available_actions || [];
  const canApprove = actions.some((a) => a.action === "APPROVE_MATCH");
  const columns = useMemo(() => evidenceColumns(item), [item]);
  const shownColumns = columns.slice(0, 2);
  const refused = (item.considered_candidates || []).filter(
    (c) => !shownColumns.some((s) => s.payment_id === c.payment_id)
  );
  const provenance = parseJson(item.provenance_json);
  const severity = (item.severity || "LOW").toLowerCase();
  // "Candidate" means a record that cleared the evidence bar. Where five
  // were retrieved and all five were refused, saying "five candidates"
  // would overstate what the engine is holding.
  const admitted = (item.candidates || []).length
    ? (item.candidates || []).length
    : (item.considered_candidates || []).filter((c) => c.admissible).length;
  const retrieved = columns.length;

  return (
    <motion.li
      /* No `layout` here. A layout animation re-measures with transforms,
         and a case whose evidence is expanded then paints outside its own
         box on any reflow — a resize, a full-page capture — laying the
         comparison table over the next three records. Removal is enough:
         AnimatePresence still collapses an actioned item on exit. */
      className="pn-case review-item"
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      /* Exit fades only. Declaring height/padding here made Motion seed
         those properties inline at mount, freezing the case at its
         collapsed height — the expanded evidence then painted straight
         over the next three records. */
      exit={{ opacity: 0, y: -6 }}
      transition={{ duration: DURATION.fast, ease: EASE, delay: listIndexDelay(index) }}
    >
      <div className="pn-case-head">
        <span className={`pn-sev severity pn-sev-${severity}`}>
          <span aria-hidden="true" className="pn-sev-dot" />
          {SEVERITY_LABEL[item.severity] || "Low"}
        </span>
        <h3 className="pn-case-title">{humanise(item.exception_type)}</h3>
        <span className="pn-case-id">{item.record_id}</span>
        <span className="pn-case-amount">
          {money(item.merchant?.amount_minor, { currency: item.merchant?.currency })}
        </span>
      </div>

      <div className="pn-case-tags">
        {provenance.ledger?.filename && (
          <span className="pn-tag">
            <span className="pn-tag-key">Ledger</span>
            <span className="pn-tag-val">
              {provenance.ledger.filename}
              {provenance.ledger.file_row != null ? ` row ${provenance.ledger.file_row}` : ""}
            </span>
          </span>
        )}
        {item.matched_payment_id ? (
          <span className="pn-tag">
            <span className="pn-tag-key">Matched</span>
            <span className="pn-tag-val">{item.matched_payment_id}</span>
          </span>
        ) : admitted > 0 ? (
          <span className="pn-tag-open">
            {admitted} candidate{admitted === 1 ? "" : "s"}, no confirmed match
          </span>
        ) : (
          retrieved > 0 && (
            <span className="pn-tag">
              <span className="pn-tag-key">Retrieved</span>
              <span className="pn-tag-val">
                {retrieved} record{retrieved === 1 ? "" : "s"}, none admitted
              </span>
            </span>
          )
        )}
        {item.ai_invoked ? (
          <span className="pn-tag">
            <span className="pn-tag-key">AI consulted</span>
            <span className="pn-tag-val">
              {item.ai_backend || "—"}
              {item.ai_confidence != null
                ? ` · confidence ${item.ai_confidence.toFixed(2)} of ${
                    item.policy_threshold != null ? item.policy_threshold.toFixed(2) : "—"
                  } required`
                : ""}
            </span>
          </span>
        ) : (
          <span className="pn-tag">
            <span className="pn-tag-key">Decided by</span>
            <span className="pn-tag-val">deterministic checks</span>
          </span>
        )}
      </div>

      <p className="pn-case-explain">{item.explanation}</p>
      <p className="pn-case-next">
        <span className="pn-eyebrow">Recommended</span>
        {item.recommended_action}
      </p>

      {!canApprove && <ApproveWithheld item={item} />}

      <div className="pn-actions">
        {actions.map((action) => (
          <button
            key={action.action}
            type="button"
            className={"pn-btn" + (action.action === "APPROVE_MATCH" ? " pn-btn-strong" : "")}
            disabled={busy}
            onClick={() => onAct(item, action)}
            title={action.description}
          >
            {action.label}
          </button>
        ))}
        <span className="pn-actions-rule" aria-hidden="true" />
        <button
          type="button"
          className="pn-btn pn-btn-quiet"
          aria-expanded={evidenceOpen}
          aria-controls={`pn-evidence-${item.record_id}`}
          onClick={onToggleEvidence}
        >
          {evidenceOpen ? "Hide evidence" : "Show evidence"}
        </button>
        <button
          type="button"
          className="pn-btn pn-btn-quiet"
          aria-expanded={investigating}
          aria-controls={`pn-investigate-${item.record_id}`}
          onClick={onToggleInvestigate}
        >
          {/* A show/hide, not an action. The investigation runs on open, so
              labelling this "Investigate" put a second, unfired-looking
              action next to results that already existed — which is what
              made the whole affordance unreadable. */}
          {investigating ? "Hide investigation" : "Show investigation"}
        </button>
      </div>

      <AnimatePresence initial={false}>
        {investigating && (
          <motion.div
            id={`pn-investigate-${item.record_id}`}
            {...expand}
            style={{ overflow: "hidden" }}
          >
            <Investigator recordId={item.record_id} batchId={batchId} record={item} autoFocus />
          </motion.div>
        )}
      </AnimatePresence>

      <AnimatePresence initial={false}>
        {evidenceOpen && (
          <motion.div
            id={`pn-evidence-${item.record_id}`}
            {...expand}
            style={{ overflow: "hidden" }}
          >
            <Evidence
              item={item}
              columns={shownColumns}
              totalColumns={columns.length}
              refused={refused}
              provenance={provenance}
            />
          </motion.div>
        )}
      </AnimatePresence>
    </motion.li>
  );
}

/**
 * Why "approve match" is missing.
 *
 * Read from the actions the backend returned, never asserted ahead of it.
 * The distinction matters to an operator: an amount dispute and a record
 * with nothing to approve are different problems with different next
 * steps, and collapsing them into one sentence would teach a false model.
 */
function ApproveWithheld({ item }) {
  const isMoney = MONEY_DISPUTES.has(item.exception_type);
  const hasAnyCandidate =
    Boolean(item.matched_payment_id) ||
    (item.candidates || []).length > 0 ||
    (item.considered_candidates || []).length > 0;
  return (
    <p className="pn-note pn-note-warn">
      <strong>Approve match is not offered here.</strong>{" "}
      {!hasAnyCandidate ? (
        <>Nothing was retrieved for this order, so there is no candidate to approve.</>
      ) : isMoney ? (
        <>
          The dispute is not about which settlement this is — the money itself disagrees — so
          reconciling it would sign off a figure already known to be wrong.
        </>
      ) : (
        <>
          The open question on this record is not which settlement it belongs to, so approving a
          match would not resolve it.
        </>
      )}
    </p>
  );
}

/* -------------------------------------------------------------- evidence */

function Evidence({ item, columns, totalColumns, refused, provenance }) {
  const rows = comparisonRows(item).filter(
    (r) => r.ledger != null || columns.some((c) => r.cell(c) != null)
  );
  const checks = item.checks || [];
  const settlementFile = provenance.settlement?.filename;

  return (
    <div className="pn-evidence">
      <h4 className="pn-evidence-title">Ledger record against each candidate</h4>

      {columns.length === 0 ? (
        <p className="pn-empty" style={{ padding: "8px 0 12px" }}>
          No settlement records were retrieved for this order, so there is nothing to place beside it.
        </p>
      ) : (
        <div className="pn-scroll">
          <table className="pn-compare">
            <caption className="sr-only">
              Field-by-field comparison of ledger record {item.record_id} against{" "}
              {columns.length} settlement candidate(s)
            </caption>
            <thead>
              <tr>
                <th scope="col">Field</th>
                <th scope="col">
                  <span className="pn-col-who">Ledger record</span>
                  <span className="pn-col-id">
                    {provenance.ledger?.filename || item.record_id}
                  </span>
                </th>
                {columns.map((c, i) => (
                  <th scope="col" key={c.payment_id}>
                    <span className="pn-col-who">
                      {c.payment_id === item.matched_payment_id
                        ? "Matched settlement"
                        : `Candidate ${i + 1}`}
                    </span>
                    <span className="pn-col-id">{c.payment_id}</span>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.label} className={r.settlementOnly ? "pn-row-only" : undefined}>
                  <th scope="row">{r.label}</th>
                  <td className={r.prose ? "pn-cell-prose" : "pn-cell-val"}>
                    {r.ledger == null ? "—" : r.ledger}
                  </td>
                  {columns.map((c) => {
                    const value = r.cell(c);
                    const same = value == null ? null : r.same(c);
                    return (
                      <td key={c.payment_id} className={r.prose ? "pn-cell-prose" : "pn-cell-val"}>
                        {value == null ? "—" : value}
                        {same === true && <span className="pn-flag pn-flag-same">same</span>}
                        {same === false && <span className="pn-flag pn-flag-diff">differs</span>}
                      </td>
                    );
                  })}
                </tr>
              ))}
              <tr>
                <th scope="row">Admitted as evidence</th>
                <td className="pn-cell-prose">—</td>
                {columns.map((c) => (
                  <td key={c.payment_id} className="pn-admit">
                    {c.meta ? (
                      <>
                        <span className={c.meta.admissible ? "pn-admit-yes" : "pn-admit-no"}>
                          {c.meta.admissible ? "Admitted" : "Refused"}
                        </span>
                        <span className="pn-admit-why">{c.meta.admissibility_reason}</span>
                        {c.meta.evidence_score != null && (
                          <span className="pn-admit-why">
                            evidence score {c.meta.evidence_score.toFixed(2)}
                          </span>
                        )}
                      </>
                    ) : (
                      <span className="pn-signal-none">not scored separately</span>
                    )}
                  </td>
                ))}
              </tr>
              <tr>
                <th scope="row">What agreed / what did not</th>
                <td className="pn-cell-prose">—</td>
                {columns.map((c) => (
                  <td key={c.payment_id}>
                    {c.meta && c.meta.supporting_signals?.length > 0 &&
                      c.meta.supporting_signals.map((s) => (
                        <span className="pn-signal pn-signal-for" key={s}>
                          Agrees: {s}
                        </span>
                      ))}
                    {c.meta && c.meta.contradicting_signals?.length > 0 &&
                      c.meta.contradicting_signals.map((s) => (
                        <span className="pn-signal pn-signal-against" key={s}>
                          Conflicts: {s}
                        </span>
                      ))}
                    {(!c.meta ||
                      ((c.meta.supporting_signals || []).length === 0 &&
                        (c.meta.contradicting_signals || []).length === 0)) && (
                      <span className="pn-signal-none">no signals recorded</span>
                    )}
                    {c.meta?.semantic_verdict && (
                      <span className="pn-signal">
                        Semantic verdict: {c.meta.semantic_verdict}
                        {c.meta.semantic_confidence != null
                          ? ` (${c.meta.semantic_confidence.toFixed(2)})`
                          : ""}
                      </span>
                    )}
                  </td>
                ))}
              </tr>
            </tbody>
          </table>
        </div>
      )}

      {totalColumns > columns.length && (
        <p className="pn-provenance">
          {totalColumns - columns.length} further retrieved record
          {totalColumns - columns.length === 1 ? " is" : "s are"} listed below rather than placed in
          a column.
        </p>
      )}

      {checks.length > 0 && (
        <div className="pn-sub">
          <h4 className="pn-sub-title">Checks the engine ran</h4>
          <div className="pn-scroll">
            <table className="pn-checks">
              <caption className="sr-only">
                Deterministic checks run against {item.record_id} and their results
              </caption>
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
                {checks.map((c) => (
                  <tr key={c.name}>
                    <th scope="row">{humanise(c.name)}</th>
                    <td>
                      <span className={`pn-result pn-result-${String(c.status).toLowerCase()}`}>
                        {c.status}
                      </span>
                    </td>
                    <td className="pn-cell-val">{c.expected || "—"}</td>
                    <td className="pn-cell-val">{c.observed || "—"}</td>
                    <td className="pn-cell-prose">{c.detail || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {refused.length > 0 && (
        <div className="pn-sub">
          <h4 className="pn-sub-title">Candidates considered, and why each was refused</h4>
          <ul className="pn-refused">
            {refused.map((c) => (
              <li key={c.payment_id}>
                <span className="pn-refused-id">{c.payment_id}</span>
                <span>{money(c.gross_amount_minor, { currency: item.merchant?.currency })}</span>
                <span>{c.admissibility_reason}</span>
                {c.contradicting_signals?.length > 0 && (
                  <span className="pn-signal-against">{c.contradicting_signals.join("; ")}</span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      <p className="pn-provenance">
        Automated reason: {item.reason}
        <br />
        Ledger evidence from <code>{provenance.ledger?.filename || "—"}</code>
        {provenance.ledger?.file_row != null ? ` row ${provenance.ledger.file_row}` : ""}
        {settlementFile ? (
          <>
            {" · settlement evidence from "}
            <code>{settlementFile}</code>
            {provenance.settlement?.file_row != null ? ` row ${provenance.settlement.file_row}` : ""}
          </>
        ) : null}
      </p>
    </div>
  );
}
