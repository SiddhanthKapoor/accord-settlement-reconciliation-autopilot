# Decision Report

This document records the research and elimination process behind
Interlock, so the final scope reads as a decision, not an assumption.

## A. What is already solved (and therefore not rebuilt)

- **Cryptographic mandate signing, open/closed binding, checkout↔payment
  binding via `checkout_hash`, and Checkout/Payment Receipts** — all
  specified and reference-implemented in Google's AP2, now moving toward
  FIDO Alliance standardization. *Trusted Surface* is genuine AP2
  terminology (one of its five formal roles), not an invented term.
- **Full-lifecycle commerce protocol** (catalog → cart → checkout → order) —
  Google's Universal Commerce Protocol (UCP), Jan 2026, backed by Shopify,
  Etsy, Walmart, Target, Wayfair, Stripe, Adyen, Mastercard, Visa.
- **Merchant validates its own cart/stock/price at session creation** —
  Stripe/OpenAI's Agentic Commerce Protocol (ACP).
- **Agent authorization / permission scoping** (RBAC/ABAC/OAuth-scoped tool
  calls) — Permit.io, Arcade.dev, Descope, Auth0, Okta, WSO2.
- **Prompt-injection / malicious-tool-call / secret-exfiltration blocking** —
  Lasso Security, Invariant Labs (MCP-scan, Guardrails), Cloudflare AI
  Gateway, Docker MCP Toolkit.
- **Tamper-evident hash-chained logging as a pattern** — Certificate
  Transparency-style Merkle logs, an IETF draft ("Agent Audit Trail"),
  academic "Notarized Agents" receiver-attested receipts, open-source
  OpenFang. Used here as plumbing, never marketed as the innovation.
- **Razorpay's own dispute/recovery agents** — Agent Studio (Dispute
  Auto-Responder, Subscription Recovery, Abandoned Cart Recovery, RTO
  Shield, Cashflow Forecasting), launched March 2026.
- **Idempotency for Payouts (mandatory `X-Payout-Idempotency` header since
  March 2025), receipt-based dedup for Orders** — already in Razorpay's
  core API, though inconsistently (standard Payments capture has none).

**DO NOT BUILD, confirmed redundant:** a new mandate-signing scheme; a new
cross-protocol commerce standard; a generic MCP/tool-call security scanner;
a generic agent-authorization/OAuth framework; a chargeback/dispute-evidence
responder; abandoned-cart/subscription-recovery agents; a hash chain
presented as the headline feature.

## B. What is partially solved

- Lasso Security's "Intent Deputy" checks whether a tool call is consistent
  with the agent's *own* prior stated objective — self-referential drift
  detection, not cross-checked against an independent external ground
  truth (the merchant's actual current catalog).
- ACP validates cart at session creation, but nothing re-validates that the
  submitted session still matches what the human approved, and nothing
  catches drift introduced after session creation but before payment.
- Mastercard's Verifiable Intent creates a tamper-resistant authorization
  *record* — accountability after the fact, not a pre-execution content
  check.

## C. What is still genuinely unsolved

An independent, deterministic, pre-execution re-verification of a mandate's
*content* against externally-fetched merchant ground truth — confirmed
absent across every adjacent product checked (Lasso, Invariant Labs,
Permit.io, Arcade.dev, Descope, Auth0, Okta, WSO2, Cloudflare, Docker,
Mastercard, Visa, AP2, ACP, UCP, RAILS, Product.ai). This is now explicitly
named — not undiscovered — in 2025–2026 literature analyzing AP2
specifically:

- **T-31 (mandate replay)** — AP2 mitigates only via a MUST-level behavioral
  rule asking the non-deterministic agent to self-police; no server-side
  enforcement specified.
- **T-33 (shared-MCP races)** — named explicitly as an unresolved
  concurrency problem distinct from simple artifact replay: two sessions
  racing against one shared open-mandate budget.
- **T-32 (state mutation) / F1 (semantic manipulation)** — a closed
  mandate's content can differ from the merchant's live catalog truth, or
  from the open mandate's declared constraints, while remaining a validly
  signed, correctly hash-bound artifact.

Source: "Beyond the Mandate: A Systematic Security Analysis of AP2"
(arXiv:2608.23858), independently corroborated by AP2's own security
considerations page, which states plainly that prompt injection is
"infeasible to prevent" and that its only mitigation is bounding worst-case
impact via constraints — not verifying content truth.

## D. Which assumptions in the original direction were wrong

- Treating "Agentic Commerce Trust Layer" as a broad grab-bag (mandate
  issuance, authorization scope, agent identity, cross-protocol
  interoperability, revocation, dispute evidence, hash-chained audit) meant
  ~8 of 12 candidate responsibilities were already owned by well-funded
  incumbents. Building all of it would dilute the one genuinely open piece.
- Treating mandate issuance/signing as a place to add value — wrong, it's
  the most standardized layer in this space (AP2 alone has 60+ launch
  partners and is heading into FIDO standardization).
- Treating cross-protocol interoperability as a headline feature — wrong,
  Google purpose-built UCP for exactly this with massive industry backing.
- Treating a hash-chained audit trail as a novel differentiator — wrong,
  it's close to a de facto standard (an IETF draft already exists).
- "Trusted Surface" was initially suspected to be an invented/misremembered
  term — verified against AP2's formal specification and an independent
  security analysis; it is genuine AP2 terminology and is used correctly
  throughout this project.

## E. What this product should be (and is)

A payment-execution-boundary enforcement point — Interlock — that independently
re-checks exactly the three AP2-named-and-unresolved things (replay,
shared-budget concurrency, content/constraint drift against live ground
truth) before a payment executes, using only structured evidence a real
implementation could access.

## F. Final architecture

See the top-level [`README.md`](../README.md) for the full component
breakdown. Three processes: a mock merchant catalog service (the one
allowed source of ground truth, run as a genuinely separate process), the
Interlock core (domain model → deterministic engine → narrow semantic
classifier → decision engine → hash-chained audit ledger → Razorpay
test-mode client), and a React frontend.

## G. Demo scenarios (see README for the full list)

Happy path · quantity drift · price drift (merchant-side, with a graduated
tolerance/hard-ceiling policy so "not every price change is malicious") ·
merchant substitution · product substitution (deterministic) · ambiguous
product-name change (semantic classifier) · shared-budget race (T-33, real
concurrent threads).

## H. Evaluation plan

- Deterministic checks: correctness-tested with unit tests, including two
  tests that hammer the budget-reservation and commitment-consumption
  primitives with 20 real concurrent OS threads and assert exactly one
  winner (`backend/tests/test_concurrency_and_replay.py`).
- Reproducible end-to-end scenario suite against the live services
  (`backend/scenarios/run_scenarios.py`).
- The one probabilistic component — the semantic classifier — is evaluated
  on a held-out labeled set (`backend/scenarios/semantic_eval.py`),
  reporting the dangerous-false-ALLOW rate separately from the safe
  false-block rate and the conservative punt rate, rather than a single
  accuracy number.

## I. Risks (and how they're handled)

- **"Isn't constraint enforcement already AP2's job?"** — Answered
  precisely by citing T-31/T-32/T-33 rather than hand-waving: AP2 defines
  constraints structurally and expects agents to honor them; it does not
  mandate independent, server-side re-verification against live ground
  truth, per its own security page and the independent security analysis.
- **Mock merchant catalog** — Framed honestly in the README as a stand-in
  (no live Zomato/Swiggy-class access available), with the Razorpay
  test-mode Payment Link creation being the one unambiguously real external
  integration.
- **The LLM classifier looking decorative** — Addressed with a genuine
  held-out evaluation reporting the metric that actually matters for a
  payments system (dangerous false-ALLOW rate), and by architecturally
  keeping it advisory-only, bounded, and never money-executing.
- **Scope creep** — Resisted deliberately: no MCP-server facade, no ONDC/
  Beckn integration, no bot-vs-human traffic classifier were added, even
  though each was considered, because none of them strengthen the one
  core insight.
