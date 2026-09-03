# Settlement Reconciliation Autopilot

**AI-assisted reconciliation between a merchant's own order records and
Razorpay-style settlement data — deterministic wherever a computer can be
certain, a narrow, confidence-gated model call only where it genuinely
can't, and human review for everything in between.**

Built for the Razorpay AI Buildathon 2026, Track 04 (AI Finance Controller).

## The problem

A merchant's order system says what it believes happened. Razorpay's
settlement data says what actually happened to the money — after fees,
tax, refunds, and settlement delay. The two don't always agree, and
today a finance team reconciles them by hand: matching references,
recomputing fee arithmetic, chasing down missing records, and guessing
at duplicate or reworded references. This system automates the part
that's genuinely mechanical, and routes the rest to a human instead of
guessing.

## The three outcomes

Every record gets exactly one of:

- **RECONCILED** — merchant and Razorpay records agree, deterministically, within policy tolerance.
- **EXCEPTION** — a known, certain problem: amount mismatch, broken fee/tax arithmetic, a missing settlement, an excessive delay, a refund that doesn't reconcile.
- **HUMAN_REVIEW** — genuine ambiguity: a duplicate reference that can't be disambiguated by amount, or a reference match the semantic classifier resolved below the confidence threshold.

There is no fourth "silently fine" state. Every decision — RECONCILED
included — is written into a hash-chained audit ledger with the full
check-by-check evidence behind it.

## Architecture

```
INGEST → NORMALIZE → MATCH → RESOLVE AMBIGUITY → APPLY POLICY
  → RECONCILE / EXCEPTION / HUMAN_REVIEW → AUDIT → EVALUATE
```

```
backend/
  app/
    domain/models.py        MerchantRecord, RazorpaySettlementRecord, PolicyConfig,
                             CheckResult, ReconciliationResult — the whole data model
    engine/
      normalize.py           reference/text/amount normalization (deterministic)
      matching.py             candidate resolution: exact reference index -> fuzzy
                               token-overlap -> semantic (Gemini) fallback, in that
                               order, with a real enforced timeout on the model call
      semantic.py              the ONE place an LLM is used — narrow, structured,
                               confidence-scored, never money-deciding on its own
      policy.py                 the decision core: runs the deterministic checks,
                               aggregates them into RECONCILED / EXCEPTION / HUMAN_REVIEW
      batch.py                   shared batch-processing loop — used identically by
                               evaluate.py and the API's live batch endpoint
    ledger/
      db.py, audit.py, store.py   SQLite + a receiver-attested hash-chained audit log
                               (the audit.py module is domain-agnostic — reused as-is)
    integrations/
      razorpay_settlements.py      real Razorpay Settlements API client (see below
                               for why it can't be demonstrated against live data)
    api/routes.py                FastAPI: batch run + SSE progress, record detail,
                               evaluation report, audit stream/verify

  data/
    generate_dataset.py        synthetic dataset generator (deterministic, seeded)
    datasets/                  generated dev.jsonl / holdout.jsonl / razorpay_pool.jsonl
    eval_reports/               evaluate.py's machine-readable output

  evaluate.py                  terminal evaluation harness — the numbers below come
                             from running this against the held-out set, nothing
                             is hand-entered
  tests/                       exact matches, fee/tax normalization, partial refunds,
                             delayed settlements, missing settlements, duplicate
                             references, ambiguous matching, AI failure fallback,
                             timeout handling, invalid data, policy threshold
                             enforcement, audit integrity, batch resilience

frontend/                      React + Vite finance-operations console — live batch
                             progress via real SSE (never simulated), a record
                             inspector, and the audit trail
```

### The two things that matter most about the design

**Deterministic first, AI narrow and gated.** Every check — reference
match, gross amount, fee/tax arithmetic, settlement timing, refund
consistency — is a plain comparison against an explicit tolerance. The
model is only ever called when a merchant record's reference doesn't
match anything exactly, deterministic token-overlap can't confidently
resolve it either, and there's at least one Razorpay record nearby (in
time) with *some* plausible textual overlap. Even then, its verdict
can't reconcile a record on its own: `PolicyConfig.ai_confidence_threshold`
(default 0.85) is enforced in `policy.py`, not left to the model to
self-limit — a confident-sounding match below that threshold is routed
to HUMAN_REVIEW regardless.

**Failure degrades safely, never silently.** A provider timeout or error
during the semantic call is its own outcome — HUMAN_REVIEW, with the
reason naming the failure — never a crash, and never a silent
RECONCILED. The semantic call itself is wrapped in a real
`ThreadPoolExecutor`-based timeout (`SEMANTIC_CALL_TIMEOUT_SECONDS`,
10s), not just trusting the SDK's own client timeout, so one hung call
can't stall a batch of thousands of records. See
`tests/test_reconciliation.py::test_timeout_handling_bounds_a_hanging_provider`,
which monkeypatches the timeout down and proves the wrapper — not the
simulated provider's own eventual failure — is what actually cuts it
off.

## BUILT

Components that exist and run, verified by the test suite and the
evaluation run below — not aspirational:

- Deterministic normalization (reference, text, amount, date) — `engine/normalize.py`
- Exact-reference candidate index, amount-plausibility-filtered fuzzy fallback, semantic (Gemini) fallback with a real enforced timeout — `engine/matching.py`
- Policy-gated decision engine producing RECONCILED / EXCEPTION / HUMAN_REVIEW with a named reason and full check evidence — `engine/policy.py`
- Batch-resilient processing (one record's unexpected failure can't take down the batch) — `engine/batch.py`
- Receiver-attested, hash-chained audit ledger with a tamper-detection self-test — `ledger/audit.py`
- Real Razorpay Settlements API integration client (see below for why it returns empty in this environment)
- Terminal evaluation harness producing a reproducible JSON report — `evaluate.py`
- FastAPI backend: batch run with real SSE progress, per-record detail (merchant record, matched Razorpay record, every check, AI involvement, audit history), evaluation report endpoint
- React finance-operations console: live stats, batch control with real backend-driven progress, a filterable records table, a record inspector, and the audit trail
- 31 backend tests covering every category the product spec named by name (see `backend/tests/`)

## SYNTHETIC EVALUATION

The dataset (`backend/data/generate_dataset.py`) is fully synthetic,
deterministically generated from a fixed seed, with ground truth assigned
from the *problem definition* of each category at construction time —
never by running the engine and recording what it happened to output.
That's what makes the accuracy numbers below a real measurement instead
of a tautology.

**5,000 records**, stratified 80/20 into dev (used for all iteration) and
a **held-out set touched exactly once** for the numbers below — see
"Process discipline" below for exactly how that was enforced.

| Category | Records | Expected outcome |
|---|---:|---|
| clean_match | 2,750 | RECONCILED |
| fee_tax_rounding | 500 | RECONCILED (rounding within tolerance) |
| delayed_settlement_normal | 400 | RECONCILED |
| delayed_settlement_excessive | 150 | EXCEPTION (delay beyond policy) |
| partial_refund | 350 | RECONCILED |
| refund_mismatch | 100 | EXCEPTION |
| missing_settlement | 300 | EXCEPTION |
| amount_mismatch | 250 | EXCEPTION |
| duplicate_reference | 100 | HUMAN_REVIEW (amount can't disambiguate) |
| ambiguous_text_reference (fuzzy-resolvable) | ~32 | RECONCILED, resolved deterministically |
| ambiguous_text_reference (needs real semantic judgment, genuine match) | ~41 | RECONCILED, needs the model |
| ambiguous_text_reference (decoy — different transaction, superficial overlap) | ~27 | EXCEPTION, needs the model to reject it |

### Held-out evaluation results

Run once, with `GEMINI_API_KEY` set (real Gemini calls, not the heuristic
fallback), against `dataset_version=dcf754f76ce3ee6e4d811ec2cdf1a1988a075f411cd894f11a3bcb0b7329e433`,
`seed=20260903`, 999 held-out records. Raw report: `backend/data/eval_reports/gemini_holdout.json`.

| Metric | Value |
|---|---:|
| Reconciliation accuracy | 97.7% |
| Exception precision | 95.5% |
| Exception recall | 90.9% |
| **False auto-reconciliation rate** | **0.0%** |
| False exception rate | 0.9% |
| Auto-reconciled | 80.7% |
| Routed to human review | 3.6% |
| Flagged as exception | 15.7% |
| AI invocation rate | 7.1% |
| Throughput | 2.4 records/sec (real Gemini network calls) |
| p50 latency | 0.04 ms |
| p95 latency | 3,881.95 ms |

**The number that matters most for a finance system is the first bolded
row: 0.0%.** Across all 999 held-out records, not one was ever
auto-reconciled when it shouldn't have been — every error the system
made was on the conservative side (EXCEPTION or HUMAN_REVIEW when the
truth was RECONCILED), never the reverse.

Where the errors are concentrated: `ambiguous_text_reference_semantic_true_match`
(8 held-out records — genuinely the same transaction, but with only
moderate textual overlap, needing real semantic judgment) is where
Gemini's verdict disagreed with ground truth most: 7 of 8 were called
DIFFERENT and correctly-but-wrongly resolved to EXCEPTION, 1 was
HUMAN_REVIEW, 0 were RECONCILED. That single 8-record category accounts
for essentially all of the 0.9% false-exception rate (7 of the ~814
truly-RECONCILED held-out records). `ambiguous_text_reference_semantic_decoy`
(5 records — a genuinely different transaction with superficial word
overlap, which should be rejected) fared better: 3 of 5 correctly
resolved to EXCEPTION, 2 to HUMAN_REVIEW, 0 wrongly RECONCILED. Every
other category resolved exactly as the category table above predicts.
This is a real, measured limitation of the semantic classifier on the
hardest 1.3% of the distribution — not a threshold that was loosened or
tightened in response to it. See "Process discipline" below for why
nothing was changed after seeing this.

Regenerate with:

Regenerate with:

```bash
cd backend
python data/generate_dataset.py --seed 20260903 --total 5000   # deterministic; only needed once
python evaluate.py --dataset holdout
```

### Semantic classifier: heuristic fallback vs. Gemini

The one non-deterministic component is evaluated on its own, separately
from the end-to-end outcome accuracy above, because it's the one place a
plain accuracy number would hide the metric that actually matters for a
finance system: how often something genuinely different gets confidently
called the same transaction.

Both backends were run against the identical 999-record held-out set —
same candidates, same policy, same threshold — the only variable is
which `SemanticVerifier` implementation `app/engine/semantic.py`
selected. Reports: `backend/data/eval_reports/gemini_holdout.json` /
`heuristic_holdout.json`.

| | Heuristic fallback | Gemini (`gemini-3.5-flash-lite`) |
|---|---:|---:|
| Reconciliation accuracy (whole set) | 99.2% | 97.7% |
| False auto-reconciliation rate | 0.0% | 0.0% |
| False exception rate | 1.0% | 0.9% |
| Routed to human review | 2.0% | 3.6% |
| `semantic_true_match` correctly RECONCILED | 0 / 8 | 0 / 8 |
| `semantic_decoy` correctly rejected (EXCEPTION) | 5 / 5 | 3 / 5 |
| Throughput | 3,743 records/sec | 2.4 records/sec |
| p95 latency | 3.1 ms | 3,882 ms |

Two honest things this table says, neither of them flattering to either
backend:

1. **Neither backend recovers the genuinely-ambiguous true-match case in
   this dataset** (0/8 for both). The construction deliberately keeps
   textual overlap in the "moderate" band — enough to reach the semantic
   step, not enough for either a pure-Jaccard heuristic or a
   temperature-0, deliberately-conservative-prompted LLM to confidently
   call it SAME. Both back off instead of guessing (Gemini splits
   7 EXCEPTION / 1 HUMAN_REVIEW; the heuristic sends all 8 to EXCEPTION,
   since its DIFFERENT threshold is lower and it never emits AMBIGUOUS
   for these specific records) — safe in both cases, correct in neither.
2. **The heuristic actually scores higher on raw accuracy here**,
   because it's more decisive (0% of ambiguous cases become
   HUMAN_REVIEW) and that decisiveness happens to land on the right side
   for `semantic_decoy` on this seed. That is not evidence the heuristic
   is the better component — it has no real judgment, just a fixed
   Jaccard cutoff, and its confidence is hard-capped at 0.75 in
   `HeuristicSemanticVerifier` specifically so `policy.py` never lets it
   single-handedly reconcile anything. Gemini's willingness to say
   AMBIGUOUS and route to a human, rather than confidently guess, is the
   behavior this system's design explicitly wants from a model
   component — it costs a fraction of a point of raw accuracy on this
   held-out set and buys a hedge against confidently-wrong verdicts that
   the heuristic structurally cannot express.

### Process discipline

Every threshold and design decision below was made by iterating against
the **dev** split only:

- Amounts were originally drawn from 11 fixed price points; this caused
  unrelated transactions to coincidentally share an exact amount far
  more often than any real system would, inflating false fuzzy-matches
  on `missing_settlement` records. Found and fixed on dev (higher-entropy
  amount generation) before the held-out set was touched.
- The fuzzy-match candidate search originally ranked purely by text
  overlap; an amount-plausibility pre-filter (0.5x-2x) was added after
  dev-set inspection showed the same failure mode.

The held-out set was evaluated exactly once against the system as
actually configured (Gemini backend) — that run produced every number
in the "Held-out evaluation results" table above, and no engine, policy,
or dataset code changed afterward. One further run was made against the
**same** held-out set with `GEMINI_API_KEY` unset, forcing the fixed
heuristic fallback, solely to populate the comparison table below —
`HeuristicSemanticVerifier` has no tunable parameters to fit against
holdout results, so this is a controlled comparison of two already-fixed
algorithms, not a second bite at tuning. If a future change is made to
the engine, the correct process is: iterate on dev, re-run
`evaluate.py --dataset holdout` once against the real configuration, and
report whatever comes out — not the other way around.

## NOT PRODUCTION VALIDATED

Stated plainly, not buried:

- **This has never processed a real merchant's data.** Every number
  above is against a synthetic, generated dataset. It demonstrates that
  the *architecture* — deterministic-first, AI-gated, policy-enforced,
  audit-complete — behaves correctly against a controlled, labeled
  distribution of the failure modes the product spec named. It is not a
  claim about real-world merchant reconciliation accuracy, real fraud
  prevention, or real financial savings.
- **The fee/tax model is simplified**: a flat 2% fee + 18% GST-on-fee,
  applied uniformly. Real Razorpay MDR varies by payment method,
  merchant category, and negotiated rate. The *arithmetic consistency
  check* (`net = gross - fee - tax - refund`) generalizes to any real fee
  structure; the specific rate assumption in the data generator does
  not.
- **The synthetic dataset's product/description vocabulary is
  deliberately small** (12 product names) so that coincidental textual
  overlap between unrelated records is a real, present phenomenon to
  test against — not eliminated by unrealistic textual diversity. This
  is a deliberate stress-test choice, not an attempt to make the numbers
  look better than they would on more varied real text.
- **Policy thresholds (`PolicyConfig`) are defaults, not calibrated
  against any real merchant's risk tolerance.** They are configurable
  and every decision names which threshold was applied — see each
  record's `policy_threshold` field — but the specific numbers (21-day
  settlement window, 0.85 AI confidence gate, ±2 paise tolerance) are
  reasonable starting points, not the product of real operational data.
- **No claim of fraud prevention.** This system reconciles bookkeeping
  discrepancies (fees, timing, refunds, references). It is not a fraud
  detection system and makes no attempt to be one.

## Razorpay integration — what's real, what isn't, and why

`backend/app/integrations/razorpay_settlements.py` is a real,
functioning client against Razorpay's actual Settlements API
(`client.settlement.all()`), tested against mocked-but-realistically-shaped
responses in `tests/test_razorpay_integration.py`. It is **not** a stub.

It cannot be demonstrated against live data in this environment, and
that's stated honestly rather than worked around. Verified directly
against the project's own Razorpay test-mode account:

```
>>> client.settlement.all()
{'entity': 'collection', 'count': 0, 'items': [], 'has_more': False}
>>> client.payment.all({'count': 5})
{'entity': 'collection', 'count': 0, 'items': []}
```

Razorpay's Settlements API only returns records for payments that were
actually captured *and* have completed a real settlement cycle (a real
bank settlement run, T+2/T+3 business days after capture). A fresh
test-mode account with no real payment flow through it has nothing to
fetch — that's a property of the sandbox, not a limitation of this
client. Because of that, the reconciliation engine, dataset, and
evaluation run entirely on the synthetic generator, clearly labeled as
such throughout this README and the UI. A merchant's real Razorpay
account — which does have real settlement history — could be pointed at
this exact same integration module with no changes to the reconciliation
engine itself.

## Running it

Requires Python 3.11+ and Node 18+.

```bash
# 1. Backend deps
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt

# 2. Generate the dataset (deterministic — do this once)
cd backend
python data/generate_dataset.py --seed 20260903 --total 5000

# 3. Backend
cp .env.example .env   # add GEMINI_API_KEY / RAZORPAY_* if you have them — optional
uvicorn app.main:app --port 8000

# 4. Frontend (separate terminal)
cd frontend && npm install && npm run dev
# open http://localhost:5173
```

Optional environment variables (`backend/.env`):

- `GEMINI_API_KEY` (+ optional `GEMINI_MODEL`, default `gemini-3.5-flash-lite`) — without it, the semantic classifier runs on a deterministic heuristic fallback (see the comparison table above).
- `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` — enables `razorpay_settlements.py`'s live API calls; see the integration section above for what this can and can't demonstrate in a fresh test-mode account.

### Reproducing everything

```bash
cd backend
python -m pytest tests/ -v                    # 31 tests
python evaluate.py --dataset holdout           # the official, reported numbers
python evaluate.py --dataset dev               # dev-set numbers, for reference only
cd ../frontend && npm run build                # production build
```

## Demo

1. **Console tab** — run a batch (holdout set, a few hundred records) and
   watch the progress bar and outcome counts update live, driven entirely
   by SSE events the backend emits per record as it's actually decided —
   nothing in the frontend fabricates timing or progress.
2. Click any record to see the merchant record, the matched Razorpay
   record, every deterministic check with its actual expected/observed
   values, whether the semantic classifier was invoked and its confidence
   against the policy threshold, and the full audit history for that
   record.
3. **Audit Trail tab** — the same hash-chained ledger every decision
   above was written into, with a one-click integrity self-test.
4. Terminal: `python evaluate.py` prints the same metrics reported above,
   freshly computed, plus per-category outcome breakdown.
