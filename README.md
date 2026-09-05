# Axiom Recon

**AI that closes the gap between payments, settlements and books.**

Upload a gateway export, a bank statement and an order book. The system
matches what it can prove deterministically, asks a language model only
about records where identity is genuinely ambiguous, refuses to book a
match the evidence does not support, and sends what remains to a person
with the evidence attached.

---

## 1. The problem

No business has one source of truth. The gateway says one thing, the bank
statement says another, the accounting export uses different identifiers,
and the order book has descriptions written for humans. Settlements
arrive late, bundled, partially refunded, or not at all.

Most of the matching is mechanical. A meaningful minority is not:

```
Order book     INV-2057  | Annual cloud platform renewal - Northwind Retail | ₹31,200
Bank statement UTR774120 | NEFT INWARD CLDPLTFRM RENEWAL NORTHWND           | ₹31,200
```

Nothing links those two except the amount, the date, and what the words
mean. And an amount is not evidence — elsewhere in the same statement:

```
Order book     INV-2058  | Gift card purchase                 | ₹5,000
Bank statement UTR774090 | UPI/COLLECT/MEDIQUIP SUPPLIES/9921 | ₹5,000
```

Identical amounts, unrelated transactions. A system that matches on
amount books the wrong one.

## 2. The product

A reconciliation run takes two or more CSVs, maps them to a common shape,
and produces one of three outcomes per record:

| Outcome | Meaning |
|---|---|
| **RECONCILED** | Both sides agree, deterministically, within policy tolerance |
| **EXCEPTION** | A known, certain problem — amount, currency, fee arithmetic, missing settlement |
| **HUMAN_REVIEW** | Genuine ambiguity — the system will not guess |

Every decision carries the candidates it considered, the signals for and
against each, and a plain-English explanation built from those signals.
Every decision, and every human override, is written to a hash-chained
audit ledger.

## 3. Architecture

```
CSV upload → schema detection → column mapping (confirmed by the user)
   → canonical records
      → tier 1  exact reference match
      → tier 2  deterministic corroboration (amount, identifier, date, IDF text)
      → tier 3  Gemini semantic comparison, for the residual only
         → policy gate (deterministic, authoritative)
            → RECONCILED / EXCEPTION / HUMAN_REVIEW
               → claim integrity + aggregation detection across the batch
                  → hash-chained audit
```

Retrieval and evidence are separate questions. The indexes are tuned for
recall and will return a settlement sharing nothing with a record but an
amount; **admissibility** then decides whether a retrieved record is
evidence of anything at all.

## 4. Why deterministic logic alone is not enough

Measured on the evaluation dataset's development split, deterministic
matching alone scores **82.4%**, and on the two categories where identity
evidence is withheld it scores **zero**:

| Category | Deterministic |
|---|---:|
| `bank_narration_match` (truncated narration, UTR namespace) | 0 / 210 reconciled |
| `merchant_alias_match` (legal entity vs trading name) | 0 / 150 reconciled |

No amount of rule-writing closes that. `CLDPLTFRM RENEWAL NORTHWND` and
`Annual cloud platform renewal - Northwind Retail` are the same payment,
and only reading them tells you so.

## 5. Where Gemini is used

One question, on one pair, with structured evidence attached:

> Given these two records and the deterministic signals already computed,
> are they representations of the same financial event?

It returns `SAME` / `DIFFERENT` / `AMBIGUOUS` with a confidence. It never
sees the database, never proposes candidates, and never writes an outcome.

Verified live on the demo data:

```
Annual cloud platform renewal - Northwind Retail
  vs  NEFT INWARD CLDPLTFRM RENEWAL NORTHWND    → SAME       0.95
Gift card purchase
  vs  UPI/COLLECT/MEDIQUIP SUPPLIES/9921        → DIFFERENT  0.95
```

Same amount in both cases. Deterministic logic resolves neither.

## 6. Why Gemini is not trusted with financial authority

`policy.py` is authoritative and the model cannot override it. A
confident `SAME` cannot produce RECONCILED when:

- the currencies differ
- the amounts differ beyond tolerance
- the fee/tax arithmetic does not reconcile
- the settlement is outside the allowed window
- another record has a stronger claim on the same settlement
- its own confidence is below `ai_confidence_threshold` (0.85)

The practical test: with `enable_semantic_matching=False` the system
still runs and still reconciles the majority. It loses recall, not
safety. A component you can remove without the product becoming unsafe
was never holding the safety property.

**Measured under a real outage.** During one benchmark run the API
rate-limited 62 of 64 calls (97%). The system produced **zero wrong
matches** and held **100% correct rejection**; recall degraded to roughly
the deterministic baseline. A timed-out call becomes HUMAN_REVIEW, never
a guess.

## 7. How data enters the system

1. Create a run
2. Drop CSVs onto the source type they belong to
3. Review detected columns — each shows a confidence and a reason
4. Correct any mapping the detector got wrong
5. Run; watch live progress streamed from the backend
6. Inspect results, work the review queue, export

Detection reports uncertainty rather than guessing, and a run will not
start while a required column is unmapped.

## 8. Supported source formats

| Source type | Role | Notes |
|---|---|---|
| Payment Gateway | Settlement | Payout / settlement exports |
| Bank Statement | Settlement | Split debit/credit columns, value dates, truncated narration |
| Orders / Invoices | Ledger | Order books |
| Accounting / ERP | Ledger | GL exports |
| Other CSV | Ledger | Anything else |

Handles comma/semicolon/tab delimiters, BOM, `₹1,234.56`, `(45.00)` for
negatives, ten date formats, minor-vs-major amount scale, and separate
debit/credit columns.

Razorpay-format exports work as one source. **Razorpay is not required** —
the product reconciles a bank statement against an accounting export with
no gateway involved.

## 9. Evaluation methodology

Ground truth is assigned from each category's construction, never by
running the engine — a dataset labelled by the system under test can only
report 100%.

Development and holdout are split before any tuning. All diagnosis
happened on the dev split; the holdout was evaluated once.

**Why this dataset exists.** The previous one was saturated: it embedded
the order number in both the reference and the description, so once
identity evidence was used properly everything resolved deterministically
and the model was never invoked. A dataset a deterministic system scores
100% on cannot measure anything above that system. This one withholds
identity evidence for 16% of records and keeps the traps that punish
naive matching — same amount different transaction, same amount same
date, adjacent invoice numbers at identical amounts, near duplicates.

Full detail: [docs/EVALUATION_METHODOLOGY.md](docs/EVALUATION_METHODOLOGY.md)

## 10. Final results

Held-out set: 1,000 records, seed 90210, evaluated once per
configuration. Frozen with dataset bytes and checksums in
`backend/evaluations/final/`.

| Metric | Deterministic only | + Gemini |
|---|---:|---:|
| Reconciliation accuracy | 78.2% | **85.0%** |
| Exception precision | 77.8% | **90.8%** |
| Exception recall | 80.3% | 72.9% |
| **False auto-reconciliation rate** | **0.0%** | **0.0%** |
| False exception rate | 7.0% | **0.3%** |
| Auto-reconciled | 50.9% | 59.5% |
| Human review | 17.1% | 15.6% |
| AI invocation rate | 20.4% | 20.4% |
| Model calls per 1,000 records | 0 | 216 |

**The number that matters most is the bolded 0.0%, in both columns.**
Across 1,000 held-out records, nothing was ever auto-reconciled that
should not have been. The model bought accuracy without spending safety.

**Exception recall falls 7.4 points, and that is the trade.** The model
converts some confident-but-sometimes-wrong EXCEPTIONs into human review.
Exception precision rises 13 points and the false exception rate drops
from 7.0% to 0.3% — the system stops flagging good records — at the cost
of being less willing to declare a problem outright.

Throughput with the model is 1.7 records/sec, network-bound at ~1s per
call; deterministic-only runs at ~250/sec on the same data. p95 was 1.9s
against a 10s timeout ceiling, so this run was not materially
rate-limited.

## 11. AI contribution

The gain is not spread thinly across the dataset. It is concentrated
exactly on the categories built to withhold identity evidence:

| Category | Deterministic | + Gemini | of |
|---|---:|---:|---:|
| `bank_narration_match` | 0 | **50** | 70 |
| `merchant_alias_match` | 0 | **26** | 50 |
| `reformatted_reference` | 79 | 89 | 90 |
| every other category | unchanged | unchanged | — |

(reconciled counts)

Two categories go from **zero** to a majority. Those are the ones where a
bank statement's narration has been truncated past recognition, or the
counterparty appears under a trading name instead of a legal entity. No
rule closes that gap; reading the text does.

Equally important is where the model changes **nothing**. On the traps —
`same_amount_different_txn`, `same_amount_same_date`,
`adjacent_invoice_same_amount`, `near_duplicate` — both configurations
reconcile zero, which is the correct answer. The model is not used where
deterministic logic already decides, and it does not talk the system into
matches the evidence refuses.

**The honest summary:** AI improves resolution of genuinely ambiguous
records by 6.8 accuracy points, recovering two categories deterministic
logic cannot touch at all, while deterministic policy holds false
auto-reconciliation at zero in both configurations.

## 12. Failure modes

| Failure | Behaviour |
|---|---|
| Model times out | HUMAN_REVIEW, reason recorded. 10s ceiling enforced by the caller, not the SDK |
| Model rate-limited | One retry honouring the provider's stated delay, then HUMAN_REVIEW |
| Malformed model output | Cannot reconcile; treated as unresolved |
| Provider unavailable | Batch continues; affected records go to review |
| Malformed CSV row | Skipped and reported, never silently dropped |
| One record raises | Batch continues; that record goes to review |
| Two orders claim one settlement | Both demoted unless one has strictly stronger evidence |
| Audit log tampered | `verify_chain` detects it and names the sequence |

## 13. Known limitations

- **All evaluation data is synthetic.** No real merchant data has been processed. Real narration, reference conventions and fee structures will differ.
- **The reference-contradiction rule assumes** the settlement reference derives from the merchant's. A provider using an opaque internal id needs `treat_reference_contradiction_as_negative=False`. The failure direction is safe — a missing settlement, with the rejected candidate shown — but it is a real false-negative mode.
- **Aggregated settlements are detected, not reconciled.** A unique decomposition is proposed and sent to review; ambiguous ones are not reported at all.
- **One payment split across several settlements is not representable** in the data model, and is not faked.
- **Thresholds are defaults, not calibration** against any merchant's risk tolerance. All live in `PolicyConfig`; every decision records which applied.
- **Gemini output is not byte-reproducible.** Temperature 0 is not a determinism guarantee.
- **The Razorpay live API returns no settlements** on a test account — verified, not assumed. See [docs/RAZORPAY_INTEGRATION.md](docs/RAZORPAY_INTEGRATION.md).

## 14. Running locally

Python 3.11+, Node 18+.

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt

cd backend
python data/generate_demo_data.py           # curated demo CSVs
cp .env.example .env                        # GEMINI_API_KEY optional
uvicorn app.main:app --port 8000

# separate terminal
cd frontend && npm install && npm run dev    # http://localhost:5173
```

Without `GEMINI_API_KEY` the semantic tier falls back to a deterministic
heuristic. That is a real fallback, and the ablation measures exactly
what it costs. Demo CSVs land in `backend/data/demo/`.

## 15. Running the tests

```bash
cd backend
python -m pytest tests/ -q                   # 174 tests

cd ../frontend
npm run build
node e2e/verify-upload-flow.mjs              # 39 browser checks, full product flow
node e2e/verify-ui.mjs                       # 44 browser + accessibility checks
```

The browser checks drive a real Chromium against the running stack. They
have found defects every backend test passed through — including a run
detail that rendered a blank page while throwing nothing.

## 16. Reproducing the evaluation

```bash
cd backend
python data/generate_final_dataset.py --seed 90210 --total 4000
python evaluate.py --dataset holdout --dataset-dir data/datasets_final --label final
python compare_engines.py --generations --dataset-dir data/datasets_final
python verify_evaluation_v1.py --rerun       # earlier evaluations still reproduce
python stress_test.py --sizes 1000 10000 50000
```

Earlier evaluations are frozen in `backend/evaluations/` with dataset
bytes, checksums and the commit that produced them. They are not modified.

---

## Repository layout

```
backend/
  app/
    ingest/          CSV parsing, schema detection, canonical mapping
    engine/          matching, policy, semantic, explain, batch
    ledger/          SQLite + hash-chained audit
    api/             runs (upload flow), routes (results, review, audit)
  data/              dataset + demo generators
  evaluations/       frozen earlier evaluations
  tests/             174 tests
frontend/
  src/components/    runs, upload, run detail, review queue, audit
  src/motion.js      shared motion vocabulary
  e2e/               real-browser verification
docs/
  PRODUCT_DECISIONS.md
  EVALUATION_METHODOLOGY.md
  ENGINEERING_FAILURES_AND_FIXES.md
  RAZORPAY_INTEGRATION.md
```
