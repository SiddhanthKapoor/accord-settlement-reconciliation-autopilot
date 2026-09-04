# Evaluation methodology

How every number in this repository was produced, and what each one is
and is not evidence of.

There are four separate measurements here, and they answer different
questions. Collapsing them is the main way this kind of work goes wrong.

| | Question | Data | Used for tuning |
|---|---|---|---|
| **Evaluation V1** | How did the original system score? | 999 held-out records, seed 20260903 | No — frozen |
| **Ablation benchmark** | Does each tier, including the model, earn its place? | 240 dev examples, seed 771 | Yes, by design |
| **Evaluation V2** | How does the hardened system score on data it has never seen? | 1,001 held-out records, seed 20260904 | No — run once |
| **Throughput evaluation** | How does it behave as batches grow? | Freshly generated, 1k–50k | Not an accuracy measure at all |

---

## Dataset generation

`backend/data/generate_dataset.py` is deterministic given `--seed`: the
same seed reproduces byte-identical output. It builds one shared pool of
settlement records — including deliberate orphans, duplicate references,
and text-similar decoys — and a set of merchant records, each constructed
to exercise exactly one scenario category.

**Ground truth is assigned from the category definition at construction
time, never by running the engine and recording what it produced.** That
distinction is what separates a measurement from a tautology: a dataset
labelled by the system under test can only ever report 100%.

### Categories

| Category | Share | Construction | Expected outcome |
|---|---:|---|---|
| `clean_match` | 55% | Amounts agree, `net = gross − fee − tax`, reference matches | RECONCILED |
| `fee_tax_rounding` | 10% | As above with a ±1–2 paise net perturbation | RECONCILED (inside tolerance) |
| `delayed_settlement_normal` | 8% | Settled 4–14 days later | RECONCILED |
| `delayed_settlement_excessive` | 3% | Settled 35–90 days later | EXCEPTION |
| `partial_refund` | 7% | Refund recorded consistently on both sides | RECONCILED |
| `refund_mismatch` | 2% | Refund amounts materially disagree | EXCEPTION |
| `missing_settlement` | 6% | No counterpart generated at all | EXCEPTION |
| `amount_mismatch` | 5% | Merchant amount materially perturbed | EXCEPTION |
| `duplicate_reference` | 2% | Two settlements share one reference, near-identical amounts | HUMAN_REVIEW |
| `ambiguous_text_reference` | 2% | Split three ways below | — |

The ambiguous slice splits into `fuzzy_strong` (high wording overlap,
resolvable deterministically), `semantic_true_match` (genuinely the same
payment, moderate overlap), and `semantic_decoy` (a different payment
sharing generic words — should be rejected).

### Split

Stratified 80/20 by category, not a global random split, so both halves
carry the same category mix and neither is accidentally short of a
scenario type.

---

## Development / evaluation separation

The rule is simple and was the constraint everything else worked around:
**anything used to make a decision cannot also be used to measure it.**

- All diagnosis, threshold selection, and prompt work happened on the
  **dev** split and the **ambiguous benchmark**.
- The V1 held-out set was frozen before any change was made.
- The V2 held-out set was generated after all development finished and
  evaluated exactly once.

Concretely, when the V1 result was investigated (see
`ENGINEERING_FAILURES_AND_FIXES.md` §1), the 33 dev-split records were
examined, not the 8 held-out ones that produced the headline number.

---

## Evaluation V1 — frozen

V1 is the first held-out evaluation. Because the generator itself changed
during hardening, a seed alone no longer reproduces it, so the dataset
bytes are archived rather than regenerated.

`backend/evaluations/v1/` contains the holdout and settlement pool
(gzipped), both backend reports, the dataset manifest, and `FROZEN.json`:
the pinned commit, seed, dataset version, policy config, Gemini model and
temperature, headline metrics, and a SHA256 for every file.

```bash
cd backend
python verify_evaluation_v1.py           # checksums + metric consistency
python verify_evaluation_v1.py --rerun   # also re-runs V1 at its pinned commit
```

`--rerun` checks the pinned commit out into a throwaway git worktree,
feeds it the frozen dataset, and compares recomputed metrics against the
frozen report. It passes.

**One honest caveat, recorded in `FROZEN.json` itself:** only the
heuristic-backend run is byte-reproducible. The Gemini run calls a hosted
model, and temperature 0 is not a guarantee of determinism across time.
The frozen report is a record of what that run produced, not a target to
re-hit.

---

## The ambiguous-matching benchmark

`backend/data/generate_ambiguous_benchmark.py` — 240 examples, 120 true
matches and 120 non-matches, seed 771. Development only.

Its unit is narrower than the main dataset's: one merchant record, a
small candidate pool, and the payment_id of the one that is genuinely the
same payment (or null). That isolates the matching decision from the
downstream financial checks, so a matching regression cannot hide behind
an unrelated arithmetic pass.

**The design constraint that makes it worth anything: amount alone must
not solve it.** Roughly half the true matches sit beside a distractor
with an identical amount, and roughly half the non-matches are single
candidates whose amount matches exactly. A ranker that only looks for an
equal amount scores near chance.

Fourteen variations cover abbreviations, word order, merchant aliases
(legal entity vs trading name), product aliases, punctuation, invoice
formatting, partial reference overlap, gateway noise, customer-name
variation, amount collisions, near-duplicates, reference-core collisions,
identical amounts on different transactions, sequential orders, and
missing counterparts.

Three of those are deliberate traps for the fixes made during hardening:
`reference_core_collision` punishes matching on shared digits alone,
`sequential_orders` punishes matching on wording plus amount, and
`amount_collision_true_match` punishes relying on amount to disambiguate.

---

## Ablation

`benchmark_matching.py` runs the real `policy.reconcile` path — not a
reimplementation — under four configurations, toggled through
`PolicyConfig` rather than by editing code:

- **A** exact normalized-reference matching only
- **B** + deterministic corroborated matching
- **C** + heuristic semantic fallback
- **D** + Gemini semantic verifier

```bash
python benchmark_matching.py --json data/eval_reports/ablation.json
```

The purpose is to answer whether the model earns its place rather than
existing because a hackathon expects one. The metric that settles it is
not accuracy: it is recall **and** correct-rejection together, since a
component can buy one by destroying the other, and one of them does.

---

## Evaluation V2 — run once

Generated after all development finished:

```bash
python data/generate_dataset.py --seed 20260904 --total 5000 --out-dir data/datasets_v2
python evaluate.py --dataset holdout --dataset-dir data/datasets_v2 --label v2
```

**V2 uses the same generator with a different seed, deliberately.** An
improved generator would have made V1 and V2 incomparable — any movement
could then be the engine getting better or the test getting easier, with
no way to separate them. Holding the generator fixed means the difference
is attributable to the engine.

The cost of that choice is that V2 inherits the generator's limitations,
including ambiguous cases that are easier than real ones. That is why the
benchmark above exists and why its numbers, not V2's, are the honest
measure of the semantic layer.

The V2 holdout was not inspected before the run, and no engine, policy,
or dataset code changed after it. Its dataset, report, and checksums are
archived in `backend/evaluations/v2/` on the same terms as V1.

### Result

| Metric | V1 | V2 |
|---|---:|---:|
| Reconciliation accuracy | 97.7% | 94.8% |
| Exception precision | 95.5% | 100.0% |
| Exception recall | 90.9% | 71.6% |
| False auto-reconciliation rate | 0.0% | 0.0% |
| False exception rate | 0.9% | 0.0% |
| Routed to human review | 3.6% | 7.2% |
| AI invocation rate | 7.1% | 5.2% |

V2 is worse on the headline figure. The hardening made the system more
cautious rather than more accurate: it stopped producing
confident-but-sometimes-wrong EXCEPTIONs and now defers those to a human,
which costs 19 points of exception recall and doubles the review queue
while taking exception precision to 100% and false exceptions to zero.
Safety is unchanged.

Nearly all of the movement is one category. `missing_settlement` went
from 47 EXCEPTION / 13 HUMAN_REVIEW to 21 / 39, because the exact-amount
index now surfaces a coincidental candidate that the model, correctly
following its instruction to prefer AMBIGUOUS over a confident wrong
answer, declines to rule out.

### Same data, different code

Comparing V1's published numbers against V2's is weaker than it looks:
they were measured on different records. `compare_engines.py` removes
that confound by checking the pre-hardening commit out into a throwaway
worktree, handing it the *V2* dataset, and scoring both engines on
identical input with the deterministic backend on both sides.

```bash
python compare_engines.py --dataset-dir data/datasets_v2
```

This is the strongest available statement about what the code change did,
and it is not a flattering one — see
`ENGINEERING_FAILURES_AND_FIXES.md` §15.

### What "run once" actually meant here

The V2 holdout was measured three times, and the distinction that matters
is *what the results were allowed to influence*:

1. old engine, heuristic backend (`compare_engines.py`)
2. new engine, heuristic backend (`compare_engines.py`)
3. new engine, Gemini backend — the headline V2 result

All three are measurements of already-fixed code. None of them fed back
into the implementation. That is the actual rule; "run the evaluation
exactly once" is a proxy for it, and the proxy is worth stating precisely
rather than pretending a single invocation happened.

The clearest test of whether the rule held: §15 identifies a specific
change that would likely recover most of the lost exception recall, and
it has deliberately not been made, because it was suggested by a held-out
result.

---

## Metric definitions

- **Reconciliation accuracy** — predicted outcome equals ground truth, over all records.
- **Exception precision / recall** — standard, treating EXCEPTION as the positive class.
- **False auto-reconciliation rate** — of records predicted RECONCILED, the fraction whose truth was not RECONCILED. The safety number: when the system approved something on its own, how often was that wrong.
- **False exception rate** — of truly-RECONCILED records, the fraction wrongly flagged EXCEPTION. Needless manual work, not a safety failure.
- **AI invocation rate** — fraction of records where the semantic verifier was actually called.
- **p50 / p95 latency** — per-record wall clock inside `policy.reconcile`, measured, not estimated.

On the benchmark specifically:

- **True-match recall** — of examples with a genuine counterpart, the fraction where the engine picked exactly that record.
- **Correct rejection rate** — of examples with no counterpart, the fraction where it correctly picked none.
- **Wrong-match rate** — picked a record, but the wrong one. The worst outcome, and the one an accuracy figure alone hides.

---

## Throughput evaluation

`stress_test.py` measures wall-clock behaviour as batch size grows:
1k, 5k, 10k, 50k. It runs on the deterministic backend by default,
because a hosted model call is network-bound at roughly a second and
including it would measure Google's latency rather than this system's.

**This is not an accuracy measurement and must not be quoted as one.**
Processing 50,000 synthetic records demonstrates that the pipeline scales
to that volume. It says nothing about 50,000 real reconciliations.

Read it step by step rather than first-to-last: the smallest batch runs
before the window-scan bound binds, so a 1k→50k ratio overstates the
growth. Flat throughput as the population grows is the real evidence.

---

## Limitations

- **No real merchant data has ever been processed.** Every accuracy number is against synthetic records. See `RAZORPAY_INTEGRATION.md`.
- **The fee model is simplified** — flat 2% plus 18% GST on the fee. Real MDR varies by method, category, and negotiated rate. The arithmetic *consistency* check generalises; the rate assumption in the generator does not.
- **The description vocabulary is small** (12 products), deliberately, so coincidental overlap is a real phenomenon to test against rather than something unrealistic diversity hides.
- **Policy thresholds are defaults, not calibrated** against any merchant's risk tolerance. They are configurable and every decision records which threshold applied.
- **The Gemini run is not byte-reproducible**, so V2's model-backed figures will move slightly on a re-run.
- **The benchmark is adversarial by construction.** Its variation mix is not a claim about real-world frequency — `reference_core_collision` is 20% of its non-matches and would be far rarer in practice.
- **One settlement can still be claimed by several merchant records.** Detected, not resolved. See `ENGINEERING_FAILURES_AND_FIXES.md`.
