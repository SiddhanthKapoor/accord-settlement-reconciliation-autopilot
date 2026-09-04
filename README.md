# Settlement Reconciliation Autopilot

Reconciles a merchant's own order records against Razorpay-style
settlement data. Deterministic matching handles everything a computer can
be certain about; a narrow, confidence-gated model call handles genuine
textual ambiguity; anything still unresolved goes to a human. Every
decision is written to a hash-chained audit trail with the evidence it
was made on.

## The problem

A merchant's order system records what it believes happened. Razorpay's
settlement data records what actually happened to the money, after fees,
tax, refunds, and settlement delay. The two disagree often enough that
finance teams reconcile them by hand: matching references that were
reformatted somewhere in the middle, recomputing fee arithmetic, chasing
settlements that never arrived, and guessing at duplicates.

Most of that work is mechanical. Some of it genuinely isn't. The point of
this system is to be honest about which is which.

## The three outcomes

Every record ends in exactly one of these, never a fourth "probably
fine" state:

- **RECONCILED** — both sides agree, deterministically, within policy tolerance.
- **EXCEPTION** — a known, certain problem: amount mismatch, broken fee/tax arithmetic, a missing settlement, an excessive delay, a refund that doesn't add up, a currency that doesn't match.
- **HUMAN_REVIEW** — real ambiguity: a duplicate reference that amount can't separate, a match the model resolved below the confidence threshold, or a model call that failed or timed out.

The confidence gate is enforced in `policy.py`, not left to the model to
respect. A match the model is not confident enough about cannot become
RECONCILED no matter how clean the rest of the arithmetic looks.

## Architecture

```
INGEST → NORMALIZE → MATCH → RESOLVE AMBIGUITY → APPLY POLICY
   → RECONCILED / EXCEPTION / HUMAN_REVIEW → AUDIT → EVALUATE
```

Matching runs in tiers, cheapest first:

1. **Exact** normalized-reference lookup. `ORD-58291`, `ord_58291` and `Ord58291 ` all normalize to the same key.
2. **Deterministic corroborated match.** Candidates are scored on amount agreement, shared reference core, date proximity, and IDF-weighted description similarity. A match resolves here — with no model call — only when two independent signals agree *and* the wording backs them up.
3. **Semantic.** Only what tier 2 couldn't settle. The model gets a structured comparison: both references, both descriptions, both amounts and dates, and the deterministic signals already computed. It returns SAME / DIFFERENT / AMBIGUOUS with a confidence, and `policy.py` decides what that's worth.

Anything falling through all three is a genuine absence, which is an
EXCEPTION rather than an ambiguity.

Why IDF weighting matters: settlement descriptions are template text.
Ranking on plain word overlap means shared boilerplate ("payment for
order customer checkout") outranks the real counterpart. That is not
hypothetical — it is the bug this system shipped with, and it is written
up in full in [docs/ENGINEERING_FAILURES_AND_FIXES.md](docs/ENGINEERING_FAILURES_AND_FIXES.md).

## Does the model actually earn its place?

Worth asking directly, because "we put an LLM in it" is not an
architecture. `benchmark_matching.py` runs the real decision path under
four configurations against a 240-example development benchmark, built
so that **amount alone cannot solve it** — half the true matches sit
beside a distractor with an identical amount, and half the non-matches
have a candidate whose amount matches exactly.

| Configuration | Accuracy | True-match recall | Correct rejection | Wrong match | Model calls / 1k |
|---|---:|---:|---:|---:|---:|
| **A** exact reference only | 60.8% | 21.7% | 100.0% | 0.0% | 0 |
| **B** + deterministic corroborated | 77.9% | 55.8% | 100.0% | 0.0% | 0 |
| **C** + heuristic semantic | 64.6% | **89.2%** | **40.0%** | 0.0% | 642 |
| **D** + Gemini semantic | **87.5%** | 75.0% | **100.0%** | 0.0% | 654 |

Read C and D together, because C is the trap. The heuristic gets the
highest recall in the table and it does it by saying SAME too easily:
correct rejection collapses to 40%, and it fails every hard rejection
outright — `sequential_orders` (adjacent order numbers, same customer,
same product, minutes apart, same amount) 0%, `reference_core_collision`
0%, `near_duplicate_different` 0%. On those three, Gemini scores 100%.

So the model buys +19.2 points of recall over deterministic-only while
holding correct rejection at 100% and wrong matches at zero. That is the
part a fixed rule set could not do here.

It is not free, and it is not uniformly better. Latency goes from
sub-millisecond to a p50 of ~1s (p95 sits at the 10s timeout ceiling,
meaning some calls do time out and correctly degrade to HUMAN_REVIEW),
and it costs ~654 calls per 1,000 records at this benchmark's difficulty
— which is far denser in ambiguity than the real distribution, where
model invocation runs around 5%. Converting calls to a currency figure
needs the account's pricing tier, so the call count is reported and the
cost is not invented.

The clearest failure is `product_alias`, 0% for every configuration
including Gemini: references share nothing, the product is recorded under
a synonym, and only amount and date link the two. Nothing in this system
currently gets those, and they are counted as failures rather than
excluded.

## Results

Two held-out evaluations, each run once, neither used for tuning.

**V1** is the original system, frozen at commit `86318d6`. Because the
generator itself changed during hardening, a seed no longer reproduces
it, so the dataset bytes, both reports, and a SHA256 for each are
archived in `backend/evaluations/v1/` and checked by
`verify_evaluation_v1.py --rerun`, which re-runs it at its pinned commit.

**V2** is the hardened system on a dataset generated afterwards with a
new seed. Same generator on purpose: an improved generator would have
made the two incomparable, since any movement could be the engine
improving or the test getting easier.

Both runs used the Gemini backend. V1: 999 records, seed 20260903.
V2: 1,001 records, seed 20260904.

| Metric | V1 | V2 | |
|---|---:|---:|---|
| Reconciliation accuracy | 97.7% | 94.8% | **−2.9** |
| Exception precision | 95.5% | 100.0% | +4.5 |
| Exception recall | 90.9% | 71.6% | **−19.3** |
| **False auto-reconciliation rate** | **0.0%** | **0.0%** | unchanged |
| False exception rate | 0.9% | 0.0% | −0.9 |
| Auto-reconciled | 80.7% | 80.7% | unchanged |
| Routed to human review | 3.6% | 7.2% | +3.6 |
| Flagged as exception | 15.7% | 12.1% | −3.6 |
| AI invocation rate | 7.1% | 5.2% | −1.9 |
| Model calls per 1,000 records | — | 52 | — |

**The hardened system scores worse on the headline number.** That is the
result, reported as it came out.

What actually changed is the system's disposition. It no longer produces
confident-but-sometimes-wrong EXCEPTIONs; it asks a human instead.
Exception precision reaches 100% and the false exception rate goes to
zero — V2 never wrongly flags a good record — while exception recall
falls 19 points because genuinely missing settlements now land in
HUMAN_REVIEW rather than EXCEPTION. Human review roughly doubles, from
3.6% to 7.2%. The safety number that matters most, false
auto-reconciliation, stays at 0.0% in both.

The mechanism is specific and traceable. The exact-amount index surfaces
a candidate for records that previously found none, so a missing
settlement whose amount coincidentally matches an unrelated record in a
4,800-record pool now gets escalated. The model is then asked about a
pair where the amount agrees exactly and nothing else does, and — as
instructed — prefers AMBIGUOUS to a confident wrong answer. `missing_settlement`
goes from 47 EXCEPTION / 13 HUMAN_REVIEW in V1 to 21 / 39 in V2, which
accounts for nearly the entire drop.

One thing did improve where V1 was weakest: `semantic_true_match` went
from 0 correct (7 wrongly flagged EXCEPTION) to 1 correct with the rest
in HUMAN_REVIEW. Wrong answers became deferred answers.

Because V1 and V2 are different records, `compare_engines.py` re-runs
both engines over *identical* V2 data on the deterministic backend. It
shows the same direction — accuracy −3.0, exception recall −18.3, safety
unchanged — so this is a property of the code change, not of the dataset.

The fix is visible: don't spend an escalation when an exact amount is the
only corroboration. It has deliberately not been made, because it was
suggested by a held-out result. See
[docs/ENGINEERING_FAILURES_AND_FIXES.md](docs/ENGINEERING_FAILURES_AND_FIXES.md) §15.

## What's real and what's generated

The Razorpay integration is real and it does not return any data. Both
halves of that were verified rather than assumed:

```
client.order.create({...})     -> order_TXwOqE2JuvpyeF, status 'created'   (works)
client.order.fetch(...)        -> full order returned                      (works)
client.settlement.all(...)     -> count 0, items []
client.settlement.report(...)  -> count 0, items []
client.payment.all(...)        -> count 0, items []
```

Credentials and the client are fine. What can't be produced is the
settlement side: a settlement exists only after a payment is captured
through the browser checkout flow *and* a real bank settlement cycle
runs. Neither is reachable from a server-side test-mode API call, so no
supported sandbox workflow yields representative settlement, fee or
adjustment records.

So the engine runs on generated data, and the boundary is explicit rather
than implied. `settlement_source.py` defines one interface with two
implementations, each reporting its own provenance; `GET /data-sources`
exposes it and the console renders it as a banner. Full probe and
reasoning in [docs/RAZORPAY_INTEGRATION.md](docs/RAZORPAY_INTEGRATION.md).

## Running it

Python 3.11+ and Node 18+.

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt

cd backend
python data/generate_dataset.py --seed 20260903 --total 5000
cp .env.example .env          # GEMINI_API_KEY and RAZORPAY_* are optional
uvicorn app.main:app --port 8000

# separate terminal
cd frontend && npm install && npm run dev   # http://localhost:5173
```

Without `GEMINI_API_KEY` the semantic step falls back to a deterministic
heuristic. That is a real fallback, not a stub — the ablation above
measures exactly what it costs you.

## Verifying it

```bash
cd backend
python -m pytest tests/ -q                                   # 86 tests
python verify_evaluation_v1.py --rerun                       # V1 still reproduces
python evaluate.py --dataset holdout                         # held-out evaluation
python benchmark_matching.py                                 # tier ablation
python stress_test.py --sizes 1000 5000 10000 50000          # throughput, not accuracy

cd ../frontend
npm run build
node e2e/verify-ui.mjs                                       # 22 browser checks
```

The browser check drives a real Chromium against the running stack. It
found three defects that every backend test passed through, including an
audit view rendering an empty feed next to a chain reporting 102 events.

## Scale

Deterministic backend, so this measures the pipeline rather than
Google's network latency.

| Records | Settlement pool | Wall clock | Throughput | p50 | p95 | p99 | Peak RSS |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1,000 | 960 | 0.10 s | 10,394/s | 0.010 ms | 0.97 ms | 1.43 ms | 42 MB |
| 5,000 | 4,800 | 1.00 s | 4,997/s | 0.010 ms | 2.19 ms | 3.05 ms | 94 MB |
| 10,000 | 9,600 | 2.13 s | 4,705/s | 0.010 ms | 2.33 ms | 3.20 ms | 158 MB |
| 50,000 | 48,000 | 11.43 s | 4,376/s | 0.010 ms | 2.38 ms | 3.32 ms | 664 MB |

| Step | Records | Time | |
|---|---|---|---|
| 1k → 5k | 5x | 10.6x | super-linear (below the scan bound) |
| 5k → 10k | 2x | 2.1x | linear |
| 10k → 50k | 5x | 5.4x | linear |

Candidate search used to be records × settlement population; 5,000
records took 2.18s before the amount and reference-core indexes and the
bounded window scan, and 0.99s after. The 664 MB at 50k is the ceiling
worth watching — a batch holds every record, the whole settlement
population, and every result in memory at once.

Read that step by step, not first-to-last: the smallest batch runs before
the window-scan bound engages, so a 1k→50k ratio overstates the growth.
Roughly flat throughput as the population grows is the real evidence.

This measures speed, not correctness. Processing 50,000 synthetic records
says nothing about 50,000 real reconciliations.

## Limitations

Stated plainly rather than buried:

- **No real merchant data has ever been processed.** Every accuracy number here is against synthetic records.
- **The fee model is simplified** — flat 2% plus 18% GST on the fee. Real MDR varies by payment method, merchant category, and negotiated rate. The arithmetic consistency check (`net = gross − fee − tax − refund`) generalises to any fee structure; the specific rate in the generator does not.
- **The generator's ambiguous cases are easier than real ones.** They embed the order number in both descriptions, so the identifier is usually recoverable. The 240-example benchmark exists to cover the harder variation, and its numbers are the more honest read on the semantic layer.
- **Policy thresholds are defaults, not calibrated** against any real risk tolerance. They're configurable, and every decision records which threshold applied.
- **The model-backed numbers are not byte-reproducible.** Temperature 0 is not a determinism guarantee across time.
- **One settlement can still be claimed by two merchant records.** Each record is judged alone and both can look reconciled. `detect_duplicate_claims` surfaces it; nothing resolves it yet. That's a real open problem, not an oversight.
- **This is not fraud detection.** It reconciles bookkeeping discrepancies and makes no attempt to be anything more.

## Repository layout

```
backend/
  app/
    domain/models.py        records, results, and PolicyConfig — every threshold in one place
    engine/
      normalize.py          reference/text/amount normalization, IDF weighting
      matching.py           tiered candidate resolution, indexes, enforced timeout
      semantic.py           the one place a model is used
      policy.py             the decision core; the confidence gate lives here
      batch.py              shared batch loop + cross-record collision detection
    ledger/                 SQLite + hash-chained audit log
    integrations/           real Razorpay client, and the synthetic/live boundary
    api/routes.py           batch runs, SSE progress, record detail, audit, provenance
  data/                     dataset + benchmark generators
  evaluations/v1/           frozen V1: dataset bytes, reports, checksums, pinned commit
  evaluate.py               held-out evaluation
  benchmark_matching.py     tier ablation
  stress_test.py            throughput evaluation
  verify_evaluation_v1.py   V1 integrity + re-run at pinned commit
  tests/                    86 tests
frontend/
  src/components/           console, record inspector, audit trail
  e2e/verify-ui.mjs         real-browser verification
docs/
  ENGINEERING_FAILURES_AND_FIXES.md
  EVALUATION_METHODOLOGY.md
  RAZORPAY_INTEGRATION.md
  MANUAL_QA.md
```
