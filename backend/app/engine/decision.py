"""
Aggregates IntegrityCheck results into one Decision. The policy is
deliberately a flat, readable table rather than a scoring model — every
outcome must be traceable to a specific check by name, because "explain
why" is a submission requirement, not a nice-to-have.

Policy:
  any FAIL              -> BLOCK   (money action refused)
  no FAIL, any WARN      -> REQUIRE_RECONFIRMATION (agent/user must re-approve)
  all PASS               -> ALLOW
"""

from __future__ import annotations

from app.domain.models import CheckStatus, Decision, DecisionOutcome, IntegrityCheck


def decide(transaction_id: str, checks: list[IntegrityCheck]) -> Decision:
    fails = [c for c in checks if c.status == CheckStatus.FAIL]
    warns = [c for c in checks if c.status == CheckStatus.WARN]

    if fails:
        reason = "BLOCKED: " + "; ".join(f"{c.name} — {c.detail}" for c in fails)
        outcome = DecisionOutcome.BLOCK
    elif warns:
        reason = "REQUIRES RECONFIRMATION: " + "; ".join(f"{c.name} — {c.detail}" for c in warns)
        outcome = DecisionOutcome.REQUIRE_RECONFIRMATION
    else:
        reason = "All integrity checks passed."
        outcome = DecisionOutcome.ALLOW

    return Decision(transaction_id=transaction_id, outcome=outcome, reason=reason, checks=checks)
