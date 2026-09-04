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

## Open, not fixed

**One settlement can still be claimed by several merchant records.**
Each record is judged in isolation, which is correct per record and blind
across them: two different orders can each match the same payment and
each look perfectly reconciled. That is double-counted revenue and no
per-record check can see it.

`batch.detect_duplicate_claims` now surfaces the collisions, and
`test_one_settlement_claimed_by_two_merchant_records_is_detected` pins
the detection. But detection is not resolution — the affected records
still carry their individual outcomes, and nothing currently forces them
to HUMAN_REVIEW. Deciding which claim is legitimate needs either
one-to-one assignment across the batch (a matching problem, not a
per-record one) or a human. It is listed here rather than quietly left
out.

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
