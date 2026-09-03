# Evaluation methodology

This is the detailed reference; README.md has the headline numbers. Read
this if you want to know exactly how the dataset was built, what each
metric means, and what "held-out" actually guarantees here.

## Dataset generation

`backend/data/generate_dataset.py` is fully deterministic given `--seed`.
It builds one shared pool of Razorpay settlement records (including
deliberate orphans, near-duplicates, and text-similar decoys) and a set
of merchant records, each constructed to exercise exactly one scenario
category, with the **expected outcome assigned from the category
definition** — never by running the matching engine and recording
whatever it happened to output. That's the difference between a real
measurement and a tautology.

### Category definitions

| Category | What's constructed | Why the expected outcome follows |
|---|---|---|
| `clean_match` | Merchant amount = Razorpay gross; net = gross − fee − tax exactly; reference matches exactly | Nothing to disagree on |
| `fee_tax_rounding` | Same, with a ±1–2 paise net perturbation | Within `PolicyConfig.amount_tolerance_minor` |
| `delayed_settlement_normal` | Settlement 4–14 days after order | Within `max_settlement_delay_days` (21) |
| `delayed_settlement_excessive` | Settlement 35–90 days after order | Beyond the policy threshold — a real anomaly |
| `partial_refund` | Refund recorded consistently on both sides, net adjusted accordingly | Arithmetic still holds |
| `refund_mismatch` | Razorpay's refund amount deliberately differs from the merchant's by a material sum | A real, known discrepancy |
| `missing_settlement` | No Razorpay counterpart generated at all | Genuine absence, not ambiguity |
| `amount_mismatch` | Merchant amount perturbed by a material sum (not rounding) | A real, known discrepancy |
| `duplicate_reference` | Two Razorpay records share one reference, near-identical amounts | Amount can't disambiguate — genuine ambiguity |
| `ambiguous_text_reference_fuzzy_strong` | Reference differs, description overlap is high (≥0.6 token Jaccard) | Resolved deterministically, never reaches the model |
| `ambiguous_text_reference_semantic_true_match` | Reference differs, description overlap is moderate, genuinely the same transaction | Needs real semantic judgment to confirm |
| `ambiguous_text_reference_semantic_decoy` | A *different* nearby transaction sharing generic words (e.g. the product name) but a different order number | A real semantic read should reject it; pure lexical overlap might not — this is the case that most differentiates a real model from the heuristic fallback |

### Split

Stratified 80/20 by category (not a global random split) so both dev
and holdout carry the same category mix — the held-out set isn't
accidentally missing or over-representing any scenario type.

### Held-out discipline

The held-out set was touched exactly once for the official, reported
numbers — one run, against the system as actually configured (Gemini
backend), after every engine/policy change was made and validated
against the dev split only. Two real fixes were made during dev-set
iteration, both documented in README.md's "Process discipline" section —
a data-generation entropy fix and a matching pre-filter — neither is a
threshold tuned to make the held-out number look better; both were
justified independently of any held-out result, because neither had been
computed yet when the fixes were made.

A second run against the same held-out set followed, with
`GEMINI_API_KEY` unset to force the fixed heuristic fallback, solely to
populate the heuristic-vs-Gemini comparison table in README.md. That
backend has no tunable parameters, so this is a comparison between two
already-fixed algorithms, not a second round of tuning — no engine,
policy, or dataset code changed as a result of either run.

## Metric definitions

- **Reconciliation accuracy** — predicted outcome == ground truth outcome, over all records.
- **Exception precision / recall** — standard precision/recall treating `EXCEPTION` as the positive class.
- **False auto-reconciliation rate** — of records the system predicted RECONCILED, what fraction were NOT actually supposed to be RECONCILED. This is the single most important safety number: it answers "when the system approved something automatically, how often was that wrong?"
- **False exception rate** — of records that were truly RECONCILED, what fraction got wrongly predicted EXCEPTION (needless manual work, not a safety issue).
- **AI invocation rate** — fraction of records where the semantic classifier was actually called (should be small — most cases resolve deterministically).
- **p50 / p95 latency, throughput** — measured per-record wall-clock time in `policy.reconcile()` (via `time.perf_counter()`), aggregated across the actual evaluation run — not estimated.

## Reproducing a result

```bash
cd backend
python data/generate_dataset.py --seed 20260903 --total 5000   # idempotent given the same seed
python evaluate.py --dataset holdout
```

The JSON report (`data/eval_reports/latest_holdout.json`) carries the
dataset version (a content hash of the generation parameters), the seed,
and a timestamp — enough to confirm any reported number was actually
produced by this exact dataset and this exact code.
