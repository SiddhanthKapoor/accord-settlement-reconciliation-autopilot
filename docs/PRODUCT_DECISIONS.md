# Product decisions

Why the system is shaped this way. Each of these was a choice with a
real alternative, and the alternative is stated rather than implied.

---

## Deterministic logic holds final authority

The model never decides an outcome. It answers one bounded question —
"do these two records describe the same payment?" — and returns a verdict
with a confidence. `policy.py` decides what that is worth, and a match
below `ai_confidence_threshold` cannot become RECONCILED regardless of
how clean everything else looks.

**The alternative** would be letting the model weigh all the evidence and
produce the decision. It would score better on the easy cases and would
be untenable in a finance context: you cannot audit it, you cannot bound
its failure modes, and you cannot explain a specific rupee figure to
someone who is entitled to an explanation.

The practical test of whether the boundary is real: with
`enable_semantic_matching=False` the system still runs, still reconciles
the large majority of records, and loses recall rather than safety. A
component you can remove without the product becoming unsafe is a
component that was never holding the safety property.

---

## The model is used only for residual semantic ambiguity

Matching runs in tiers: exact reference, then deterministic corroboration
on independent signals, then the model. By the time a pair reaches the
model, the cheap and certain routes have been exhausted.

This is not merely an efficiency argument, though it is that too — model
invocation on the main dataset now sits near zero, and each call costs
about a second against sub-millisecond deterministic matching. It is
mainly a scope argument: the narrower the model's question, the more
meaningful its confidence, and the smaller the surface where a wrong
answer can do damage.

**This produced an uncomfortable finding, and then a corrected one.**
Once identity evidence was used properly, the deterministic tiers
resolved the *old* dataset's ambiguous cases on their own and AI
invocation fell to 0%. On that data the model was genuinely unnecessary —
which was a fact about the dataset, not about reconciliation. The
generator had embedded the order number in both the reference and the
description, so an identifier always survived.

The current dataset withholds identity evidence for 16% of records the
way real data does: truncated bank narration, a reference in a different
numbering system, a trading name instead of a legal entity. AI invocation
is around 20%, and the two categories that need it score zero
deterministically. The lesson kept from the earlier finding is that "the
model turned out to be unnecessary on our own test data" is the kind of
thing a panel should hear from us rather than discover — so it is
recorded here rather than quietly overwritten.

---

## Amount agreement is retrieval, not evidence

The single most consequential decision in the system.

In a population of a few thousand settlements, two unrelated payments
sharing an exact amount is ordinary. The indexes are tuned for recall and
return those collisions by design. Treating a retrieved record as a
candidate — which the system did until this phase — meant a genuinely
missing settlement would surface a coincidence, escalate it, and land in
HUMAN_REVIEW instead of being reported as missing. That was the entire
Evaluation V2 regression.

Retrieval and admissibility are now separate questions. A candidate must
carry identity evidence — a shared reference core, or wording that
corroborates — before it counts as a candidate at all.

**Measured on 312 development scenarios:** every coincidental top-ranked
candidate had zero shared reference identifiers; every genuine one had a
shared identifier. That is what the rule is built on, not intuition.

---

## Contradicting references are treated as negative evidence

When both sides carry a recognisable identifier and the identifiers
disagree, the system treats that as a statement that the records are
different — not as a failure to prove they are the same.

**This is an assumption, and it is load-bearing.** It holds because
Razorpay's `order_reference` is the merchant's own reference as recorded
by Razorpay. A provider whose reference is an opaque internal id would
break it, which is why `treat_reference_contradiction_as_negative` exists
as a switch rather than being hard-coded.

The failure mode when the assumption is wrong is a false negative — the
record is reported as a missing settlement, with the rejected candidate
listed and the reason given. That is the safe direction, and it is
visible to the reviewer rather than silent.

---

## Uncertainty routes to a human, and the queue is real work

Three outcomes, no fourth "probably fine" state. Anything the system
cannot settle goes to a person with the evidence attached.

The review queue is a view over the pipeline's own decisions rather than
a separate workflow store, so it cannot drift from what the engine
actually decided. Items are ordered by severity then amount, because an
operator with an hour should spend it on the largest thing that is
definitely wrong. Only actions that make sense for a record's state are
offered — no inviting someone to approve a match against a record with no
candidate.

Every human action writes to the same hash-chained ledger as the
automated ones, carrying prior state, new state, and the reason the
automation escalated. A reviewer overriding the system is itself evidence.

---

## A settlement that is not due yet is not missing

A payment captured this morning has no settlement because none is owed.
Reporting that as a missing settlement sends someone chasing a provider
for money that was never late — a false positive with a phone call
attached.

This requires an observation point. When none is supplied the check is
skipped rather than guessed: inventing a "now" would manufacture a
finding.

---

## One settlement, one merchant record

Two orders can each match the same payment and each look perfectly
reconciled, because every decision is made for one record in isolation.
That is double-counted revenue, and no per-record check can see it.

Conflicts are settled on evidence tier — an exact reference match
outranks a semantic verdict — and when the top tier ties, every claimant
is demoted to review rather than decided by position in the batch.

**The alternative was global assignment** (bipartite matching over the
whole batch). Rejected deliberately: it makes one record's outcome depend
on every other record, which cannot be explained to the operator who has
to act on it. "Your order was not reconciled because a different order
won an assignment contest" is not an answer anyone can use.

Note the invariant is on `payment_id`. Many payments legitimately settle
in one batch and share a `settlement_id`; that is aggregation, not a
conflict, and it is tested as such.

---

## Explanations are built from recorded signals, never narrated by the model

`explain.py` composes every operator-facing string from evidence the
engine already produced. The model's own rationale is stored on the
candidate assessment for inspection and never reaches the explanation.

An explanation that sounds plausible but is not the actual reason is
worse than no explanation. It survives review right up until someone
audits it, and then it destroys trust in every other explanation the
system has produced.

---

## What the Razorpay integration actually proves

It proves the client works: `order.create` writes a real object and
`order.fetch` reads it back. It does not prove the engine reconciles real
Razorpay data, because no settlement data can be obtained —
`settlement.all`, `settlement.report`, `payment.all`, `order.all` and
`refund.all` all return zero items on a test account, verified rather
than assumed.

A settlement exists only after a payment is captured through browser
checkout *and* a bank settlement cycle runs. Neither is reachable from a
server-side test-mode call, so there is no supported sandbox workflow
that produces representative settlement, fee or adjustment records.

`settlement_source.py` keeps the two apart behind one interface, each
source reporting its own provenance, and the console renders it as a
banner. The full probe is in `RAZORPAY_INTEGRATION.md`.

---

## What synthetic data is for

Measuring the architecture against a controlled distribution of known
failure modes, with ground truth assigned from each category's definition
rather than from what the engine happened to output.

It is not evidence about real-world accuracy. Real settlement text,
reference conventions and fee structures will differ, and the generator's
ambiguous cases are easier than real ones — it embeds the order number in
both descriptions, so the identifier is usually recoverable.

---

## Settlement shapes: what is handled, and what is deliberately not

| Shape | Status |
|---|---|
| Fees and tax deducted from gross | Handled. `net = gross − fee − tax − refund` is checked as arithmetic, not assumed. |
| Partial refunds | Handled. Both sides must agree on the refund total. |
| Multiple refunds against one payment | Handled at the total, not per event — the data model carries a refund total, not a refund ledger. |
| Aggregated settlement (many payments, one `settlement_id`) | Handled. Matching is on `payment_id`, so a shared `settlement_id` is not a conflict, and this is tested. |
| One payment split across several settlements | **Not representable.** The data model is one settlement record per payment. |

The last row is the honest one. Supporting split settlement would mean
changing the record model from one-to-one to one-to-many and reworking
every check that assumes a single counterpart. Rather than fake it, the
model does not claim it. If such data appeared, the amounts would not
reconcile and the record would surface as an amount mismatch for a human
— wrong in its label, but safe in its direction and visible.

---

## The product is not a Razorpay wrapper

The engine was never gateway-specific: it compares two sides on amount,
reference, date and text, and none of that cares where the rows came
from. Only the *loading* was Razorpay-shaped.

`app/ingest/` is the seam. Any CSV maps into one of two canonical roles —
**ledger** (what the business believes happened) and **settlement** (what
happened to the money) — and the engine is unchanged. A bank statement
reconciles against an accounting export using the same code that
reconciles a gateway payout against an order book.

`MerchantRecord` and `RazorpaySettlementRecord` keep their names
deliberately. Renaming them would churn the engine, the tests and three
frozen evaluations for no behavioural gain. Read them as roles, not
vendors.

**The alternative** was a generic `Record` type with a bag of optional
fields. Rejected: the two sides genuinely have different shapes — only
one of them has a fee, a tax and a settlement date — and collapsing that
into one nullable structure would have pushed the distinction into
runtime checks scattered through the engine.

---

## Schema detection asks rather than guesses

Detection reports a confidence and a reason per column, and a run will
not start while a required column is unresolved.

This is the one place where being unhelpful is correct. A reconciliation
tool that quietly mis-reads an amount column produces confident, wrong
financial output, and the error is invisible precisely because the
output looks normal. Asking the user to confirm a column is a small cost;
mis-reading one is not recoverable by anything downstream.

Two consequences worth stating:

- **Assignment is global, not first-come.** A header often fits several
  slots, and which reading is right depends on what else the file
  contains — `order_id` is the cross-system reference in a gateway export
  and the file's own key in an order book. Assigning by column order got
  this backwards and silently reconciled an order book against nothing.
- **A stated net amount is kept, never recomputed.** Deriving
  `net = gross − fee − tax` would make the arithmetic check verify a
  subtraction the mapper had just performed, so a payout file that
  genuinely does not add up could never fail it.

---

## Identifier namespaces, not just identifiers

Reference *contradiction* — both sides naming a transaction, naming
different ones — is strong negative evidence, and it is what stops
amount-only coincidences being matched.

But a bank UTR and an invoice number are both digit runs that will never
agree, and their disagreement means nothing: they are different numbering
systems. Treating that as contradiction rejected every genuine
bank-statement match on principle.

Contradiction now requires *comparable* identifiers, using width as the
proxy — a counter issued by one system produces identifiers of consistent
length. When two identifier sets are incomparable, the pair carries no
identifier evidence either way, and amount and date can admit it for the
model to judge.

That single change is what gives the semantic tier a real job on bank
data, and it is the difference between a product that reconciles gateway
exports and one that reconciles bank statements.

---

## Aggregated settlements are proposed, never booked

A bank credits one amount for several gateway payments, so an unmatched
settlement can be the sum of unmatched orders.

Deciding *which* orders make up a lump sum is subset-sum. With enough
unmatched records several decompositions usually add up, and picking one
would be a guess with money attached. So a grouping is reported only when
it is the **unique** combination summing to the settlement within
tolerance, and even then its members go to HUMAN_REVIEW with the proposed
grouping attached rather than being matched.

The search is bounded — groups of at most three, from records inside the
window, with a hard cap on candidates. An unbounded search is
exponential, and a run that hangs is worse than one that misses an
aggregation.

**Still not supported:** one payment split across several settlements.
That would require changing the record model from one-to-one to
one-to-many and reworking every check that assumes a single counterpart.
It is not faked; such data surfaces as an amount mismatch for a human.

---

## Thresholds are defaults, not calibration

Every threshold lives in `PolicyConfig`, every decision records which one
applied, and none of them is calibrated against a real merchant's risk
tolerance. They are reasonable starting points chosen against development
data, and they are configuration rather than code so that a merchant can
move them without touching the engine.
