import {
  createCommitment,
  createIntent,
  catalogPatch,
  catalogReset,
  executePayment,
  newRequestId,
  recordEvidence,
  verifyPayment,
} from "./api.js";

// Every scenario drives the real backend exactly the way run_scenarios.py
// does (that script remains the source of truth for reproducible eval
// numbers) — nothing here is hardcoded or animated independently of what
// the API actually returns. If the backend returns BLOCK, this shows
// BLOCK; if it errors, this surfaces the real error.

async function setup({ maxAmount, merchantId, productId, quantity = 1, tolerance = 0, categories = null }, log) {
  log(`declaring intent: max ₹${(maxAmount / 100).toFixed(0)}, tolerance ${tolerance}%`);
  const intent = await createIntent({
    constraints: {
      max_amount_minor: maxAmount,
      max_quantity: quantity,
      price_tolerance_pct: tolerance,
      allowed_categories: categories,
    },
  });
  log(`agent discovers product ${productId} at ${merchantId} (independently fetched from merchant catalog)`);
  const evidence = await recordEvidence(intent.intent_id, { merchant_id: merchantId, product_id: productId, stage: "SELECTED" });
  log(`agent commits to cart: ${evidence.product.name} x${quantity} @ ₹${(evidence.product.price_minor / 100).toFixed(2)}`);
  const commit = await createCommitment(intent.intent_id, { evidence_id: evidence.evidence_id, quantity });
  // Reported via `data` (not just text) so the caller can subscribe the
  // live audit stream to exactly this transaction — see ScenariosView.
  log(
    commit.budget_reserved ? "budget reserved for this commitment" : "BUDGET RESERVATION FAILED (T-33 guard)",
    false,
    { commitmentId: commit.commitment.commitment_id }
  );
  return { intent, evidence, commit };
}

async function verify(intentId, commitmentId, overrides, log) {
  log("payment request reaches Interlock — running integrity checks…");
  const result = await verifyPayment(intentId, commitmentId, {
    client_request_id: newRequestId(),
    merchant_id: "merchant_electronics_01",
    product_id: "mouse_001",
    product_name: "Wireless Mouse",
    category: "electronics",
    quantity: 1,
    price_minor: 149900,
    ...overrides,
  });
  log(`decision: ${result.decision.outcome}`);
  return result;
}

function moneyStr(minor) {
  return `₹${(minor / 100).toLocaleString("en-IN", { minimumFractionDigits: 0 })}`;
}

// ------------------------------------------------------------------ hero

export const HERO_SCENARIOS = {
  valid_transaction: {
    label: "Valid Transaction",
    description: "A legitimate agent purchase, verified and handed to Razorpay.",
    kind: "standard",
    preview: {
      intent: "Agent buys 1 Wireless Mouse for ₹1,499.",
      changes: "Nothing. The payment request matches exactly what was committed.",
      verifies: "Replay status, budget reservation, merchant/product/price/quantity match, live catalog price.",
      risk: "None expected — should allow.",
    },
    run: async (log) => {
      const { intent, commit } = await setup(
        { maxAmount: 200000, merchantId: "merchant_electronics_01", productId: "mouse_001" }, log
      );
      const result = await verify(intent.intent_id, commit.commitment.commitment_id, {}, log);
      let execution = null;
      if (result.decision.outcome === "ALLOW") {
        log("integrity verified — handing off to Razorpay test mode…");
        execution = await executePayment(intent.intent_id, commit.commitment.commitment_id, { client_request_id: newRequestId() });
        log(execution.simulated ? "execution simulated (Razorpay not configured)" : "payment link created");
      }
      return { ...result, execution };
    },
  },
  transaction_mutation: {
    label: "Transaction Mutation",
    description: "Committed quantity silently changes before payment. Caught before execution.",
    kind: "compare",
    preview: {
      intent: "Agent commits to 1 Wireless Mouse for ₹1,499.",
      changes: "The payment request silently claims 3 units for ₹4,497 instead.",
      verifies: "Whether the final payment request still matches the committed quantity and price.",
      risk: "Quantity and price drift — should block.",
    },
    run: async (log) => {
      const { intent, commit } = await setup(
        { maxAmount: 500000, merchantId: "merchant_electronics_01", productId: "mouse_001" }, log
      );
      const verified = { label: "Wireless Mouse", qty: 1, price: 149900 };
      log("!! payment request silently claims 3 units instead of 1 !!", true);
      const result = await verify(intent.intent_id, commit.commitment.commitment_id, { quantity: 3, price_minor: 149900 * 3 }, log);
      const observed = { label: "Wireless Mouse", qty: 3, price: 149900 * 3 };
      return { ...result, compare: { verified, observed, diffField: "qty" } };
    },
  },
  shared_budget_race: {
    label: "Shared Budget Race",
    description: "8 agents race one single-use delegated budget. Exactly one may win.",
    kind: "race",
    preview: {
      intent: "8 separate agent sessions each try to spend from one ₹2,000 single-use budget.",
      changes: "All 8 attempts reach Interlock at the same instant.",
      verifies: "Atomic budget reservation under real, concurrent load — not simulated timing.",
      risk: "Exactly one may acquire the budget; the rest must be rejected.",
    },
    // execution handled by runSharedBudgetRace below, not a generic run()
  },
};

// -------------------------------------------------------------- secondary

export const SECONDARY_SCENARIOS = {
  merchant_substitution: {
    label: "Merchant substitution",
    description: "Payment request targets a different merchant than the commitment.",
    preview: {
      intent: "Agent commits to buying from merchant_electronics_01.",
      changes: "The payment request targets merchant_grocery_02 instead.",
      verifies: "Merchant identity, checked against the original commitment.",
      risk: "Should block.",
    },
    run: async (log) => {
      const { intent, commit } = await setup(
        { maxAmount: 200000, merchantId: "merchant_electronics_01", productId: "mouse_001" }, log
      );
      log("!! payment request targets merchant_grocery_02 instead of merchant_electronics_01 !!", true);
      return verify(intent.intent_id, commit.commitment.commitment_id, { merchant_id: "merchant_grocery_02" }, log);
    },
  },
  product_substitution: {
    label: "Product substitution",
    description: "Wireless Mouse silently swapped for an unrelated premium bundle.",
    preview: {
      intent: "Agent commits to 1 Wireless Mouse for ₹1,499.",
      changes: "Payment request claims 'Wireless Mouse Premium Gaming Bundle' at 3x the price.",
      verifies: "Product identity — resolved deterministically, no model call needed.",
      risk: "Should block.",
    },
    run: async (log) => {
      const { intent, commit } = await setup(
        { maxAmount: 1000000, merchantId: "merchant_electronics_01", productId: "mouse_001" }, log
      );
      log("!! payment request claims 'Wireless Mouse Premium Gaming Bundle' at 3x the price !!", true);
      return verify(intent.intent_id, commit.commitment.commitment_id, {
        product_id: "mouse_bundle_premium",
        product_name: "Wireless Mouse Premium Gaming Bundle",
        price_minor: 449700,
      }, log);
    },
  },
  semantic_ambiguity: {
    label: "Semantic ambiguity",
    description: "'Salted Potato Chips 150g' vs 'Classic Salted Chips 150 grams' — needs judgment.",
    preview: {
      intent: "Agent commits to 1 bag of Salted Potato Chips 150g.",
      changes: "Payment request claims 'Classic Salted Chips 150 grams' under a different product id.",
      verifies: "Deterministic matching can't resolve this — escalates to the semantic classifier (Gemini).",
      risk: "Outcome depends on the classifier's verdict.",
    },
    run: async (log) => {
      const { intent, commit } = await setup(
        { maxAmount: 100000, merchantId: "merchant_grocery_02", productId: "chips_001" }, log
      );
      log("payment request claims 'Classic Salted Chips 150 grams' under a different product id");
      log("deterministic normalized-string match is inconclusive — escalating to semantic classifier");
      return verify(intent.intent_id, commit.commitment.commitment_id, {
        merchant_id: "merchant_grocery_02",
        product_id: "chips_001_classic_variant",
        product_name: "Classic Salted Chips 150 grams",
        category: "snacks-savory",
        price_minor: 4900,
      }, log);
    },
  },
  price_drift: {
    label: "Price drift (merchant-side)",
    description: "Merchant catalog price changes after commit, before payment.",
    preview: {
      intent: "Agent commits to 1 Wireless Mouse for ₹1,499, with a 2% price-drift tolerance.",
      changes: "The merchant's live catalog price changes to ₹2,999 before payment executes.",
      verifies: "Commitment price vs. live catalog price, against the declared tolerance.",
      risk: "Drift exceeds tolerance — should block.",
    },
    run: async (log) => {
      await catalogReset();
      const { intent, commit } = await setup(
        { maxAmount: 1000000, merchantId: "merchant_electronics_01", productId: "mouse_001", tolerance: 2 }, log
      );
      log("!! merchant's live catalog price for this product changes to ₹2,999 !!", true);
      await catalogPatch("merchant_electronics_01", "mouse_001", { price_minor: 299900 });
      const result = await verify(intent.intent_id, commit.commitment.commitment_id, {}, log);
      await catalogReset();
      return result;
    },
  },
  replay_attempt: {
    label: "Replay attempt",
    description: "The same settled commitment is presented again.",
    preview: {
      intent: "Agent completes a legitimate purchase; the commitment is executed.",
      changes: "The same commitment is presented to Interlock a second time.",
      verifies: "The replay ledger — has this commitment already backed a completed payment?",
      risk: "Should block, regardless of whether execution was real or simulated.",
    },
    run: async (log) => {
      const { intent, commit } = await setup(
        { maxAmount: 200000, merchantId: "merchant_electronics_01", productId: "mouse_001" }, log
      );
      const first = await verify(intent.intent_id, commit.commitment.commitment_id, {}, log);
      if (first.decision.outcome !== "ALLOW") return first;
      log("settling the transaction (execute)…");
      const execution = await executePayment(intent.intent_id, commit.commitment.commitment_id, { client_request_id: newRequestId() });
      log(execution.simulated ? "execution simulated — commitment is now consumed" : "payment settled — commitment is now consumed");
      log("!! the same commitment is presented again !!", true);
      const second = await verify(intent.intent_id, commit.commitment.commitment_id, {}, log);
      return second;
    },
  },
};

export async function runSharedBudgetRace(onAgentUpdate, log, n = 8) {
  log(`declaring one single-use ₹2,000 intent, then firing ${n} concurrent commit attempts against it…`);
  const intent = await createIntent({ constraints: { max_amount_minor: 200000, max_quantity: 1 } });
  const evidence = await recordEvidence(intent.intent_id, {
    merchant_id: "merchant_electronics_01", product_id: "mouse_001", stage: "SELECTED",
  });

  for (let i = 0; i < n; i++) onAgentUpdate(i, "waiting");

  const promises = Array.from({ length: n }, (_, i) =>
    createCommitment(intent.intent_id, { evidence_id: evidence.evidence_id, quantity: 1 }).then((res) => {
      onAgentUpdate(i, res.budget_reserved ? "won" : "lost");
      return res.budget_reserved;
    })
  );
  const results = await Promise.all(promises);
  const winners = results.filter(Boolean).length;
  log(`result: ${winners}/${n} attempts won the shared budget (expected exactly 1)`);
  return { intent, winners, n };
}

export { moneyStr };
