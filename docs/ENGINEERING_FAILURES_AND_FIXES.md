# Engineering failures and fixes

Every defect below was actually found in this codebase. Nothing here is
invented to look thorough, and nothing found has been left out to make
the system look better than it is. Where a fix is partial or a problem is
still open, it says so.

The ordering is by how much each one mattered, not by when it was found.

---

## 1. The candidate ranker made the model look wrong for its own mistake

**What failed.** Evaluation V1 reported that Gemini correctly reconciled
0 of 8 held-out `semantic_true_match` records, and that finding was
written up as a limitation of the model. It was not.

**How it was discovered.** Before changing anything, the same category
was examined on the *dev* split (33 records) to understand the mechanism
rather than to tune against the held-out set. The diagnostic printed,
for each record, which candidate the ranker selected and where the true
counterpart ranked.

```
fuzzy ranker picked TRUE match : 0/33
true match rank when present   : median=35 min=11 max=65
candidate pool after filters   : ~600
```

The model had never been shown the right record. For every one of those
33 records it was asked to compare the merchant order against some other
transaction, and it answered DIFFERENT — which was correct for the pair
it was given.

**Root cause.** Candidates were ranked on unweighted description Jaccard
alone:

```python
scored = sorted(((normalize.jaccard(merchant.description, r.description), r) for r in pool), ...)
```

Settlement descriptions are template text. Words like *payment*, *order*,
*customer*, *checkout*, *settlement* appear in nearly every record, so an
unrelated transaction sharing that boilerplate scored higher than the
genuine counterpart, whose wording differed. Meanwhile the true match
agreed on the exact amount and shared the order number inside a
differently-formatted reference (`ORD.200427.CHK` vs `RZP/200427/SETL`) —
two strong signals the ranker never looked at.

A concrete case from the dev split:

| | amount | reference | selected |
|---|---|---|---|
| merchant `ORD200427` | 1,337,600 | `ORD.200427.CHK` | — |
| **picked** `pay_0000200706` | 1,157,400 | `RZP/200706/SETL` | Jaccard 0.417 |
| **true** `pay_0000200427` | **1,337,600** | `RZP/**200427**/SETL` | Jaccard 0.250, rank 65 |

**Impact.** Every genuinely-ambiguous record was decided on the wrong
evidence, and the resulting failures were attributed to the model rather
than to the retrieval feeding it. That is worse than a wrong number: it
points future work at the wrong component.

**Fix.** `matching.score_candidate` now computes a composite score over
four signals — amount agreement (0.45), shared reference core (0.25),
date proximity (0.10), and IDF-weighted description similarity (0.20).
IDF weighting is the specific antidote to the boilerplate problem: a
token present in every description carries almost no weight, so the
distinctive tokens decide the ranking. Amount and reference-core lookups
are indexed, so the true counterpart is found even when the wording
shares nothing at all.

**Regression tests.**
`test_hardening.py::test_shared_boilerplate_does_not_outrank_the_genuine_counterpart`
reconstructs the exact case above and asserts the true record ranks
first. `test_ranker_surfaces_a_counterpart_that_shares_no_wording_at_all`
covers the amount-index path.

---

## 2. Concluding the model was weak without checking what it was shown

**What failed.** A process failure rather than a code one, and the reason
defect #1 survived a full evaluation cycle. V1's write-up reasoned from
an aggregate ("Gemini got 0/8") straight to a conclusion about the
model's capability. The intermediate question — *what pair was it
actually asked about?* — was never asked, and the number was consistent
with a model limitation, so it was accepted as one.

**Impact.** A real, fixable retrieval bug was documented as an inherent
limitation of `gemini-3.5-flash-lite`.

**Fix.** Two things, one procedural and one in the code. The semantic
verifier now receives a structured `CandidateComparison` including both
references and the deterministic signals, and every decision records the
evidence it was made on, so a wrong answer can be traced to the input
that produced it. Procedurally: an aggregate about a component is no
longer accepted without inspecting the inputs that component received,
which is exactly what the dev benchmark exists to make cheap.

---

## 3. No currency check anywhere in the financial checks

**What failed.** Amounts are integers in minor units with no currency
attached. A merchant record for ₹1,000 (100000 paise) and a settlement
of $1,000 (100000 cents) compared equal, passed every check — amount,
fee/tax arithmetic, timing, refund — and reconciled clean.

**How it was discovered.** Reading `_run_financial_checks` while
enumerating realistic failure modes. Both models carry a `currency`
field; nothing compared them.

**Impact.** Silent cross-currency reconciliation. This is the most
serious class of defect for this product, because it produces a
confident RECONCILED on records that are not comparable at all — the one
outcome the system is supposed to never get wrong.

**Fix.** `currency_match` runs first in `_run_financial_checks`, before
any amount comparison, and a mismatch is a FAIL (so, an EXCEPTION).
Comparison is case-insensitive.

**Regression tests.**
`test_same_number_in_a_different_currency_is_not_reconciled`,
`test_currency_comparison_is_case_insensitive`.

---

## 4. Re-running a dataset silently stole the earlier batch's records

**What failed.** `records` was keyed on `record_id` alone and written
with `INSERT OR REPLACE`. Record ids come from the dataset, so processing
the same records again — a normal operation after a policy change — moved
every row out of the first batch into the second. The first batch still
reported its original `processed_records`, but listing it returned fewer
and fewer records.

**How it was discovered.** Enumerating what "repeated batch execution"
would actually do, then reading the schema.

**Impact.** An audit-facing inconsistency: a batch claiming to have
processed 999 records while showing 40. For a reconciliation tool whose
main claim is an inspectable trail, a batch that cannot produce the
records it says it processed undermines the whole point.

**Fix.** `PRIMARY KEY (batch_id, record_id)`, plus a migration in
`init_db` that rebuilds the table if it still has the old key (announced,
not silent — the rows are derived and reproduced by re-running).
`get_record` takes an optional `batch_id`; the API and the console pass
it so opening a record shows that batch's decision.

**Regression tests.**
`test_re_running_the_same_records_keeps_both_batches_intact`,
`test_record_lookup_can_select_a_specific_batch`,
`test_api.py::test_re_running_the_same_dataset_leaves_the_first_batch_listable`.

---

## 5. `/batch/latest` was unreachable, and changed shape when it wasn't

**What failed.** Two defects in one endpoint.

`@router.get("/batch/{batch_id}")` was registered before
`@router.get("/batch/latest")`. Starlette matches in registration order,
so `/batch/latest` was always handled as a batch whose id was the literal
string `latest`, and always 404'd.

Separately, the handler returned the batch object directly when it found
one but `{"batch": null}` when it did not. The console's `if (b.batch)`
check was therefore false in both cases, so it never restored a batch on
load.

**How it was discovered.** Clicking through a running server. Neither
was visible to the unit tests, and the second is invisible to a casual
curl because both responses are valid JSON with a 200.

**Impact.** The console silently lost its batch on every reload.

**Fix.** Route registration reordered; the response is always
`{"batch": ...}`.

**Regression tests.**
`test_batch_latest_is_not_shadowed_by_the_parameterised_batch_route`,
`test_batch_latest_keeps_one_shape_whether_or_not_a_batch_exists`, and
`test_route_ordering_puts_literals_before_their_parameterised_siblings`,
which generalises the first: it walks every registered route and fails if
any literal segment is shadowed by a sibling path parameter, so the next
route added in the wrong order fails immediately.

---

## 6. `limit=0` processed the entire dataset

**What failed.** `if limit and len(records) >= limit` — zero is falsy, so
requesting a batch of zero records read the whole file. Asking for
nothing ran 4,001 records.

**How it was discovered.** A test written for the "empty batches"
category, which failed with `assert 4001 == 0`.

**Impact.** Minor in a demo, unpleasant in general: the smallest possible
request triggered the largest possible job.

**Fix.** `if limit is not None`, and `ge=0` validation on the field. The
first fix was itself wrong — it checked the limit after appending, so
`limit=0` still returned one record — which the same test caught.

**Regression test.** `test_an_empty_batch_completes_cleanly`.

---

## 7. One malformed row aborted an entire batch

**What failed.** `_load_split` validated each row with no error handling.
A single malformed line in a merchant export raised, and the whole batch
request failed.

**Impact.** One bad row out of thousands stopped every other record from
being reconciled — the opposite of the per-record resilience the batch
loop already had.

**Fix.** Rows are validated individually; a bad row is skipped and
reported in `rejected_rows` on the response and in the `BATCH_STARTED`
audit event. Skipped, but never silently.

Note the deliberate asymmetry: `evaluate.py` still fails loudly on a
malformed row. A corrupt evaluation dataset should stop an evaluation,
not quietly shrink it.

---

## 8. The audit stream re-read the entire log ten times a second

**What failed.** The SSE endpoint called `get_full_log()` on every poll —
every 100ms, per connected client — and filtered in Python. It also
started every connection at sequence 0, so each new client (or each
reconnect) was sent the entire ledger.

**Impact.** Fine at a hundred events. At the 50,000-record scale the
throughput suite covers, it is a full table read ten times a second per
viewer, and a reconnect mid-batch replays everything.

**Fix.** `get_events_since(seq, limit)` does the filtering in SQL with a
bound. Frames carry `id:`, the endpoint honours `Last-Event-ID` and an
explicit `?since=`, and it defaults to the current head. Keepalive
comments every ~15s stop idle connections being culled.

**Regression tests.** `test_events_since_returns_only_newer_events`,
`test_events_since_is_bounded_by_limit`, and four tests on
`resume_point`, which was extracted as a pure function precisely so
resumption could be tested without opening a stream that never ends.

---

## 9. Fixing the stream broke the audit view (a regression I introduced)

**What failed.** With the stream correctly starting at the head, the
audit page showed **"Event ledger (0) — No events yet"** directly beside
a chain-integrity panel reporting **"102 events verified"**. The view had
been relying on the stream replaying all history to populate itself.

**How it was discovered.** The browser verification run, on the very
first pass after the SSE change. It is visible in a screenshot and
invisible to every backend test — both the stream and the verifier were
behaving exactly as specified.

**Impact.** The audit trail, which is the feature that makes every other
claim checkable, appeared empty.

**Fix.** History is a query, not a stream: `GET /audit/log` returns
existing events plus the current `head_seq`, and the view loads that
first, then tails the stream from that sequence. Incoming events are
de-duplicated by `seq`.

**Lesson recorded rather than glossed:** a correct fix to one component
broke another that depended on the old behaviour, and only an end-to-end
check caught it. That is the argument for the browser run existing at all.

---

## 10. Candidate search was records x settlement population

**What failed.** `nearby_by_date` scanned the entire population per
record. The throughput suite made it obvious: 5x the records took 19.6x
the time.

**Impact.** At 50,000 records against a 48,000-record population this is
billions of comparisons. The scalability evaluation would not have
finished.

**Fix.** Three changes. Records are bucketed by day, so the window scan
is proportional to the window. Exact gross amount and reference core are
indexed for O(1) lookup, which is where a genuine counterpart is almost
always found. The date-window scan — the fallback used when neither
index hits, i.e. a genuinely missing settlement — is bounded by
`max_window_scan_candidates` (400).

**Measured result.** 5,000 records went from 2.18s to 0.99s. Scaling is
now linear from 5k upward: 5k→10k is 2.1x time for 2x records, 10k→50k
is 5.4x for 5x, and throughput stays flat at roughly 4,400–5,000
records/sec. 50,000 records complete in 11.4s.

**Regression test.** `test_window_scan_is_bounded_so_a_large_population_stays_tractable`.

---

## 11. Negative amounts silently inverted the amount signal

**What failed.** Nothing forbade a negative `amount_minor`. In
`score_candidate` the relative-difference term divides by the merchant
amount, so a negative amount produced a negative relative difference and
pushed the amount component *above* 1.0 — a corrupt record scored as
better-than-perfect evidence.

**Fix.** `ge=0` on the amount fields of both models. Malformed input is
rejected at the boundary rather than scored.

This changed an existing test's contract. `test_negative_amount_still_processes_without_crashing`
asserted the engine tolerated a negative amount and returned EXCEPTION;
it is now `test_negative_amount_is_rejected_before_it_reaches_the_engine`.
The change is deliberate: a negative order amount is corrupt input, not a
reconcilable business condition, and the loader-level skip (#7) is what
keeps one such row from stopping a batch.

---

## 12. Two threshold defects in the new matching logic

Both were found by tests written against the new code, before it went
anywhere near an evaluation set.

**`deterministic_match_score` was unreachable.** Set to 0.75, while the
maximum score achievable without a shared reference core is exactly
0.45 + 0.10 + 0.20 = 0.75. A record with an exact amount, the same date,
and 0.8 text similarity scored 0.71 and escalated to the model instead of
resolving for free. Caught by
`test_strong_fuzzy_match_never_calls_the_model`. Lowered to 0.70.

**Shared digits plus an equal amount auto-matched.** The benchmark's
`reference_core_collision` case — an invoice counter on one side
carrying the same digits as an order number on the other, same amount,
two days apart — scored 0.82 with three corroborating signals and was
matched deterministically. Wrong: the two records describe different
products. Deterministic resolution now also requires
`deterministic_min_text_similarity` (0.25), so identifier and amount
agreement that the wording does not corroborate escalates instead of
resolving. Correct rejection on that variation went from 0% to 100%.

**Regression test.** `test_shared_digits_plus_equal_amount_alone_does_not_auto_match`.

---

## 13. Two UI defects, both found only in a real browser

**The policy threshold was hidden on most records.** It was rendered
only inside the `ai_invoked` branch, so the majority of records — the
ones resolved deterministically — showed no threshold at all, despite
the record view existing to explain what governed the decision. Now
shown either way, with the non-AI case stating that the threshold
applies only to model-resolved matches.

**The header pushed the page 193px sideways on a phone.** `.topnav-right`
holds two fixed-width status chips in a non-wrapping flex row. At 390px
they extended to x=583. Measured precisely by enumerating every element
wider than the viewport, which also cleared the records table — its
`overflow-x` container was already working. Fixed by wrapping the header
and hiding the backend-status chips below 720px, plus scroll containers
on both wide tables.

**Regression test.** Both are checks in `frontend/e2e/verify-ui.mjs`
(`detail shows policy threshold`, `no horizontal overflow at 390px`).

---

## 14. A test-design failure of my own

The first version of the SSE tests drove the live stream through
`TestClient` and blocked forever: the endpoint never completes by design,
so `iter_lines()` waits for data that only arrives on a timer, and the
deadline check only ran when a line arrived. The run had to be killed
twice, and the output was hidden because it was piped through `tail`,
which buffers until the process exits.

Recorded because it cost real time and the lesson generalises: an
endless stream is not testable by reading from it. The logic that
mattered — where a client resumes from — was extracted into
`resume_point`, a pure function, and tested directly. The wire format is
verified by the browser run and by curl against a live server.

---

## 15. The hardened engine is more dependent on the semantic backend, and that costs recall when the backend is weak

**What it is.** Not a bug, but a real and initially unwelcome result,
found by running both engines over the same dataset with
`compare_engines.py`. Old commit and current working tree, identical V2
records, deterministic heuristic backend on both sides:

| | old | new | change |
|---|---:|---:|---:|
| Reconciliation accuracy | 99.1% | 96.1% | **−3.0%** |
| Exception precision | 97.6% | 100.0% | +2.4% |
| Exception recall | 97.6% | 79.3% | **−18.3%** |
| False auto-reconciliation rate | 0.0% | 0.0% | 0.0% |
| False exception rate | 0.5% | 0.0% | −0.5% |
| Routed to human review | 2.5% | 5.9% | +3.4% |

**Why.** Two changes push more records into the semantic tier. The
exact-amount index surfaces a candidate for records that previously found
none, and `deterministic_min_text_similarity` refuses to resolve
identifier-plus-amount agreement that the wording does not corroborate.
Both are deliberate. The consequence is that a genuinely missing
settlement whose amount coincidentally matches an unrelated record in the
pool now gets escalated rather than dismissed — and with a backend that
cannot make that judgement, it lands in HUMAN_REVIEW instead of
EXCEPTION. `missing_settlement` moves from 58 EXCEPTION to 34, and the
decoy and true-match categories shift from EXCEPTION to HUMAN_REVIEW.

**How to read it.** Safety is unchanged: false auto-reconciliation stays
at 0.0% and exception precision actually improves to 100% — the new
engine never wrongly flags an exception. What it loses is decisiveness.
It converts confident-but-sometimes-wrong EXCEPTIONs into "ask a human",
which is more manual work and a worse accuracy score.

The architecture now assumes a semantic backend capable of the judgement
it defers. The ablation supports that assumption — Gemini scores 100%
correct rejection on exactly these cases while the heuristic scores 40% —
but the dependency is real and is the honest cost of the redesign. A
deployment without a model key should expect this profile, which is why
`enable_semantic_matching` exists as an explicit switch rather than a
silent fallback.

**Deliberately not fixed.** The obvious tightening — do not spend an
escalation on a candidate whose *only* corroboration is an exact amount —
would likely recover most of that recall. It has not been made, because
this comparison was measured on the V2 dataset, and changing the
implementation in response to a held-out result is precisely the thing
the evaluation protocol forbids. It is recorded here as the first change
to make in the next cycle, to be developed on dev data and measured on a
V3 set.

---

## 16. Amount agreement was treated as evidence of identity

**What failed.** The root cause of the Evaluation V2 regression, and the
single most consequential defect found in this phase. Candidate retrieval
and candidate evidence were the same thing: whatever the indexes returned
became a candidate.

The indexes are tuned for recall — exact amount, shared reference core,
date window. In a population of a few thousand settlements, two unrelated
payments sharing an exact amount is ordinary, not remarkable. So a
genuinely missing settlement would reliably surface a coincidence, the
pipeline would escalate it to the model, the model would decline to rule
it out (correctly — it was told to prefer AMBIGUOUS over a confident
wrong answer), and the record landed in HUMAN_REVIEW instead of being
reported as the missing settlement it was.

V2 read this as the system becoming conservative. It was not conservatism.
It was a category error.

**How it was discovered.** Not from V2 — the held-out data that revealed
the symptom cannot be used to design the fix. 312 development scenarios
(`data/generate_settlement_scenarios.py`) were built to isolate the
question: when no correct match exists, can the system tell that apart
from a coincidence? Baseline on that set:

```
overall              85.3%
absent (no counterpart exists)   62%
wrong-record matches             24
model calls per 1,000            417
```

`amount_collision_boilerplate_text` scored **0%**, selecting the wrong
record every time. Only the confidence gate stopped those becoming false
reconciliations — the system was one confident model verdict away from
booking the wrong settlement.

**Root cause.** Measuring the signals directly separated the two
populations cleanly:

| top-ranked candidate | shared reference core | text similarity |
|---|---:|---:|
| genuine counterpart | **100%** | 0.78 |
| every coincidental one | **0%** | 0.06 – 0.63 |

Text does not separate them — boilerplate reached 0.63 against a genuine
0.78. Shared identifiers separate them perfectly.

**Fix.** Retrieval and admissibility became separate questions. A
retrieved record must carry identity evidence to become a candidate at
all, and the discriminator is *negative* evidence: when both sides carry
a recognisable identifier and the identifiers disagree, that is a
statement that the records differ, not a failure to prove they match.

**Result on the development scenarios:**

```
overall              85.3%  ->  100%
absent               62%    ->  100%
wrong-record         24     ->  0
model calls / 1,000  417    ->  0
```

The cost dropped to zero because coincidences no longer justify an API
call.

**A first attempt that was wrong.** The initial rule admitted candidates
only above 0.50 text similarity. That broke seven AI-path tests, and the
tests were right: a floor that high starves the semantic tier of exactly
the ambiguous cases it exists to judge. Lowered to 0.20 — wording is a
weak separator and was never what should reject coincidences.

**Regression tests.** `test_evidence_and_admissibility.py`, 19 tests,
including that the amount-only guard and the contradiction guard stay
independent.

---

## 17. Two orders could reconcile against one settlement

**What failed.** Detected since the previous phase, unresolved. Each
record is decided in isolation, so two orders could each match the same
payment and each look perfectly reconciled. That is double-counted
revenue, and no per-record check can see it.

**Fix.** Conflicts are settled on evidence tier — an exact reference
match outranks a semantic verdict — and a tie at the top demotes every
claimant to review rather than deciding by position in the batch.
Order-dependent outcomes are indistinguishable from wrong ones once
someone audits them.

Global assignment (bipartite matching) was considered and rejected: it
makes one record's outcome depend on every other record in the batch,
which cannot be explained to the operator who has to act on it.

**Regression tests.** `test_claim_integrity.py`, 13 tests, including
order-independence, batch isolation, concurrency, and that a shared
`settlement_id` is aggregation rather than a conflict.

---

## 18. A settlement that was not due yet was reported as missing

**What failed.** Nothing distinguished "no settlement exists" from "no
settlement is owed yet". A payment captured hours ago was reported as a
missing settlement — a false positive with a phone call to the provider
attached.

**Fix.** An `as_of` observation point plus `settlement_expected_days`
(T+2). Records inside the window are classified PENDING_SETTLEMENT at low
severity with "re-run after the window" as the action. Without an `as_of`
the check is skipped rather than guessed, because inventing a "now" would
manufacture a finding.

---

## 19. Three accessibility defects, found only in a browser

**Record rows were click-only.** A keyboard user could not open a record
at all — the entire inspection surface was unreachable. Rows are now
focusable controls with Enter/Space handling and accessible names.

**No h1 on two of three pages.** Page titles were styled divs, so the
document had no heading structure to navigate by.

**Severity was carried by colour alone** in the first draft of the review
queue. Now shape and text as well, so it survives greyscale and every
form of colour blindness.

Browser suite went from 22 to 44 checks covering keyboard operability,
landmarks, accessible names, table semantics, positive-tabindex
hijacking, reduced motion, and three viewport widths.

---

## 20. Cleanly-matched records were scored twice

**What failed.** Every exact-reference match ran `score_candidate` a
second time — full IDF similarity, reference-core extraction — purely to
build a candidate-evidence table. On the ~80% of records that reconcile
cleanly there is nothing to explain: the reference matched.

**How it was discovered.** The throughput suite. p50 per-record latency
had tripled from 0.010ms to 0.026ms after the admissibility work.

**Fix.** Evidence is built only where there is a refusal or a competing
claim to justify. p50 0.026ms → 0.012ms, 50,000 records 13.77s → 12.99s,
peak RSS 772MB → 705MB.

---

## 21. A Gemini benchmark run that measured rate limits, not the model

**What happened.** The ablation reported Gemini at 80.8% accuracy against
the heuristic's 84.6% — apparently worse. The p95 latency was 10,010ms,
which is exactly the configured semantic timeout, and the run had made
only 64 calls where an earlier run made 157.

That is the signature of throttling, not of model quality: a timed-out
call degrades to HUMAN_REVIEW, which scores as a miss.

**Fix.** The benchmark now counts `PROVIDER_ERROR` classifications
separately and prints them, so a degraded run is visibly degraded rather
than being read as evidence about the model. Reporting the first number
without that caveat would have been the most misleading thing in this
document.

---

## 22. Column detection assigned by position, and reconciled an order book against nothing

**What failed.** Every record in the first demo run came back a missing
settlement. Not some — all eighteen.

**Root cause.** Detection walked columns in file order and let the first
plausible match claim a canonical field. In the order book, `order_id`
appears before `invoice_ref`, and both match the reference pattern. So
`order_id` claimed the reference slot, the ledger side referenced
`ORD-5001`, the gateway side referenced `INV-2048`, and nothing could
ever match.

The same header means different things in different files: `order_id` is
the cross-system reference in a gateway export and the file's own primary
key in an order book. Which reading is right depends on what else the
file contains, and position cannot express that.

**Fix.** Every header now yields all its plausible readings with a
confidence, and assignment is a global best-fit: the highest-confidence
pairing wins and consumes both the column and the field. The
`order_id → transaction_id` reading is deliberately weighted below the
reference reading (0.70), so it loses that contest by default and wins
only when nothing better claims the reference slot.

**How it was found.** Building the demo dataset and running it. No unit
test would have caught it — each component was behaving exactly as
specified.

**Regression tests.**
`test_ingest.py::test_an_order_book_does_not_let_its_own_key_claim_the_reference_slot`,
`test_a_gateway_export_is_detected_without_help`.

---

## 23. The fee/tax check could not fail on uploaded data

**What failed.** A payout file with deliberately broken arithmetic
reconciled cleanly.

**Root cause.** The mapper derived `net = gross − fee − tax − refund`
when building settlement records. The `fee_tax_arithmetic` check then
verified that same subtraction — a tautology. The check could not fail on
uploaded data no matter what the file said.

**Impact.** One of the five deterministic financial checks was silently
inert for the entire upload path, which is the product's main path.

**Fix.** A stated net amount is read from the file and kept; the
derivation is a fallback for files that do not carry one. `net_amount` is
now a recognised canonical field.

**Regression test.** `test_a_stated_net_is_kept_rather_than_recomputed`.

---

## 24. Reference contradiction rejected every genuine bank match

**What failed.** The truncated-bank-narration case — the one the semantic
tier exists for — never reached the model. It was dismissed before that.

**Root cause.** The contradiction rule fired between an invoice number
(`INV-2057`) and a bank UTR (`UTR774120`). Both are digit runs, they
disagree, so the rule concluded the records were different transactions.

But they are not disagreeing identifiers — they are identifiers from
different numbering systems. A bank UTR has no relationship to a merchant
invoice number and never will. Their disagreement carries no information,
and treating it as negative evidence rejects every bank-statement match
on principle.

**Fix.** Contradiction now requires *comparable* identifiers, with digit
width as the proxy: a counter issued by one system produces identifiers
of consistent length. When two identifier sets are incomparable the pair
carries no identifier evidence either way, so amount and date can admit
it and the model judges it.

**Why this one mattered most.** It is the difference between a product
that reconciles gateway exports and one that reconciles bank statements.
Verified live afterwards: `NEFT INWARD CLDPLTFRM RENEWAL NORTHWND`
matched to `Annual cloud platform renewal - Northwind Retail` (SAME,
0.95), while an unrelated credit at an identical amount was refused
(DIFFERENT, 0.95).

---

## 25. Drilling into a run rendered a blank page

**What failed.** After starting a reconciliation, the app showed the nav
bar and nothing else. Nothing threw, no console error, no failed request.

**Root cause.** The run drill-in had been lifted into `App` and folded
into the page-transition key. `AnimatePresence mode="wait"` waits for the
exiting child before mounting the next one, and the exiting subtree had
already been replaced underneath it, so the exit never completed and the
entry never began.

**Impact.** The primary product flow ended in a blank screen. Every
backend test passed; the API had produced a correct, complete run.

**Fix.** `Runs` owns its own list / create / detail navigation, and the
page transition keys on the view alone. A drill-in is not a top-level
view change and should not have been modelled as one.

**How it was found.** The browser suite, on a screenshot. This is the
fourth defect in this project that only a real browser caught.

---

## 26. Two dropped files created two runs

**What failed.** Latent, found by reading the code after the blank-page
fix. `NewRun` held its run in React state, and `ensureRun` checked that
state before creating one. Two files dropped in quick succession would
both read the pre-update value, and each would create its own run —
splitting the sources across two runs that could never reconcile with
each other.

**Fix.** A ref, which updates synchronously.

---

## 27. Aggregation detection cost a third of a large run

**What failed.** The 50,000-record throughput run went from 12.99s to
19.14s after aggregation detection was added.

**Root cause.** The pass scanned every unmatched record for every
settlement: O(settlements × unmatched), roughly 48,000 × 8,000 on a large
batch.

**Fix.** Skipped entirely above `max_aggregation_candidates` rather than
throttled. The performance argument is real, but the second reason is
better: among thousands of unmatched records a "unique" combination
summing to a lump sum is almost certainly a coincidence, so the answer
would be untrustworthy even if it were free. Aggregation detection is
meaningful at the scale a bank statement actually arrives at. 50k back to
15.74s.

---

## 28. A dataset bug that looked like an engine bug

**What failed.** The new evaluation dataset showed a 1.0% false
auto-reconciliation rate — the one metric that must stay at zero.

**Root cause.** Not the engine. The `corrupted_reference` category
transposes two digits of a reference, and transposing two *identical*
digits is a no-op. Those references were byte-identical to the real one,
so they reconciled correctly while being labelled as requiring review.

**Fix.** The generator now finds a pair that actually differs. Rate back
to 0.0%.

**Worth recording** because the first instinct was to look at the
admissibility rules. The dataset is as capable of being wrong as the code
is, and a metric moving in the wrong direction is not by itself evidence
about the engine.

---

## Open, not fixed

**One payment split across several settlements is not representable.**
The data model is one settlement record per payment. Aggregation (many
payments sharing a `settlement_id`) is supported and tested; splitting a
single payment across settlements would require changing the record model
from one-to-one to one-to-many and reworking every check that assumes a
single counterpart. It is not faked. Such data would surface as an amount
mismatch for a human — wrong in its label, safe in its direction, and
visible.

**The reference-contradiction rule has a real false-negative mode.** It
assumes the settlement's `order_reference` derives from the merchant's,
which is what Razorpay's data model specifies. A provider using an opaque
internal id would break it, and a genuine match would be reported as a
missing settlement. Mitigated three ways: the rejected candidate is
listed with its supporting signals so a reviewer sees it immediately, the
failure direction is safe, and
`treat_reference_contradiction_as_negative` turns it off. It is still a
real limitation.

**`product_alias` remains 0% across every configuration.** Investigated
rather than chased. That benchmark variation asserts two records are the
same payment while their references name *different* transactions
(order 91426 against reference SETL91430). That is not an alias problem;
it is a contradictory premise, and a system that matched them would be
matching on amount and date alone — the exact behaviour that caused the
V2 regression. The system now refuses and shows the operator the rejected
record with "amount matches exactly, dated 0d apart" attached, which is a
one-click confirmation for a human and an honest refusal for the machine.

**The generator's ambiguous cases are easier than real ones.** Its
`semantic_true_match` records embed the order number in both
descriptions, so the numeric core is usually recoverable. Real settlement
text is messier. The dev benchmark exists to cover the harder variation —
aliases, abbreviations, gateway noise, no shared identifier — and the
ablation numbers on it should be read as the more honest measure of the
semantic layer.

**Every accuracy number is synthetic.** No real merchant data has been
processed. See `docs/RAZORPAY_INTEGRATION.md` for exactly why, and what
was verified rather than assumed.
