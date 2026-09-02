# Interlock

**The mandate was signed. The transaction was still a lie.**

Interlock is a payment-execution-boundary integrity verifier for agentic payments. It sits
between an AI agent (or any AP2-style mandate-carrying flow) and the actual
Razorpay payment call, and independently re-checks — from structured evidence
only, never an agent's private reasoning — three specific things that Google's
Agent Payments Protocol (AP2) names as unresolved in its own security
analysis:

| Threat | What AP2 says today | What Interlock enforces |
|---|---|---|
| **T-31 — mandate replay** | A MUST-level rule asking the *agent itself* to avoid reusing a consumed mandate. No server-side enforcement specified. | An atomic, database-enforced consumption ledger. A replayed commitment is rejected regardless of agent behavior. |
| **T-33 — shared-MCP races** | Named as an open, unresolved concurrency problem: two sessions racing against one shared budget. | An atomic compare-and-swap reservation (SQLite `BEGIN IMMEDIATE`) — exactly one concurrent request can ever win a shared, single-use budget. |
| **T-32 / F1 — state mutation / semantic manipulation** | `checkout_hash` proves a payment corresponds to *a specific signed artifact*. It does not prove that artifact's content is still true, in-budget, or fresh relative to the merchant's live state. | Independent re-fetch of merchant ground truth + a deterministic diff against both the commitment and the declared constraints, with a narrow, evaluated semantic classifier for the one genuinely ambiguous case (fuzzy product-name equivalence). |

This is not a claim to have invented mandates, cross-protocol commerce
standards, or agent authorization — those are AP2's, Google/Shopify/Stripe's
UCP's, and Permit.io/Auth0/Okta's jobs respectively, and are treated as
solved. See [`docs/DECISION_REPORT.md`](docs/DECISION_REPORT.md) for the full
research trail and exactly what was ruled out and why.

## Why this, why now

AP2's own reference specification and security considerations page state
plainly: *"AP2 assumes that preventing prompt injection attacks is
infeasible... all LLMs and Agents MUST be considered potential attackers."*
Its mitigation is bounding worst-case impact via signed constraints — not
verifying that a transaction's actual content is still true. An independent
security analysis of AP2 (arXiv:2608.23858) catalogs this gap formally as
threats T-31 (replay), T-32 (state mutation), T-33 (shared-MCP races), and
category F1 (semantic manipulation). Nobody has shipped the fix — checked
against Lasso Security, Invariant Labs, Permit.io, Arcade.dev, Descope,
Auth0, Okta, WSO2, Cloudflare AI Gateway, Docker MCP Toolkit, Mastercard
Verifiable Intent, and Visa's Trusted Agent Protocol, none of which
independently re-verify a mandate's *content* against live merchant ground
truth — this system is a working implementation of that fix.

## Architecture

Three processes, deliberately:

```
catalog_service/   Mock merchant catalog (FastAPI, port 8100)
                    The one allowed source of "ground truth." Runs as a
                    separate process on purpose — "independently fetched"
                    has to be literally true, not an in-process shortcut.

backend/            Interlock core (FastAPI, port 8000)
  domain/           Pydantic models: Intent, Evidence, Commitment,
                     PaymentRequest, IntegrityCheck, Decision, AuditEvent
  engine/
    checks.py        The deterministic core — replay, budget reservation,
                     merchant/product/quantity/price diffs, constraint
                     conformance, staleness, ground-truth reconciliation
    semantic.py      The ONE place an LLM is used: fuzzy product-name
                     equivalence, narrow and bounded (see below)
    text_normalize.py Shared unit/phrasing normalization (handles
                     "500g" vs "500 grams" deterministically, before
                     anything is escalated to the semantic layer)
    decision.py      Aggregates checks -> ALLOW / BLOCK / REQUIRE_RECONFIRMATION
  ledger/
    db.py            SQLite — the one storage dependency, chosen because
                     the actual requirement is "atomic CAS under
                     concurrency," which SQLite's BEGIN IMMEDIATE gives
                     for free, with none of a Redis/Kafka/microservice
                     footprint
    store.py         Budget reservation (T-33), replay ledger (T-31)
    audit.py         Receiver-attested, hash-chained audit log
  integrations/
    catalog_client.py  Real HTTP calls to catalog_service
    razorpay_client.py  Real Razorpay test-mode Payment Link creation —
                        refuses to fake success if keys aren't configured
  scenarios/         Reproducible scenario suite + semantic eval harness
  tests/             Unit tests, incl. real-concurrency proofs for T-31/T-33

frontend/            React + Vite. Overview / Scenarios / Audit Trail /
                     Architecture — a product shell, not a raw dashboard,
                     driving the real backend end to end.
```

### The two comparison axes (and why they're kept separate)

Every check compares evidence across one of two boundaries:

1. **PaymentRequest vs. Commitment** — did the *agent's final ask* drift from
   what it already committed to? Catches an agent quietly changing quantity,
   price, product, or merchant at the last step (e.g. via a manipulated tool
   response).
2. **Commitment vs. freshly-fetched catalog ground truth** — did the *world*
   change since the commitment was made? Catches merchant-side price changes,
   discontinued products, and staleness.

A system that only checked one of these would miss real failure modes: an
agent could stay perfectly consistent with a commitment that was already
wrong, or the world could change in a way the agent legitimately doesn't know
about yet.

## The one place AI is used, and why

Every check above is deterministic — exact ID matches, numeric comparisons
against an explicit tolerance policy, atomic ledger lookups. None of that
needs a model.

What deterministic matching cannot resolve is fuzzy product identity:
*"Amul Butter 500g"* vs *"Amul Butter 500 grams"* is the same product (this
particular pair is actually resolved by deterministic unit-normalization in
`text_normalize.py`, and never reaches the model at all); *"Wireless Mouse"*
vs *"Wireless Mouse Premium Gaming Bundle"* is not, even though it shares
every word; *"Salted Potato Chips 150g"* vs *"Classic Salted Chips 150
grams"* is genuinely ambiguous. `engine/semantic.py` is called only on cases
that survive deterministic normalization still looking different. It:

- Never sees the agent's private reasoning — only two structured
  `{name, category}` records (declared vs. independently-fetched-observed).
- Never executes a money action — it returns a verdict + bounded confidence;
  `decision.py` decides what that becomes, conservatively.
- Runs on Gemini (`GEMINI_API_KEY`, via Google's official `google-genai` SDK
  with a Pydantic `response_schema` — the model's output is structurally
  validated by the SDK, not regex-parsed hopefully-JSON) if configured;
  falls back to Claude (`ANTHROPIC_API_KEY`) if that's set instead; falls
  back further to a deterministic token/containment-based heuristic if
  neither is configured — so the system is fully runnable offline. The
  heuristic fallback is never trusted to auto-confirm equivalence outright
  (only a real LLM backend can produce a PASS from a case ambiguous enough
  to reach this module); see `checks.py` for exactly where that's enforced.
  A transient provider error (e.g. a rate limit) degrades to the heuristic
  for that one decision — clearly labeled `heuristic-fallback-after-error`
  in the check detail — rather than crashing the integrity check.
- Is evaluated honestly on a held-out labeled set —
  `backend/scenarios/semantic_eval.py` — reporting the metric that actually
  matters for a payments system: the rate of a truly *different* product
  being wrongly called equivalent (a false ALLOW), separately from the safe
  failure mode (an unnecessary reconfirmation).

## Honest boundary — what this does NOT do

- Does not read private model chain-of-thought, and does not claim to know
  "true user intent" beyond what's captured in structured, declared
  constraints and evidence.
- Does not detect or prevent prompt injection itself — only its *effects* on
  a transaction's content, at the payment execution boundary.
- Does not replace AP2, Razorpay, or NPCI, and does not reimplement mandate
  signing, cross-protocol commerce standards, or generic agent
  authorization — see the Decision Report for what's explicitly out of
  scope and why.
- Does not replace fraud/risk scoring or dispute-response workflows
  (Razorpay's own Agent Studio ships those).
- The merchant catalog is a realistic stand-in, not a live integration with
  a real merchant (Zomato/Swiggy-class catalog access isn't available in
  this setting) — stated plainly rather than faked. The Razorpay Payment
  Link creation on ALLOW is the one unambiguously real external
  integration; without test-mode credentials, execution is clearly labeled
  `simulated` in both the API response and the UI, never presented as a
  real payment.

## Running it

Requires Python 3.11+ and Node 18+.

```bash
# 1. Backend deps
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt

# 2. Catalog service (separate terminal)
cd catalog_service && uvicorn main:app --port 8100

# 3. Backend (separate terminal)
cd backend && cp .env.example .env   # fill in keys if you have them — optional
uvicorn app.main:app --port 8000 --reload

# 4. Frontend (separate terminal)
cd frontend && npm install && npm run dev
# open http://localhost:5173
```

Optional environment variables (`backend/.env`, see `.env.example`):

- `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` — free test-mode keys, no KYC
  required (Razorpay dashboard → Settings → API Keys, test mode). Without
  these, `/execute` still completes the transaction (the commitment is
  genuinely consumed, so replay detection still works) but returns
  `{"status": "simulated"}` instead of a real Razorpay response.
- `GEMINI_API_KEY` (+ optional `GEMINI_MODEL`, default
  `gemini-3.5-flash-lite`) — primary semantic-classifier provider.
  `ANTHROPIC_API_KEY` is supported as an alternate if this isn't set.
  Without either, the semantic layer runs on the deterministic heuristic
  fallback described above.

### Reproducing the evaluation

```bash
source .venv/bin/activate
python backend/scenarios/run_scenarios.py     # end-to-end scenario suite
python backend/scenarios/semantic_eval.py     # held-out semantic classifier eval
python -m pytest backend/tests/ -v            # unit + real-concurrency proofs
```

Latest local run: **10/10 scenarios** behaved as expected (including a full
ALLOW → real Razorpay test-mode Payment Link → replay-rejected cycle),
**9/9 unit tests** pass, including two tests that hammer the
budget-reservation and commitment-consumption primitives with 20 real
concurrent OS threads each (not simulated) and assert exactly one winner.

The semantic eval was run against both configured backends on the same
20-pair held-out set — the honest comparison, not a single cherry-picked
number:

| Backend | Dangerous false-ALLOW rate | Safe false-BLOCK rate | Punted to reconfirmation |
|---|---|---|---|
| Heuristic fallback (no API key) | 0.0% (0/8) | 8.3% (1/12) | 40.0% |
| **Gemini** (`gemini-3.5-flash-lite`, real API) | **0.0% (0/8)** | **0.0% (0/12)** | **0.0%** |

Gemini resolved every pair correctly on this set, including the genuinely
ambiguous ones (a rebrand-wording chips pair, a color-variant mouse, a
size-shrink pair it correctly flagged as a material change). See
`backend/scenarios/results/` and script output for the full per-pair
breakdown; re-run to regenerate against your own key/model.

## Demo scenarios

The frontend's Scenarios tab drives these live against the real backend —
three hero scenarios plus five more, no JSON hand-editing required:

**Hero:**
1. **Valid Transaction** — legitimate purchase, ALLOW, handed to Razorpay.
2. **Transaction Mutation** — committed qty=1 → payment request claims
   qty=3 → BLOCK, shown as a before/after comparison.
3. **Shared Budget Race** — 8 concurrent commit attempts against one
   single-use budget, real concurrency, exactly 1 wins, live.

**More scenarios:** merchant substitution, product substitution (deterministic
bundle-upsell detection), semantic ambiguity (escalates to Gemini), merchant-side
price drift (graduated tolerance/hard-ceiling policy), and replay attempt
(settle a transaction, then present the same commitment again — rejected
regardless of whether execution was real or simulated).

Every run's decisions and every check's evidence are written to the
hash-chained audit ledger, inspectable in the Audit Trail tab with a
one-click chain-integrity self-test.

## Sources

- AP2 specification & security considerations — https://ap2-protocol.org/ap2/specification/, https://ap2-protocol.org/ap2/security_and_privacy_considerations/
- "Beyond the Mandate: A Systematic Security Analysis of AP2" — https://arxiv.org/html/2608.23858v1 (source of the T-31/T-32/T-33/F1 threat references used throughout)
- Google Universal Commerce Protocol — https://ucp.dev/, https://developers.googleblog.com/under-the-hood-universal-commerce-protocol-ucp/
- Stripe/OpenAI Agentic Commerce Protocol — https://docs.stripe.com/agentic-commerce/acp
- Razorpay MCP Server (real integration surface) — https://github.com/razorpay/razorpay-mcp-server
- Google Gen AI SDK (structured output) — https://googleapis.github.io/python-genai/
- Full research trail and elimination log — [`docs/DECISION_REPORT.md`](docs/DECISION_REPORT.md)
