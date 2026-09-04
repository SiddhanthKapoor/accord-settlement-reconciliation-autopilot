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

| Configuration | Accuracy | Recall | Correct rejection | Wrong match | Calls / 1k |
|---|---:|---:|---:|---:|---:|
| **A** exact reference only | 60.8% | 21.7% | 100.0% | 0.0% | 0 |
| **B** + deterministic corroborated | 77.9% | 55.8% | 100.0% | 0.0% | 0 |
| **C** + heuristic semantic | 84.6% | **89.2%** | 80.0% | 0.0% | 267 |
| **D** + Gemini semantic | *not cleanly measurable — see below* | | | 0.0% | 267 |

C is the instructive one. The heuristic takes the highest recall in the
table by saying SAME too readily, and pays for it in rejection. Before
the admissibility work it scored 64.6% accuracy at 40% correct rejection,
failing every hard rejection outright; admissibility now catches those
coincidences before the heuristic sees them, which lifted it to 84.6% at
80%. It still fails `reference_core_collision` completely — shared digits
plus a matching amount is exactly the trap a fixed rule walks into.

**The Gemini configuration could not be measured cleanly.** The final run
reported 78.8% accuracy, which looks like the model losing to the
heuristic. It is not a measurement of the model:

```
Provider failures: 62 of 64 AI-invoked examples (97%)
p95 latency:       10,010 ms  (exactly the configured semantic timeout)
```

The API was throttling essentially every call, and a timed-out call
degrades to HUMAN_REVIEW, which scores as a miss. The benchmark now
counts provider failures separately so a degraded run is visibly
degraded rather than being read as evidence. The last clean measurement,
taken before quota exhaustion and before the admissibility change, was
**87.5% accuracy / 75.0% recall / 100% correct rejection** — stated as a
pre-change number, because that is what it is.

That failed run did measure something worth having. Under a 97% outage of
the one external dependency, the system produced **zero wrong matches**
and held **100% correct rejection**, with recall degrading to roughly the
deterministic baseline. The claim that the model can be removed without
the product becoming unsafe is measured here, not asserted.

`product_alias` is 0% in every configuration, and was investigated rather
than chased: that variation asserts two records are the same payment
while their references name *different* transactions. Matching them would
mean matching on amount and date alone — the exact behaviour that caused
the V2 regression. The system refuses, and shows the operator the
rejected record with "amount matches exactly, dated 0d apart" attached.

## Results

Three held-out evaluations, each run once, none used for tuning. Same
generator throughout — only the seed differs — so the generations stay
comparable.

| Metric | V1 | V2 | V3 |
|---|---:|---:|---:|
| Reconciliation accuracy | 97.7% | 94.8% | **100.0%** |
| Exception precision | 95.5% | 100.0% | 100.0% |
| Exception recall | 90.9% | 71.6% | **100.0%** |
| **False auto-reconciliation rate** | **0.0%** | **0.0%** | **0.0%** |
| False exception rate | 0.9% | 0.0% | 0.0% |
| Routed to human review | 3.6% | 7.2% | 2.0% |
| AI invocation rate | 7.1% | 5.2% | **0.0%** |

### Read the 100% as a finding about the dataset

A perfect score should raise suspicion. This one has a specific cause:
**the model was never called.** Deterministic tiers resolved every
record.

This generator embeds the order number in both the reference and the
description, so once identity evidence is used properly the identifier
core recovers every ambiguous case without semantics. The synthetic
dataset is saturated — it no longer discriminates between a good system
and a better one, and a V4 on the same generator would be uninformative.
The caveat is recorded inside `evaluations/v3/FROZEN.json` so it travels
with the number.

The honest measure of the semantic layer is the ambiguous-matching
benchmark, where references share nothing and wording differs.
Deterministic matching scores 77.9% there.

### Controlled comparison — same data, three engines

V1, V2 and V3 were measured on different records, so those numbers are
not directly comparable. `compare_engines.py --generations` removes the
confound by checking each pinned commit into a worktree and scoring all
three on the **same** V3 dataset:

| Metric | V1 engine | V2 engine | current |
|---|---:|---:|---:|
| Reconciliation accuracy | 98.9% | 96.7% | **100.0%** |
| Exception recall | 98.2% | 80.1% | **100.0%** |
| False auto-reconciliation rate | 0.0% | 0.0% | 0.0% |
| Routed to human review | 2.4% | 5.3% | 2.0% |

The V2 regression is fully recovered and V1 is beaten on every metric.
This is the stronger claim, because it isolates the code.

### What broke and what fixed it

V2's regression was not conservatism, it was a category error: an exact
amount collision was being treated as evidence of identity. In a
population of thousands, two unrelated payments sharing an amount is
ordinary, so a genuinely missing settlement surfaced a coincidence,
escalated it, and landed in review instead of being reported missing.

Retrieval and admissibility are now separate questions, and the
discriminator is negative evidence — when both sides name a transaction
and name different ones, that is a statement that they differ. Measured
on 312 development scenarios built for this failure class:

| | before | after |
|---|---:|---:|
| Overall | 85.3% | 100% |
| `absent` (no counterpart exists) | 62% | 100% |
| Wrong-record selections | 24 | 0 |
| Model calls per 1,000 | 417 | 0 |

Full account in [docs/ENGINEERING_FAILURES_AND_FIXES.md](docs/ENGINEERING_FAILURES_AND_FIXES.md).

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
python -m pytest tests/ -q                                   # 128 tests
python verify_evaluation_v1.py --rerun                       # V1, V2, V3 all reproduce
python evaluate.py --dataset holdout                         # held-out evaluation
python benchmark_matching.py                                 # tier ablation
python stress_test.py --sizes 1000 5000 10000 50000          # throughput, not accuracy

cd ../frontend
npm run build
node e2e/verify-ui.mjs                                       # 44 browser + a11y checks
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
  tests/                    128 tests
frontend/
  src/components/           console, record inspector, audit trail
  e2e/verify-ui.mjs         real-browser verification
docs/
  ENGINEERING_FAILURES_AND_FIXES.md
  EVALUATION_METHODOLOGY.md
  RAZORPAY_INTEGRATION.md
  MANUAL_QA.md
```
