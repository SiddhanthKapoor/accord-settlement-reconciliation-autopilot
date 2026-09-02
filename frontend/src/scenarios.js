import {
  createCommitment,
  createIntent,
  catalogPatch,
  catalogReset,
  newRequestId,
  recordEvidence,
  verifyPayment,
} from "./api.js";

// Mirrors backend/scenarios/run_scenarios.py, but drives the UI live for
// the demo instead of asserting pass/fail. The Python script remains the
// source of truth for the reproducible evaluation numbers in the README.

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
  log(commit.budget_reserved ? "budget reserved for this commitment" : "BUDGET RESERVATION FAILED (T-33 guard)");
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

export const SCENARIOS = {
  happy_path: {
    label: "Happy path",
    description: "Legitimate purchase, everything matches. Should ALLOW.",
    run: async (log) => {
      const { intent, commit } = await setup(
        { maxAmount: 200000, merchantId: "merchant_electronics_01", productId: "mouse_001" }, log
      );
      return verify(intent.intent_id, commit.commitment.commitment_id, {}, log);
    },
  },
  quantity_drift: {
    label: "Quantity drift",
    description: "Committed qty=1, payment request claims qty=3. Should BLOCK.",
    run: async (log) => {
      const { intent, commit } = await setup(
        { maxAmount: 500000, merchantId: "merchant_electronics_01", productId: "mouse_001" }, log
      );
      log("!! agent's final payment request silently asks for qty=3 instead of 1 !!");
      return verify(intent.intent_id, commit.commitment.commitment_id, { quantity: 3, price_minor: 149900 * 3 }, log);
    },
  },
  price_drift: {
    label: "Price drift (merchant-side)",
    description: "Merchant catalog price doubles after commit, before payment. Should BLOCK.",
    run: async (log) => {
      await catalogReset();
      const { intent, commit } = await setup(
        { maxAmount: 1000000, merchantId: "merchant_electronics_01", productId: "mouse_001", tolerance: 2 }, log
      );
      log("!! merchant's live catalog price for this product changes to ₹2,999 !!");
      await catalogPatch("merchant_electronics_01", "mouse_001", { price_minor: 299900 });
      const result = await verify(intent.intent_id, commit.commitment.commitment_id, {}, log);
      await catalogReset();
      return result;
    },
  },
  merchant_substitution: {
    label: "Merchant substitution",
    description: "Payment request targets a different merchant than the commitment. Should BLOCK.",
    run: async (log) => {
      const { intent, commit } = await setup(
        { maxAmount: 200000, merchantId: "merchant_electronics_01", productId: "mouse_001" }, log
      );
      log("!! payment request targets merchant_grocery_02 instead of merchant_electronics_01 !!");
      return verify(intent.intent_id, commit.commitment.commitment_id, { merchant_id: "merchant_grocery_02" }, log);
    },
  },
  product_substitution: {
    label: "Product substitution",
    description: "Wireless Mouse silently swapped for an unrelated premium bundle. Should BLOCK.",
    run: async (log) => {
      const { intent, commit } = await setup(
        { maxAmount: 1000000, merchantId: "merchant_electronics_01", productId: "mouse_001" }, log
      );
      log("!! payment request claims 'Wireless Mouse Premium Gaming Bundle' at 3x the price !!");
      return verify(intent.intent_id, commit.commitment.commitment_id, {
        product_id: "mouse_bundle_premium",
        product_name: "Wireless Mouse Premium Gaming Bundle",
        price_minor: 449700,
      }, log);
    },
  },
  product_equivalence: {
    label: "Ambiguous substitution (AI-assisted)",
    description: "'Salted Potato Chips 150g' vs 'Classic Salted Chips 150 grams' — genuinely ambiguous.",
    run: async (log) => {
      const { intent, commit } = await setup(
        { maxAmount: 100000, merchantId: "merchant_grocery_02", productId: "chips_001" }, log
      );
      log("!! payment request claims 'Classic Salted Chips 150 grams' under a different product id !!");
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
};

export async function runSharedBudgetRace(log, n = 8) {
  log(`declaring one single-use intent, then firing ${n} concurrent commit attempts against it…`);
  const intent = await createIntent({ constraints: { max_amount_minor: 200000, max_quantity: 1 } });
  const evidence = await recordEvidence(intent.intent_id, {
    merchant_id: "merchant_electronics_01", product_id: "mouse_001", stage: "SELECTED",
  });
  const attempts = await Promise.all(
    Array.from({ length: n }, () => createCommitment(intent.intent_id, { evidence_id: evidence.evidence_id, quantity: 1 }))
  );
  const winners = attempts.filter((a) => a.budget_reserved).length;
  log(`result: ${winners}/${n} attempts won the shared budget (expected exactly 1)`);
  return { intent, attempts, winners };
}
