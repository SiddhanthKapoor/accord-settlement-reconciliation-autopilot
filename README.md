<div align="center">
  <img src="frontend/public/brand/accord-logo-512.png" alt="" width="88" height="80">
  <h1>Accord</h1>
  <p><strong>When the numbers don&rsquo;t agree, Accord finds where the trail breaks.</strong></p>
</div>

Accord reconciles payments, settlements, bank statements and your ledger,
then investigates each record that did not close — tracing the money until
it can name the stage where the trail breaks, and saying plainly what it
could not confirm.

Deterministic arithmetic decides first. A language model is consulted only
where the ambiguity is genuinely semantic, and it can never book money.

---

## 1. The problem

A finance team does not have one reconciliation. They have last month's and
this month's ICICI statement, an HDFC account, three gateways because
different products settled through different processors, a Shopify export,
a Tally dump, and a chargeback report someone emailed them.

Matching the easy 80% is arithmetic. The work is the residue: a bank
narration that truncated the customer's name, a settlement that bundled
five payments into one credit, a payout that is not late because it is not
due yet, and two unrelated transactions that happen to be for exactly the
same amount on the same day.

Getting that residue **wrong** is worse than leaving it. A tool that
confidently mis-matches ₹18,420 produces a clean-looking book that is
false, and nobody finds out until an audit.

## 2. The product

```
UPLOAD MANY FILES → ACCORD CLASSIFIES THEM → YOU CONFIRM THE MAPPING
   → RECONCILE → AI ASSISTS ON GENUINE AMBIGUITY
   → BREAKPOINT ANALYSIS ON WHAT DIDN'T CLOSE → HUMAN REVIEW → AUDIT TRAIL
```

Three outcomes, never a fourth "probably fine" state: **RECONCILED**,
**EXCEPTION**, **HUMAN_REVIEW**.

## 3. Architecture

```
     ingest/          engine/                       ledger/
  reader   ──┐   ┌─ matching.py  (retrieval + admissibility)
  classify ──┼──▶├─ semantic.py  (the only model call)   ──▶ hash-chained
  schema   ──┤   ├─ policy.py    (decides every outcome)      audit ledger
  mapper   ──┘   ├─ batch.py     (claim integrity, aggregation)
                 └─ investigate.py (breakpoint + investigator)
                          │
                     providers.py  Gemini ──▶ Groq ──▶ HUMAN_REVIEW
```

`MerchantRecord` and `RazorpaySettlementRecord` keep their names for
historical reasons. Read them as **roles** — *ledger* (what the business
believes happened) and *settlement* (what happened to the money) — not as
vendors. Accord is not a Razorpay wrapper and runs with no Razorpay
credentials at all.

## 4. Multi-source ingestion

Upload up to **50 files** in one workspace. CSV and XLSX. No fixed slots.

Accord classifies each file after upload rather than making you declare it
first — combining column headers, identifier value shapes in the data
(`pay_XXXXXXXXXXXXXX` → Razorpay, an IFSC prefix → the actual bank), and
filename tokens. **A filename alone is capped at 0.45 confidence and can
never produce a confident answer on its own.**

Measured on the bundled 13-file demo workspace:

The bundled sample is one month of close for **Sahyadri Coffee Works**, a
fictional Bengaluru coffee-equipment retailer: 21 sources, 7,063 records,
ingested in about two seconds.

| File | Detected | Provider | Confidence |
|---|---|---|---:|
| `bank_hdfc_current_5521_mar2026.csv` | Bank statement | HDFC Bank | 0.97 |
| `bank_icici_escrow_8347_mar2026.xlsx` | Bank statement | ICICI Bank | 0.97 |
| `razorpay_settlements_mar_apr2026.csv` | Gateway settlement | Razorpay | 0.97 |
| `nodal_payout_advice_mar2026.csv` | Gateway settlement | — | 0.97 |
| `collections_settlement_advice_mar2026.csv` | Gateway settlement | — | 0.97 |
| `sahyadri_shopify_orders_mar2026.csv` | Order ledger | Shopify | 0.95 |
| `card_acquirer_settlement_mar2026.xlsx` | Gateway settlement | — | 0.88 |
| `sahyadri_zoho_books_invoices_mar2026.csv` | Order ledger | Zoho Books | **0.54** |

Two rows carry the argument.

The **0.54** is where Accord **refuses to run** until you confirm the role.

The three files with **no provider at all** matter more. They classify at
0.88–0.97 with `provider = None`, higher than several files whose vendor is
recognised — because the classifier reads column semantics, not brand
names. A settlement export from a system nobody has heard of still lands on
the right side of the reconciliation. Handles `₹7,700.00`, `(2,500.00)`, unix epochs,
`16-Mar-2026`, integer paise, split debit/credit columns, and an XLSX with
a two-row title block above the header.

Duplicate files are detected by content hash and **flagged, not dropped** —
the same file uploaded twice is a real finding.

## 5. Where the model is used

Only on the residue, and only for one bounded question: *do these two
records describe the same payment?*

By the time a pair reaches the model, exact reference matching and
deterministic corroboration have both failed. On the held-out set the model
sees **20.4%** of records; the other 79.6% are decided by arithmetic. On
the demo workspace it is 4 records out of 54.

Two categories score **zero** without it — `bank_narration_match` and
`merchant_alias_match` — because the identifier is genuinely absent or in a
different numbering system:

```
bank:    NEFT INWARD CLDPLTFRM RENEWAL NORTHWND
ledger:  Annual cloud platform renewal
```

## 6. Why the model is not trusted with financial authority

`policy.py` decides every outcome. The model returns a verdict and a
confidence; a match below `ai_confidence_threshold` (0.85) can never become
RECONCILED regardless of how clean the arithmetic looks. The model cannot
override an amount mismatch, a currency mismatch, or contradictory
identifiers. It never writes an explanation shown to an operator —
`explain.py` composes those from recorded signals.

The practical test: with `enable_semantic_matching=False` the system still
runs and loses **recall, not safety**. A component you can remove without
the product becoming unsafe was never holding the safety property.

**And this is measured, not asserted — see §10, where the AI configuration
does show a non-zero false auto-reconciliation rate.**

## 7. Provider fallback

```
Gemini (primary) → Groq (secondary) → HUMAN_REVIEW
```

Failures are classified precisely — `AUTH_FAILURE`, `MODEL_NOT_FOUND`,
`RATE_LIMIT`, `QUOTA_EXHAUSTED`, `TIMEOUT`, `CONFIGURATION_ERROR`,
`PROVIDER_ERROR` — because a bad key and an exhausted quota need different
responses and used to look identical in the logs. Gemini's per-day quota is
distinguished from its per-minute rate limit by inspecting
`QuotaFailure.violations[].quotaId`.

```
$ python health_check.py
PROVIDER    AVAILABLE   MODEL                    LATENCY
gemini      yes         gemini-3.5-flash-lite     957 ms
groq        yes         openai/gpt-oss-120b       656 ms
chain status: AI_AVAILABLE
```

If both fail, the record goes to a human with the failure recorded as a
provider error — never as a match verdict. **The third branch is the floor;
the first two are conveniences on top of it.**

## 8. Breakpoint Analysis

For every record that did not close, Accord names *where* the trail broke:

```
ORDER        FOUND          Order ORD-7021, ₹31,200.00 on 2026-03-17
PAYMENT      NOT_EVALUATED  Matched directly against the bank statement
SETTLEMENT   NOT_EVALUATED  No gateway payout row matched this record
BANK         FOUND          matched ICICI_January.csv row 4182
BOOKS        NOT_EVALUATED  No accounting export was included in this run
```

The distinctions that matter, and that the code enforces:

- **NOT_EVALUATED is not MISSING.** If you uploaded no bank statement,
  Accord says the bank was not checked. Claiming "no bank credit found"
  for a file nobody supplied is a fabricated finding.
- **PENDING is not MISSING.** A payment captured this morning has no
  settlement because none is owed. Reporting that as missing sends someone
  chasing a provider for money that was never late.
- Everything downstream of a break is NOT_EVALUATED — nothing asserts
  absence at a hop that was never tested.

**Honest limit:** Accord reconciles a pooled ledger side against a pooled
settlement side. It does *not* independently prove order→gateway and then
gateway→bank as two chained hops. It reports where the evidence ran out,
which is genuinely useful and is not the same thing as a five-stage graph
traversal.

## 9. The AI Exception Investigator

An explicit action on an exception — not a chatbot, not automatic. It
returns three sections that never blur:

**CONFIRMED EVIDENCE** — deterministic facts only, computed in code.
*"Three unmatched orders in window total ₹50,000; bank credit ₹48,200; the
₹1,800 difference equals recorded fee ₹1,525 + tax ₹275."*

**LIKELY EXPLANATION** — ranked hypotheses, each labelled `DETERMINISTIC`
or `AI`. Arithmetic hypotheses are generated with **no model call at all**;
a deterministic hypothesis above 0.85 suppresses the model entirely.

**UNRESOLVED** — what the data genuinely cannot settle. *"No settlement
breakdown file was supplied, so the aggregation cannot be booked
automatically."*

Any model claim asserting a number or identifier **not present in the
evidence bundle is stripped before it reaches the caller**, and the count of
dropped claims is reported. `recommended_action` is decided by policy, not
by the model. Investigation is read-only: it writes one audit event and
never changes a record's outcome.

When no model call was needed, the status is `AI_NOT_CONSULTED` — distinct
from `AI_UNAVAILABLE`, because "we didn't need to ask" and "we asked and
nobody answered" are different facts.

## 10. Evaluation

1,000-record held-out split, seed 90210, 19 labelled failure categories,
stratified 75/25. Ground truth comes from each category's definition, never
from what the engine produced. Run once per configuration on the shipped
commit.

Frozen at `backend/evaluations/accord/`, commit `b6145bb`, all four
reports produced against a clean tree.

| Configuration | Accuracy | **False auto-recon** | Exc. precision | Exc. recall | Human review | Provider failures |
|---|---:|---:|---:|---:|---:|---:|
| **A** — deterministic only | 82.5% | **0.0%** | 72.6% | 98.4% | 7.1% | — |
| **B** — Gemini primary | **88.3%** | **0.17%** | 87.9% | 91.0% | 9.1% | 74/204 (36%) |
| **D** — Groq only (Gemini down) | 77.5% | 0.0% | 87.8% | 79.0% | 20.9% | 196/204 (96%) |
| **C** — both providers dead | 76.8% | 0.0% | 87.6% | 77.7% | 21.6% | 204/204 (100%) |

The model earns its +5.8 points in exactly two categories, both of which
deterministic matching scores **zero** on:

| Category | Deterministic | With the model |
|---|---:|---:|
| `bank_narration_match` | 0 / 70 | **43 / 70** |
| `merchant_alias_match` | 0 / 50 | **28 / 50** |
| `corrupted_reference` | 0 / 40 | 0 / 40 — the model does not rescue it, and should not |

**Read the second column before the first.**

The AI configuration adds **+5.2 accuracy points** over deterministic
matching — and it is the only configuration with a **non-zero false
auto-reconciliation rate**. One record in the `same_amount_different_txn`
trap category was auto-reconciled: the model returned SAME above the 0.85
gate on two unrelated transactions sharing an amount.

That is one record in 1,000, and it is reported here rather than smoothed
away. Across three measured runs of this configuration the rate was
**0.2%, 0.0% and 0.17%** — the same trap category each time it appeared.
So the honest claim is a range, not a constant: with the model enabled,
false auto-reconciliation is **0.0%–0.2%**, driven by the model's
run-to-run nondeterminism. The deterministic configuration's 0.0% is
*structural* — with no model, no model verdict can admit a match.

The threshold that admitted it (`ai_confidence_threshold`) was **not**
adjusted after seeing this result. Tuning a safety knob to make a holdout
number look better is how evaluations stop meaning anything. It is a
per-merchant policy value, documented as one.

**Configurations B and D were measured under free-tier rate limiting** —
38% and 89% of model calls failed. Those failures degrade recall, not
safety: every one routed to human review.

Configuration A reproduces the previously frozen deterministic baseline
**bit-identically** on every accuracy and safety metric, which is the
evidence that this cycle's work changed no engine behaviour.

## 11. Failure modes, and what happens

| Failure | Response |
|---|---|
| Gemini rate-limited or quota-exhausted | Falls through to Groq |
| Both providers down | HUMAN_REVIEW, classified PROVIDER_ERROR |
| Model returns malformed JSON | Not trusted; treated as a provider error |
| Model asserts a fact not in the evidence | Claim stripped before it is shown |
| Required column unmapped | Run is blocked with the reason on the button |
| File role detected below threshold | Run is blocked until a human confirms |
| Two records claim one settlement | Both demoted to review, never decided by position |

## 12. Honest limitations

- **All evaluation data is synthetic.** No customer data, no production
  figures, no merchant users. Real settlement text and fee structures will
  differ.
- **The AI configuration's false auto-reconciliation rate is not zero**
  (§10).
- **Split settlements** — one payment across several settlements — are not
  representable. Such data surfaces as an amount mismatch for a human.
- **Aggregation is proposed, never booked.** Only a *unique* decomposition
  is reported, and its members go to review.
- No chained multi-hop matching (§8).
- Source classification confidence is a hand-calibrated heuristic, not
  fitted to a labelled corpus. It has no accuracy number, and one is not
  invented.
- Duplicate file detection is exact-bytes only.
- Legacy `.xls` is unsupported; re-save as `.xlsx` or CSV.
- The Razorpay integration proves the client works. It does **not** prove
  the engine reconciles real Razorpay data — a test account returns zero
  settlements. See `docs/RAZORPAY_INTEGRATION.md`.

## 13. The sample workspace

One month of close for **Sahyadri Coffee Works**, a fictional Bengaluru
coffee-equipment retailer. 21 sources, 7,063 records: four bank accounts,
a gateway payments export and its settlement report, a card acquirer
settlement, a nodal payout advice, a collections advice, a marketplace
payout, a POS register, a webstore order book, invoices, an ERP general
ledger and a Tally sales register.

A full run, measured:

```
7,063 records ingested          ~2s
3,504 ledger records reconciled ~10s
3,428 reconciled · 66 exceptions · 10 human review
7 model calls
```

Five records carry the argument:

| Record | Outcome | What it shows |
|---|---|---|
| `ORD-7032` | Reconciled | Exact reference. No model. The ordinary 98%. |
| `ORD-7021` | Reconciled, AI-assisted | Bank narration with no shared identifier. 0.93 against the 0.85 gate. |
| `ORD-7031` | **Exception** | Same ₹8,650 as `ORD-7032`, one day apart — **refused**. |
| `ZB-6107` | Exception | No settlement, but **pending**, not missing. |
| `ORD-7104` | Human review | Two plausible candidates. Neither proven. |

`ORD-7031` and `ORD-7032` share an amount to the paisa. One matched, one
was refused. That pair is the product in miniature.

## 14. Running it

```bash
python -m venv .venv && .venv/bin/pip install -r backend/requirements.txt
cp backend/.env.example backend/.env       # optional; runs fully offline without keys

cd backend && ../.venv/bin/python -m uvicorn app.main:app --port 8000
cd frontend && npm install && npm run dev  # http://localhost:5173
```

Then open http://localhost:5173, go to **New run**, and click **Load
sample workspace** — 21 sources and 7,063 records, ingested through exactly
the path an upload takes. A full run takes about ten seconds.

To regenerate those files from the seed instead:

```bash
cd backend && ../.venv/bin/python data/generate_demo_workspace.py --verify
```

`--verify` re-reads every generated file through the real reader, detector,
classifier and engine, and fails loudly if any scenario stops landing where
it was designed to.

## 15. Tests

```bash
cd backend && ACCORD_AI_DISABLED=1 ../.venv/bin/python -m pytest -q
# 351 passed, 1 skipped

../.venv/bin/python health_check.py            # live provider probe
../.venv/bin/python verify_evaluation_v1.py    # frozen evaluation integrity

cd frontend && node e2e/verify-upload-flow.mjs # product flow in a real browser
node e2e/verify-ui.mjs                         # evaluation + accessibility
```

`ACCORD_AI_DISABLED=1` forces the offline heuristic so outcomes are a
property of the code rather than of a live model.

## 16. Reproducing the evaluation

```bash
cd backend
../.venv/bin/python data/generate_final_dataset.py
ACCORD_AI_DISABLED=1 ../.venv/bin/python evaluate.py \
    --dataset holdout --dataset-dir data/datasets_final --label deterministic
```

Frozen evaluations live under `backend/evaluations/`, each pinning the
commit that produced it, with SHA-256 checksums over every report.
`verify_evaluation_v1.py` checks that integrity, and `freeze_evaluation.py`
refuses to freeze a set of reports whose commits disagree or whose working
tree is dirty — a freeze that points at a commit the code no longer matches
is worse than no freeze at all.

## 17. Repository layout

```
backend/
  app/ingest/      reader · classify · schema · mapper · flow
  app/engine/      matching · policy · batch · semantic · providers · investigate · explain
  app/ledger/      hash-chained audit ledger, SQLite store
  app/api/         routes · runs · ai · investigate
  data/            generators, demo_workspace/ (the sample month), eval reports
  evaluations/     frozen, checksummed, commit-pinned
  tests/           351 tests
frontend/
  src/             router · Landing · workspace components · motion
  e2e/             browser verification driving real Chromium
docs/              product decisions · evaluation methodology · failures · demo script
```

---

<div align="center">
<sub>Synthetic evaluation data throughout. No customer data, no production figures.</sub>
</div>
