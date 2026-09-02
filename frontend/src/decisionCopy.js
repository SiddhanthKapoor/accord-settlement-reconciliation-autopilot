// Turns a real Decision (from the backend) into a headline a viewer
// understands in under a second, without reading check-by-check output
// first. The technical detail (decision.reason, the full checks table)
// stays available right below — this only changes what's read FIRST.

const THREAT_HEADLINES = {
  "T-31": "REPLAY DETECTED",
  "T-32": "DRIFT DETECTED",
  "T-33": "BUDGET UNAVAILABLE",
};

// More specific than the generic per-threat label, when we can tell
// exactly which field drifted — this is what makes the Mutation scenario
// read as "QUANTITY DRIFT DETECTED" rather than a generic "DRIFT DETECTED".
const CHECK_HEADLINES = {
  quantity_vs_commitment: "QUANTITY DRIFT DETECTED",
  price_vs_commitment: "PRICE DRIFT DETECTED",
  ground_truth_price: "PRICE DRIFT DETECTED",
  merchant_identity: "MERCHANT MISMATCH DETECTED",
  product_identity: "PRODUCT SUBSTITUTION DETECTED",
  replay_check: "REPLAY DETECTED",
  budget_reservation: "BUDGET RACE LOST",
  ground_truth_availability: "PRODUCT UNAVAILABLE",
  commitment_staleness: "STALE TRANSACTION",
};

export function humanizeDecision(decision) {
  if (decision.outcome === "ALLOW") {
    return { statusLine: "ALLOWED", headline: "Transaction verified", tone: "allow" };
  }
  if (decision.outcome === "REQUIRE_RECONFIRMATION") {
    const flagged = decision.checks.find((c) => c.status === "WARN");
    return {
      statusLine: "RECONFIRMATION REQUIRED",
      headline: flagged ? CHECK_HEADLINES[flagged.name] || "REVIEW REQUIRED" : "REVIEW REQUIRED",
      tone: "warn",
    };
  }
  // BLOCK
  const failing = decision.checks.find((c) => c.status === "FAIL");
  const headline =
    (failing && CHECK_HEADLINES[failing.name]) ||
    (failing && THREAT_HEADLINES[failing.threat_ref]) ||
    "TRANSACTION BLOCKED";
  return { statusLine: "PAYMENT EXECUTION BLOCKED", headline, tone: "block" };
}
