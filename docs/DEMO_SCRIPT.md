# Demo workspace and demo script

`backend/data/demo_workspace/` is six weeks of one fictional business's
paperwork: **20 sources, 3,504 ledger records and 3,553 settlement
records — 7,057 in all** — plus one file that is a byte-identical
re-upload of another. It is written by
`backend/data/generate_demo_workspace.py`, which is deterministic: two
runs produce byte-identical output, XLSX included, and the observation
point is a pinned constant (`AS_OF = 2026-04-15`) rather than
`datetime.now()`, so nothing here decays.

The set is built in two halves, on purpose.

**Everything that carries a claim is hand-written.** Every scenario named
in §3, and every record that can end a run with no matched settlement, is
a literal row in the generator, and every claim this document makes about
it is asserted in code (§2).

**The ordinary majority is generated.** Nearly 3,450 records exist to
demonstrate the thing the product actually claims — that almost all of a
reconciliation is decided by arithmetic, at scale, with no model call —
and a 54-record workspace cannot demonstrate that. They come from a
second fixed seed under constraints strict enough that they cannot
accidentally become interesting: amounts unique to well outside the
matcher's 2-paise tolerance, never equal to any 2- or 3-way sum of the
unresolved population, and drawn from counterparty and product wording
that shares nothing distinctive with the hand-written cases.

Regenerate and self-check:

```bash
python backend/data/generate_demo_workspace.py            # write the files
python backend/data/generate_demo_workspace.py --verify    # + detector + engine
```

`--verify` feeds every generated file back through the real
`app.ingest.reader.read_table`, `app.ingest.schema.detect_schema`,
`app.ingest.classify.classify_source`, `app.ingest.mapper.map_rows` and
`app.engine.batch.process_batch`, with `ACCORD_AI_DISABLED=1` so no key
is read and no network call is made. It prints the real outcome
distribution and **exits non-zero** if any invariant below stops holding
or any scenario stops landing where §3 says it does.

---

## 1. File inventory

### Ledger side — 3,504 records across seven sources

| File | Rows | Shape | Amounts | Dates |
|---|---:|---|---|---|
| `webstore_orders_master.csv` | 1600 | storefront order book: `Order Reference`, `Placed On`, `Customer Name`, `Total Amount`, `GST`, `Sales Channel` | plain decimals | ISO-8601 instants, `2026-03-19T17:09:20Z` |
| `erp_gl_export_fy2026.csv` | 1050 | ERP general ledger: `Posting Date`, `Journal`, `GL Account Code`, `Cost Centre`, `External Ref`, `Narrative`, split `Debit`/`Credit` | plain decimals, credit column | `24-Mar-2026` |
| `pos_counter_sales_register.csv` | 800 | retail counter register: `Bill No`, `Store Code`, `Terminal Id`, `Payment Mode`, `Bill Amount` | Indian grouping, `27,460.00` | `23/03/2026` |
| `invoices_internal_export.csv` | 30 | internal invoice book, `INV-3xxx` / `ORD-7xxx` | plain decimals | ISO dates |
| `orders_shopify_export.csv` | 12 | Shopify export: `Name`, `Financial Status`, `Total`, `Lineitem name` | plain decimals | `2026-03-06 09:14:22` |
| `tally_sales_register.csv` | 7 | Tally sales register: `Voucher No`, `Bill Ref No`, `Particulars`, split `Debit`/`Credit` | plain decimals | `18-03-2026` |
| `zoho_books_invoices.csv` | 5 | Zoho Books invoice export | plain decimals | ISO dates |

### Settlement side — 3,553 records across thirteen sources

| File | Rows | Shape | Amounts | Dates |
|---|---:|---|---|---|
| `razorpay_payments_export.csv` | 1595 | a **payments** export, not a settlement export: `id`, `order_id`, `method`, `captured`, `fee`, `tax`, no net column at all | integer **paise** | unix epoch seconds |
| `upi_collections_settlement_report.csv` | 1050 | UPI collections: `Txn Id`, `Merchant Ref No`, `Payer VPA`, `Settlement Utr`, `MDR`, `Net Amount`, `Refund Amount` | `₹19,430.00` | `27/03/2026 16:01` |
| `paytm_pos_settlements.xlsx` | 796 | acquirer report, `BANKTXNID` / `TXNID` / `ORDERID` / `TXNAMOUNT` / `NETAMOUNT` / `REFUNDAMT` — **XLSX whose real header is on row 5** under a three-line title block | plain decimals | `2026-03-23 19:26:33` / `25-03-2026` |
| `gateway_fee_adjustments_mar2026.csv` | 24 | the gateway's fee and adjustment register: MDR, GST on MDR, chargeback fees, rolling reserve, reversals; `Entry Id`, `Merchant Ref`, split `Debit`/`Credit` | plain decimals | `2026/03/06` |
| `razorpay_settlements_mar_apr.csv` | 22 | `pay_XXXXXXXXXXXXXX`, `settlement_id`, `fee`, `tax`, `net_amount` | integer **paise** | unix epoch seconds |
| `collections_settlement_advice_mar2026.csv` | 19 | the collection account's **sweep advice**, not a gateway export: `Advice No`, `Sweep Credit Date`, `Collection Mode`, `Invoice Ref`, `Payout Id`, `Gross Amount` / `Collection Charges` / `Tax` / `Payout Amount`. **No provider branding anywhere in it** — see §1a | Indian grouping with the direction **inside the cell**, `21,600.00 Cr` / `432.00 Dr` | ISO-8601 instants with a real **+05:30 offset**, `2026-03-10T11:42:08+05:30` |
| `bank_axis_current_marapr2026.csv` | 16 | third bank account: money in **and out in one signed column**, accounting-style parenthesised negatives, `Dr/Cr` indicator, running `Balance` | `(1,18,450.00)` / `27,310.00` | `05-Mar-2026` |
| `bank_hdfc_current_mar2026.csv` | 9 | Indian bank statement: `Narration` / `Ref No` / `Withdrawal` / `Deposit` / `Closing Balance` | `4,20,450.00` | `04-03-2026` |
| `bank_hdfc_current_apr2026.xlsx` | 6 | same account, next month, **XLSX with a two-row title block** — header on row 4 | as above | as above |
| `bank_icici_escrow_mar2026.xlsx` | 6 | second account (escrow), XLSX, header on row 1 | as above | as above |
| `payu_settlements_mar.csv` | 4 | `Mihpayid`, `Txnid`, `Merchant Ref No`, `Service Charge` | `₹7,700.00` | `21/03/2026 09:15` |
| `kartway_marketplace_payout.csv` | 3 | marketplace payout, hyphenated headers, weekly window | plain decimals, negative fee column | `16-Mar-2026` |
| `refunds_chargebacks_mar2026.csv` | 3 | refund / chargeback report | **negative in parentheses**, `(2,500.00)` | `Mar 22, 2026` |

### Not data

| File | Purpose |
|---|---|
| `bank_icici_escrow_mar2026 (1).xlsx` | byte-identical copy of `bank_icici_escrow_mar2026.xlsx` — the accidental second upload, for the duplicate-file check |
| `_manifest.json` | generator metadata: both seeds, `as_of`, per-file SHA256, the aggregation groups, the unresolved population, the scenario index with its expected offline outcome. Not an upload. |

Nine money conventions and eleven date formats across the set. Running
balances are consistent within each bank account, and the April HDFC
statement opens where the March one closed. Total on disk: 1.03 MB
across all 22 files.

### 1a. The unbranded source

`collections_settlement_advice_mar2026.csv` is the one settlement source
in the workspace with **no provider identity anywhere in it** — no
vendor-prefixed column, no vendor-shaped identifier, no vendor token in
the filename. It is the advice the business's collection account
operator issues when it sweeps the day's net-banking, payment-link and
card collections into the current account: one line per collection, with
its own charge and tax breakdown, batched under a per-day `Advice No` at
a fixed 18:30 IST cut-off.

It is in the set on purpose. `classify.py`'s provider table is a *label*,
not a gate, and this file is what proves it: on column semantics alone it
lands at **PAYMENT_GATEWAY 0.97, stage SETTLEMENT, `provider = None`** —
a more confident classification than any of the branded settlement files
— and `--verify` fails if that ever stops holding. It is also where two
of the set's conventions live alone: money as Indian-grouped rupees with
the direction marked inside the cell (`21,600.00 Cr`, `432.00 Dr`)
rather than by a sign, a bracket or a second column, and dates as
ISO-8601 instants carrying a real `+05:30` offset rather than UTC — the
storefront export writes the same shape with a trailing `Z`, and
`schema.parse_date` reads both.

It carries nineteen collections: five internal invoices, eleven web
orders, one Zoho invoice, and the two rows §3 H and §3 I turn on —
`INV-3117` (the ₹8,650.00 lookalike `ORD-7031` must be refused against)
and `INV-306` (the truncated form of `ORD-7040`'s `INV-3062`).

---

## 2. What the generator asserts about its own data

These are not comments. `check_invariants` raises and the process exits
non-zero if any of them stops holding, and they run on every plain
generation as well as under `--verify`. A dataset bug in this repository
once invalidated a whole evaluation because a supposedly semantic case
leaked its identifier into both sides; these exist so that cannot happen
again quietly, and they are what makes it safe to grow the population to
several thousand records.

Real output, current tree:

```
AGG-A  4230000 + 2785000 + 941500 = 7956500 paise (Rs. 79,565.00)
AGG-B  (3315000-66300-11934) + (1840000-36800-6624) + (990000-19800-3564) = 5999978 paise (Rs. 59,999.78)
AGG-C  1234000 + 891500 = 2125500 paise (Rs. 21,255.00) -> UTR9930518
TRAP   ORD-7031 ['3042'] vs INV-3117 ['3117'] — same amount, comparable namespaces, disjoint: deterministic refusal
SEMAN  ORD-7021 ['3055'] vs UTR774120 ['774120'] — disjoint, incomparable widths (HDFC narration)
SEMAN  ZB-6104 ['6104'] vs KWY-88213 ['88213'] — disjoint, incomparable widths (marketplace payout)
SEMAN  WB-104217 ['104217'] vs UTR9930114 ['9930114'] — disjoint, incomparable widths (Axis narration)
SEMAN  WB-104931 ['104931'] vs UTR9930287 ['9930287'] — disjoint, incomparable widths (merchant alias, trading name vs legal name)
REFMT  WB-105204 vs UPI/WB105204/COLL share core ['105204'] at an identical amount — recoverable with no model call
TIME   as_of 2026-04-15 (latest bank value date), pending cutoff 2026-04-13; pending ZB-6107, WB-106988, WB-107455, POS-303551; missing ORD-7034, WB-106402, GLX-206115, POS-302640
FEETAX BR-4481 stated net 680000 vs gross-fee-tax 751828 (difference -71828 paise)
FEETAX 12 generated payout rows also fail gross-fee-tax-refund, by 7395-11353 paise
AMTMIS POS-300412 books 2746000, the payout says 2704600 (41400 paise short) and no record in the workspace accounts for the difference
REFS   3547 distinct settlement references, 6 deliberately doubled
AGGR   over 3553 settlements and 26 possibly-unmatched records, the only 2-3 combinations that sum to a settlement are the two intended ones
AMTS   3504 ledger records, and the only two sharing an amount are the trap pair
TWINS  each of the 26 possibly-unmatched records is amount-matched only by the settlements it was designed to be matched by
```

The last three are the ones that make scale safe rather than merely
large. `AMTS` and `TWINS` say that no generated record has quietly become
a candidate for a hand-written case — the amount index is O(1) and
recall-oriented, so a single colliding amount anywhere in 3,553
settlements is enough to change an outcome. `AGGR` says the same thing
for aggregation: the search space is every 2- and 3-way sum of the
unresolved population against every settlement, and exactly two of those
combinations are allowed to hit.

---

## 3. Scenarios

Every record id below is what the engine shows, i.e. what
`mapper.map_rows` produces as `order_id`. "Correct outcome" is what the
construction of the data entails. The offline outcome in §4 is measured;
the with-a-model outcome is not asserted anywhere.

### A — clean deterministic matches (the majority)

**3,423 of 3,504 records — 97.69%.** They resolve on an exact normalized
reference, pass currency, gross amount, fee/tax arithmetic, settlement
timing and refund consistency, and reconcile. `ORD-7036` resolves through
`DISAMBIGUATED_BY_AMOUNT` instead (two settlements carry its reference;
exactly one matches on amount). **Correct outcome: RECONCILED, no model
call.** This is the point of the demo, not the filler around it.

Two named members worth opening on stage:

| | |
|---|---|
| **A1 `ORD-7032`** | the identical-amount trap's twin — the real owner of the ₹8,650.00 sweep-advice collection, matched on its own exact reference while `ORD-7031` is refused. |
| **A2 `WB-105204`** | ₹19,430.00, "Handloom fabric bulk consignment — Kesari Handlooms Pvt Ltd". The UPI report wrapped the merchant reference in its own string: `Merchant Ref No` is `UPI/WB105204/COLL`. Exact matching misses it; the shared identifier core `105204` plus an exact amount and corroborating wording resolve it in the deterministic fuzzy tier. **RECONCILED / `CORROBORATED`, still no model call.** The generator asserts both halves: that the references do *not* match exactly, and that they *do* share a core. |

### B — aggregation: several payments, one settlement

Proposed, never auto-booked. `batch.detect_aggregated_settlements`
reports a group only when it is the **unique** 2-or-3 subset of unmatched
records summing to a settlement within tolerance, and even then the
members go to review.

| | |
|---|---|
| **B1 `BR-4471`, `BR-4472`, `BR-4473`** | three Tally invoices to one distributor, ₹42,300.00 + ₹27,850.00 + ₹9,415.00. One ICICI escrow credit on 2026-03-23, ref `UTR881318`, is ₹79,565.00 — the exact sum. |
| **B2 `POS-300771`, `POS-300779`** | two counter bills, ₹12,340.00 + ₹8,915.00. One Axis credit on 2026-04-06, ref `UTR9930518`, `NEFT INWARD POS CONSOLIDATED PAYOUT`, is ₹21,255.00 — the exact sum. |

**Correct outcome for all five: HUMAN_REVIEW / `AGGREGATED_SETTLEMENT`.**

There is a third, informational tie-out with no engine finding attached:
`INV-3010`, `INV-3011` and `INV-3012` settle in one Razorpay batch on
2026-03-11, and after each row's recorded fee and tax the nets are
₹32,367.66 + ₹17,965.76 + ₹9,666.36 = **₹59,999.78**, exactly the HDFC
credit `UTR774008` on the same day. The three orders reconcile
individually on their references, so nothing needs decomposing — the
tie-out is there for a human to check the bank line against the batch,
which is what a finance operator actually does.

### C — the deterministic tier genuinely cannot resolve it

The identifier is absent or in a different namespace. The generator
asserts, using the engine's own `normalize.reference_cores` and
`normalize.references_comparable`, that the two sides share **no** digit
run and that their identifier widths are **not comparable** — so no
deterministic path can recover the link, and the disagreement cannot be
read as a contradiction either.

| | |
|---|---|
| **C1 `ORD-7021`** | books say `INV-3055`, "Annual cloud platform renewal — Northwind Retail Private Limited", ₹31,200.00 on 2026-03-17. The only trace of the money is one HDFC line on 2026-03-18: `NEFT INWARD CLDPLTFRM RENEWAL NORTHWND`, ref `UTR774120`. Cores `{3055}` vs `{774120}` — widths 4 vs 6. |
| **C2 `ZB-6104`** | Zoho invoice "Marketplace channel settlement week 12 — Kartway Seller Services", ₹27,450.00. The settlement is marketplace payout row `KWY-88213`, "Seller services payout for week 12". Cores `{6104}` vs `{88213}` — widths 4 vs 5. |
| **C3 `WB-104217`** | ₹46,185.00 on 2026-03-19, "Cold pressed oil case pack bulk despatch — Vaayu Organics Private Limited". One Axis credit on 2026-03-21: `NEFT INWARD COLD PRESS OIL CASE VAAYU ORGNC`, ref `UTR9930114`. Cores `{104217}` vs `{9930114}` — widths 6 vs 7. |

**Correct outcome for all three: RECONCILED via `SEMANTIC_CONFIRMED` when
the model answers SAME at or above the 0.85 confidence gate;
HUMAN_REVIEW otherwise.** Never a silent match —
`PolicyConfig.ai_confidence_threshold` is enforced in `policy.py`, not by
the model.

### D — merchant alias

**`WB-104931`**, ₹58,940.00 on 2026-03-26, booked against **Trisool
Online Pvt Ltd** for a "Quarterly platform licence". The Axis statement
shows `RTGS INWARD TRISOOL ONLINE SERVICES` on 2026-03-28, ref
`UTR9930287` — the trading name, not the legal entity, which is how a
bank narration usually arrives. Cores `{104931}` vs `{9930287}`, widths 6
vs 7: no identifier links them and none contradicts them either, so the
pair is admitted on wording and amount and escalated.

**Correct outcome: RECONCILED via `SEMANTIC_CONFIRMED` at or above 0.85;
HUMAN_REVIEW otherwise.**

### E — exceptions: money known to be wrong

| | | Count in the run |
|---|---|---:|
| **E1 `POS-300412`** — amount mismatch nothing explains | books ₹27,460.00; the Paytm report settles `POS-300412` at **₹27,046.00**, ₹414.00 short. Transposed digits, not a fee. The generator asserts no ledger record in the workspace equals the difference, so a missing line item does not explain it. **EXCEPTION / `AMOUNT_MISMATCH`, HIGH.** | 24 |
| **E2 `BR-4481`** — fee/tax arithmetic | PayU states gross ₹7,700.00, service charge ₹154.00, GST ₹27.72 and **net ₹6,800.00**, while gross − fee − tax is ₹7,518.28. **EXCEPTION / `FEE_TAX_INCONSISTENT`, HIGH.** The match itself is fine; the payout file does not add up. | 13 |
| **E3 `ORD-7037`** — chargeback the books never recorded | ₹16,800.00 booked as fully paid; the gateway recorded a ₹4,200.00 chargeback and the refund report carries it as `(4,200.00)`. **EXCEPTION / `REFUND_MISMATCH`, MEDIUM.** | 9 |
| **E4 `ORD-7038`** — currency mismatch | booked as **USD** 1,200.00; the gateway settled **INR** 1,200.00. Minor units carry no currency, so every other check passes on this pair. **EXCEPTION / `CURRENCY_MISMATCH`, HIGH.** | 5 |
| **E5 settlement delayed** | generated: a settlement that arrived 24–28 days after capture, past `max_settlement_delay_days` (21). **EXCEPTION / `SETTLEMENT_DELAYED`, MEDIUM.** | 6 |

E1–E4 each have a hand-written, individually asserted exemplar and a
generated population behind them; E5 is generated only. The point is that
the exception queue looks like a real month rather than a demo of four
singletons.

### F — human review: the evidence does not settle it

| | |
|---|---|
| **F1 `ORD-7104`** — one payment, two sources | ₹24,999.00 (`INV-3050`). It appears once in `razorpay_settlements_mar_apr.csv` and once in `bank_hdfc_current_mar2026.csv` as `NEFT INWARD RAZORPAY SETL NORTHWND RTL`, where the bank's `Ref No` carries the remitter's reference. Two candidates, both matching on amount. |
| **F2 `WB-105633`** — the gateway reported one payment twice | ₹27,180.00, two rows in `razorpay_payments_export.csv` with the same `order_id` and the same amount, different `pay_` ids. |
| **F3 `GLX-207209`** — the same, in the UPI report | ₹36,415.00, two `UPI…` rows carrying `GLX-207209`. |
| **F4 `GLX-204880`** — two equally plausible bank credits | ₹33,750.00, "Consignment despatch schedule 14 — Meghdoot Packaging LLP". The Axis statement shows **two** credits from that counterparty at exactly ₹33,750.00, on 2026-03-31 and 2026-04-01, with different narrations and different UTRs. Neither leads the other by the deterministic margin. |
| **F5 `SH-88211`** — ambiguous for the model too | ₹4,120.00 on 2026-03-25, notes "Counter sale payment received". One HDFC credit on 2026-03-26 for exactly ₹4,120.00: `UPI/COLLECT/9911/PAYMENT RECEIVED`. Both sides are vague and the identifier namespaces are not comparable, so there is nothing to confirm and nothing to contradict. |

**Correct outcome for all five: HUMAN_REVIEW.** F1–F3 are
`AMBIGUOUS_MULTIPLE` and are refused deterministically, with no model
call, in every run. F4 and F5 reach the semantic tier.

On F4, one honest caveat: the engine escalates **one pair at a time**, so
a model asked about `GLX-204880` against the first credit is not shown
that a second, equally good credit exists. In the live run recorded in §5
the model answered SAME on the first pair and the record reconciled,
leaving the second credit unclaimed. Offline — where nothing overrides
the deterministic near-tie — it lands in review, which is the correct
answer. F5 is the case where the *pair itself* carries nothing, and is
the one to point at when the claim is "not everything is solvable".

### G — pending vs missing

Both pinned to `AS_OF = 2026-04-15`, which is also the latest settlement
date in the workspace — the value `batch.process_batch` derives when the
upload path does not pass one. The cutoff is `AS_OF − 2 days`
(`PolicyConfig.settlement_expected_days`).

| | |
|---|---|
| **G1 pending** — `ZB-6107` (₹15,750.00, 2026-04-14), `WB-106988` (₹9,875.00, 2026-04-14), `WB-107455` (₹14,260.00, 2026-04-15), `POS-303551` (₹18,435.00, 2026-04-14) | a settlement is not due yet. **EXCEPTION / `PENDING_SETTLEMENT`, severity LOW — "wait", not "chase the provider".** |
| **G2 missing** — `ORD-7034` (₹18,650.00), `WB-106402` (₹22,900.00), `GLX-206115` (₹41,320.00), `POS-302640` (₹5,680.00) | old enough that a settlement is due, with no lookalike anywhere in the population. **EXCEPTION / `MISSING_SETTLEMENT`.** |

### H — the refusal

**`ORD-7031`** (`INV-3042`, "Gift card bulk purchase corporate",
₹8,650.00, 2026-03-24) has **no settlement anywhere**. One day later,
`INV-3117` — a completely different customer's refrigeration unit deposit
— is collected and swept for **exactly ₹8,650.00** on the collections
advice. The two references live in the same numbering system and name
different transactions.

**Correct outcome: EXCEPTION, refused, with the refusal naming the record
it looked at and why it rejected it.** Matching on amount and date would
double-count ₹8,650 and orphan a real order. Deterministic — no model
call is spent on it.

### I — truncated reference

**`ORD-7040`** (`INV-3062`), ₹13,475.00. The collections advice recorded
the reference as **`INV-306`** — truncated. `306` is three digits, below
`normalize.MIN_REFERENCE_CORE_DIGITS`, so it is not an identifier core at
all: exact matching misses and nothing contradicts. **Correct outcome:
RECONCILED via `SEMANTIC_CONFIRMED` at or above 0.85, otherwise
HUMAN_REVIEW / `LOW_CONFIDENCE_MATCH`.** Both are correct behaviour; a
silent low-confidence reconcile is not.

### J — the two ingestion beats

| | |
|---|---|
| **J1 duplicate file** | `bank_icici_escrow_mar2026 (1).xlsx` is a byte-for-byte copy of `bank_icici_escrow_mar2026.xlsx` (same SHA256, recorded in `_manifest.json`). **The duplicate is detected and reported before reconciliation.** |
| **J2 a source Accord will not run on without asking** | `zoho_books_invoices.csv` classifies as `ORDERS` at **0.54**, below `classify.CONFIDENCE_THRESHOLD` (0.65). Its required fields are all mapped, so nothing is broken — but the *role* was inferred weakly, and putting a ledger on the settlement side would reconcile the books against themselves and return a page of clean matches. `POST /runs/{id}/execute` refuses until a person confirms it. This is the single most important ingestion beat: **the system asks instead of guessing.** |

One more mapping is deliberately left for the operator:
`orders_shopify_export.csv` has a bare `Name` column, which the detector
reads as a counterparty — the more common reading, and wrong for a
Shopify export where it is the order number. Required fields (`Total`,
`Created at`) both resolve so the run is not blocked, but until `Name` is
mapped to `reference` the twelve Shopify orders have no cross-system
identifier. `SH-88211` (§3 F5) only exists as a record id once that
mapping is confirmed.

---

## 4. Verification output

Real output of `ACCORD_AI_DISABLED=1 python
backend/data/generate_demo_workspace.py --verify`, run against the
current tree. Reconciliation ran with the labelled offline heuristic
verifier and no network call. Exit code 0.

```
  20 sources, 3504 ledger records, 3553 settlement records, 0 rejected rows
  1 source(s) need confirmation before a run: zoho_books_invoices.csv
  ingestion (read + detect + classify + map): 0.53s
  reconciliation (process_batch, 3504 records): 0.16s
  derived as_of = 2026-04-15T00:00:00+00:00  (expected 2026-04-15T00:00:00+00:00)

  outcome distribution
    RECONCILED       3423   97.69%
    EXCEPTION          67    1.91%
    HUMAN_REVIEW       14    0.40%
  exception / review types
    AMOUNT_MISMATCH              24
    FEE_TAX_INCONSISTENT         13
    REFUND_MISMATCH               9
    AMBIGUOUS_MATCH               8
    MISSING_SETTLEMENT            6
    SETTLEMENT_DELAYED            6
    CURRENCY_MISMATCH             5
    AGGREGATED_SETTLEMENT         5
    PENDING_SETTLEMENT            4
    LOW_CONFIDENCE_MATCH          1
  match classifications
    EXACT_REFERENCE                3477
    NO_ADMISSIBLE_CANDIDATE          10
    SEMANTIC_UNRESOLVED               5
    PENDING_SETTLEMENT_WINDOW         4
    AMBIGUOUS_MULTIPLE                3
    DISAMBIGUATED_BY_AMOUNT           2
    CORROBORATED                      1
    ALL_CANDIDATES_REJECTED           1
    SEMANTIC_CONFIRMED                1
  3497/3504 records decided without any model call (99.80%); 7 verifier calls in total
  23 records ended unmatched (PolicyConfig.max_aggregation_candidates is 40; above it the aggregation pass is skipped)

  scenario records
  record       outcome        exception                classification               scenario
  BR-4471      HUMAN_REVIEW   AGGREGATED_SETTLEMENT    NO_ADMISSIBLE_CANDIDATE      S4a aggregated settlement
  BR-4472      HUMAN_REVIEW   AGGREGATED_SETTLEMENT    NO_ADMISSIBLE_CANDIDATE      S4a aggregated settlement
  BR-4473      HUMAN_REVIEW   AGGREGATED_SETTLEMENT    NO_ADMISSIBLE_CANDIDATE      S4a aggregated settlement
  BR-4481      EXCEPTION      FEE_TAX_INCONSISTENT     EXACT_REFERENCE              S6 fee/tax arithmetic
  GLX-204880   HUMAN_REVIEW   AMBIGUOUS_MATCH          SEMANTIC_UNRESOLVED          S17 two equally plausible candidates
  GLX-206115   EXCEPTION      MISSING_SETTLEMENT       NO_ADMISSIBLE_CANDIDATE      S20 missing settlement
  GLX-207209   HUMAN_REVIEW   AMBIGUOUS_MATCH          AMBIGUOUS_MULTIPLE           S16 one reference reported twice
  ORD-7021     EXCEPTION      MISSING_SETTLEMENT       ALL_CANDIDATES_REJECTED      S2a semantic bank narration
  ORD-7031     EXCEPTION      MISSING_SETTLEMENT       NO_ADMISSIBLE_CANDIDATE      S3 identical-amount trap
  ORD-7032     RECONCILED     -                        EXACT_REFERENCE              S3 the trap's twin
  ORD-7034     EXCEPTION      MISSING_SETTLEMENT       NO_ADMISSIBLE_CANDIDATE      S5b missing
  ORD-7036     RECONCILED     -                        DISAMBIGUATED_BY_AMOUNT      S7 refund offset
  ORD-7037     EXCEPTION      REFUND_MISMATCH          DISAMBIGUATED_BY_AMOUNT      S7b chargeback not booked
  ORD-7038     EXCEPTION      CURRENCY_MISMATCH        EXACT_REFERENCE              S9 currency mismatch
  ORD-7040     HUMAN_REVIEW   LOW_CONFIDENCE_MATCH     SEMANTIC_CONFIRMED           S11 truncated reference
  ORD-7104     HUMAN_REVIEW   AMBIGUOUS_MATCH          AMBIGUOUS_MULTIPLE           S8 same payment in two sources
  POS-300412   EXCEPTION      AMOUNT_MISMATCH          EXACT_REFERENCE              S18 amount mismatch nothing explains
  POS-300771   HUMAN_REVIEW   AGGREGATED_SETTLEMENT    NO_ADMISSIBLE_CANDIDATE      S19 second aggregation
  POS-300779   HUMAN_REVIEW   AGGREGATED_SETTLEMENT    NO_ADMISSIBLE_CANDIDATE      S19 second aggregation
  POS-302640   EXCEPTION      MISSING_SETTLEMENT       NO_ADMISSIBLE_CANDIDATE      S20 missing settlement
  POS-303551   EXCEPTION      PENDING_SETTLEMENT       PENDING_SETTLEMENT_WINDOW    S21 pending, not due yet
  SH-88211     HUMAN_REVIEW   AMBIGUOUS_MATCH          SEMANTIC_UNRESOLVED          S10 ambiguous for the model too
  WB-104217    HUMAN_REVIEW   AMBIGUOUS_MATCH          SEMANTIC_UNRESOLVED          S13 semantic, second bank account
  WB-104931    HUMAN_REVIEW   AMBIGUOUS_MATCH          SEMANTIC_UNRESOLVED          S14 merchant alias
  WB-105204    RECONCILED     -                        CORROBORATED                 S15 gateway-reformatted reference
  WB-105633    HUMAN_REVIEW   AMBIGUOUS_MATCH          AMBIGUOUS_MULTIPLE           S16 one reference reported twice
  WB-106402    EXCEPTION      MISSING_SETTLEMENT       NO_ADMISSIBLE_CANDIDATE      S20 missing settlement
  WB-106988    EXCEPTION      PENDING_SETTLEMENT       PENDING_SETTLEMENT_WINDOW    S21 pending, not due yet
  WB-107455    EXCEPTION      PENDING_SETTLEMENT       PENDING_SETTLEMENT_WINDOW    S21 pending, not due yet
  ZB-6104      HUMAN_REVIEW   AMBIGUOUS_MATCH          SEMANTIC_UNRESOLVED          S2b semantic marketplace payout
  ZB-6107      EXCEPTION      PENDING_SETTLEMENT       PENDING_SETTLEMENT_WINDOW    S5a pending
```

Read that with the offline verifier in mind. Every deterministic scenario
lands where §3 says it should, with no model involved. The semantic
records behave exactly as designed for a run with no model: `ORD-7021`
was retrieved, admitted as a candidate and then rejected
(`ALL_CANDIDATES_REJECTED`); `ZB-6104`, `WB-104217` and `WB-104931` were
admitted and left unresolved. **They cannot resolve without a real
provider — that is what makes them semantic cases rather than fuzzy
ones.** `ORD-7040` shows the confidence gate working end to end: the
offline heuristic returned SAME at 0.70, below the 0.85 threshold, so
policy routed it to HUMAN_REVIEW rather than accepting it.

`--verify` asserts each of those 31 rows against a recorded expected
offline outcome, so this table cannot silently drift.

Reproducibility was checked by generating the whole workspace into two
clean directories and diffing: all 22 outputs byte-identical, XLSX
included.

---

## 5. Timings

Measured on the development machine, single run each. These are
observations, not guarantees.

| | |
|---|---|
| `POST /runs/sample` — read, detect, classify and store all 21 files | **0.47 s** |
| `POST /runs/{id}/execute` — map every source and reconcile 3,504 records against 3,559 settlements | **0.35 s** |
| `--verify` ingestion, in-process (read + detect + classify + map, 20 sources) | **0.52 s** |
| `--verify` reconciliation, in-process (`process_batch`, 3,504 records) | **0.16 s** |

Both API calls are comfortably inside a live demo; neither needs a
loading state to hide anything. In the `/runs/sample` run recorded above the backend reported
`AI_AVAILABLE` on both providers (Gemini primary, Groq fallback), and the whole 3,504-record
reconciliation spent **7 model calls** — the semantic residue and nothing
else. `ORD-7021`, `ORD-7040`, `WB-104217`, `WB-104931` and `GLX-204880`
reconciled with `ai_invoked=yes`; `ZB-6104` went to review.

Two operational notes worth knowing before you change the data:

- **`PolicyConfig.max_aggregation_candidates` is 40.** Above that many
  unmatched records, `detect_aggregated_settlements` skips itself
  entirely — deliberately, since subset-sum is exponential and a
  "unique" decomposition among thousands of unmatched records is a
  coincidence rather than a finding. This workspace ends a run with 23
  unmatched, and `--verify` fails loudly if that ever crosses 40. It is
  why the missing-settlement rate here (6 in 3,504) is lower than a real
  month's: the alternative would be a workspace where the aggregation
  finding silently disappears.
- **The aggregation pass costs O(unclaimed settlements × subsets of the
  unmatched pool).** Settlements already claimed by some record are
  skipped, which is why 3,553 settlements cost nothing measurable here.

---

## 6. A five-step recording sequence

**Step 1 — load the workspace (about 20 seconds).**
One click: `POST /runs/sample` brings in all 21 files, server-side,
through exactly the path an upload takes. Twenty sources, seven thousand
rows, two file formats, paise in one file and `₹1,23,456.78` in another,
a bank export with a title block, a bank statement that writes money out
as `(1,18,450.00)`, and a collections advice that writes it as
`21,600.00 Cr`. Nothing was pre-configured: each file's schema is
inferred, and each guess carries a confidence and a reason. Point at the
inventory: the duplicate ICICI upload is already flagged, and
`collections_settlement_advice_mar2026.csv` — which carries no vendor
name anywhere — still classifies as a settlement source at 0.97 with
`provider` blank. Naming the vendor is a label Accord adds when the
evidence supports it, never the thing that decides how a file is read.

**Step 2 — the question it refuses to skip.**
The run is **Blocked**. `zoho_books_invoices.csv` classified at 0.54,
below the 0.65 threshold — Accord worked out it is a ledger but is not
confident enough to reconcile money on that guess. Confirm it. Then open
`orders_shopify_export.csv` and map `Name` to the reference column. This
is the argument for the whole ingestion design: a reconciliation tool
that mis-reads a column is worse than one that asks.

**Step 3 — run it, and let the boring part be boring.**
3,423 of 3,504 records reconcile on exact references. 99.8% of the batch
is decided without a single model call. Say the number, say the model was
not involved, move on — the credibility of the interesting cases depends
entirely on the ordinary ones being handled by arithmetic.

**Step 4 — the refusals.**
Open `ORD-7031`. There is an ₹8,650.00 credit one day later, the amount
matches to the paisa, and the system refused it — because `INV-3042` and
`INV-3117` are references from the same numbering system naming different
transactions. Then `ORD-7032`, where that ₹8,650.00 settlement matched
its actual owner. This is the moment worth the most: the system declined
free-looking money and said why.

Follow it with three refusals of three different kinds, all
deterministic: `POS-300412` (the payout is ₹414.00 short and nothing in
the workspace explains the difference), `ZB-6107` (pending, not missing —
no settlement is due until 2026-04-16), and `BR-4481` (the payout file's
own arithmetic is off by ₹718.28).

**Step 5 — where the model earns its place.**
`WB-104931`. Books: "Quarterly platform licence — Trisool Online **Pvt
Ltd**", ₹58,940.00. Bank: `RTGS INWARD TRISOOL ONLINE **SERVICES**`,
`UTR9930287`. No shared identifier — the order number is six digits, the
UTR is seven, they are not the same kind of thing — so the deterministic
tiers have nothing, and the alias is the only link. Show the
investigation trace, the confidence, and that **policy**, not the model,
decided the outcome.

Then show `SH-88211`, where both sides say nothing useful and the correct
answer is "a person should look at this", and close on the two
aggregations — `BR-4471`/`BR-4472`/`BR-4473` → `UTR881318` (₹42,300 +
₹27,850 + ₹9,415 = ₹79,565.00) and `POS-300771`/`POS-300779` →
`UTR9930518` (₹12,340 + ₹8,915 = ₹21,255.00) — as **proposals**, not
auto-matches, and on the audit trail.

If there is time for a sixth beat: `WB-105204`, where the gateway wrapped
the merchant reference in `UPI/WB105204/COLL` and the deterministic fuzzy
tier recovered it from the shared identifier core with no model call at
all. It is the quiet counterpart to Step 5 — most "AI problems" in
reconciliation are not AI problems.

---

## 7. What this data is not

- **It is not an accuracy measurement.** Ground truth here is the
  construction of each record, and the correct outcomes in §3 follow from
  that construction. Accuracy numbers come from `backend/evaluations/`
  and `docs/EVALUATION_METHODOLOGY.md`, on held-out data this workspace
  has no part in.
- **It is not tuned against the engine.** No threshold was changed to
  make a record land where this document says it should. Where the
  offline run in §4 differs from §3 — the semantic records — the
  difference is reported rather than papered over.
- **The with-a-model outcomes are not asserted.** `--verify` runs with
  `ACCORD_AI_DISABLED=1` and asserts only the offline result. The live
  numbers in §5 are one observed run, reported as such.
- **Every business name in it is fictional**, on both the hand-written
  and the generated side.
