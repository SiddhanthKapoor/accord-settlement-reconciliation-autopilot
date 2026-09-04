#!/usr/bin/env python
"""
Settlement-presence discrimination benchmark (development only).

    python benchmark_settlement_presence.py
    python benchmark_settlement_presence.py --backend heuristic

Scores one question: when the system does not reconcile a record, does it
correctly distinguish *no settlement exists* from *a settlement exists and
I could not find it* from *a settlement is not due yet*?

Evaluation V2 lost 19 points of exception recall on exactly this, and the
held-out records that revealed it cannot be used to design the fix. These
scenarios are the development stand-in. Nothing here is a headline metric.

Scoring maps each ground-truth class to the outcome a correct system
should produce:

    present    -> RECONCILED (and the right payment_id)
    absent     -> EXCEPTION  (a confident "no settlement", not a shrug)
    pending    -> HUMAN_REVIEW or EXCEPTION flagged as not-yet-due
    ambiguous  -> HUMAN_REVIEW
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from datetime import datetime  # noqa: E402

from app.domain.models import (  # noqa: E402
    ExceptionType, MerchantRecord, PolicyConfig, RazorpaySettlementRecord, ReconciliationOutcome,
)
from app.engine import matching  # noqa: E402
from app.engine import policy as policy_engine  # noqa: E402

SCENARIOS = Path(__file__).parent / "data" / "datasets" / "settlement_scenarios.jsonl"
POOL = Path(__file__).parent / "data" / "datasets" / "settlement_scenarios_pool.jsonl"

ACCEPTABLE = {
    "present": {ReconciliationOutcome.RECONCILED},
    "absent": {ReconciliationOutcome.EXCEPTION},
    "pending": {ReconciliationOutcome.HUMAN_REVIEW, ReconciliationOutcome.EXCEPTION},
    "ambiguous": {ReconciliationOutcome.HUMAN_REVIEW},
}

# The outcome alone is too coarse for two classes. "Pending" reaching
# EXCEPTION is only correct if it is typed as not-yet-due; reporting it as
# a missing settlement sends an operator chasing a provider for money that
# was never late. Likewise "absent" must be typed as genuinely missing
# rather than as something the system merely failed to resolve.
REQUIRED_EXCEPTION_TYPE = {
    "pending": {ExceptionType.PENDING_SETTLEMENT},
    "absent": {ExceptionType.MISSING_SETTLEMENT},
}


def load() -> list[dict]:
    if not SCENARIOS.exists():
        raise SystemExit(
            f"missing {SCENARIOS}\nGenerate it first:\n"
            "  python data/generate_settlement_scenarios.py --seed 4127 --count 312"
        )
    return [json.loads(line) for line in SCENARIOS.open()]


def load_pool() -> list[RazorpaySettlementRecord]:
    """One shared population, as the real pipeline has. Per-scenario pools
    would make the IDF text statistics meaningless and would hide the
    cross-record collisions that make this problem hard."""
    return [RazorpaySettlementRecord.model_validate_json(line) for line in POOL.open()]


def make_verifier(kind: str):
    from app.engine.semantic import GeminiSemanticVerifier, HeuristicSemanticVerifier
    import os

    if kind == "gemini":
        if not os.environ.get("GEMINI_API_KEY"):
            raise SystemExit("--backend gemini needs GEMINI_API_KEY in backend/.env")
        return GeminiSemanticVerifier()
    return HeuristicSemanticVerifier()


def run(examples: list[dict], verifier, policy: PolicyConfig) -> dict:
    per_scenario: dict[str, dict] = defaultdict(lambda: {"n": 0, "ok": 0})
    per_truth: dict[str, dict] = defaultdict(lambda: {"n": 0, "ok": 0})
    outcome_by_truth: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    wrong_payment = 0
    ai_calls = 0
    latencies: list[float] = []

    population = load_pool()
    index = matching.ReferenceIndex(population)

    started = time.perf_counter()
    for ex in examples:
        merchant = MerchantRecord.model_validate(ex["merchant"])
        exact = index.exact_candidates(merchant)

        result = policy_engine.reconcile(merchant, exact, index, policy, verifier, ex["example_id"],
                                         as_of=datetime.fromisoformat(ex["as_of"]))
        latencies.append(result.latency_ms)
        ai_calls += result.ai_calls

        truth = ex["settlement_truth"]
        ok = result.outcome in ACCEPTABLE[truth]
        required = REQUIRED_EXCEPTION_TYPE.get(truth)
        if ok and required is not None and result.outcome is ReconciliationOutcome.EXCEPTION:
            ok = result.exception_type in required
        if truth == "present" and ok and result.matched_payment_id != ex["true_payment_id"]:
            ok = False
            wrong_payment += 1
        # Matching the wrong record is worse than finding nothing.
        if truth in ("absent", "pending") and result.matched_payment_id is not None:
            ok = False
            wrong_payment += 1

        per_scenario[ex["scenario"]]["n"] += 1
        per_scenario[ex["scenario"]]["ok"] += ok
        per_truth[truth]["n"] += 1
        per_truth[truth]["ok"] += ok
        outcome_by_truth[truth][result.outcome.value] += 1

    wall = time.perf_counter() - started
    n = len(examples)
    latencies.sort()
    return {
        "examples": n,
        "accuracy": sum(v["ok"] for v in per_truth.values()) / n if n else 0.0,
        "false_match_count": wrong_payment,
        "per_truth": {k: {"n": v["n"], "accuracy": v["ok"] / v["n"] if v["n"] else 0.0}
                      for k, v in sorted(per_truth.items())},
        "per_scenario": {k: {"n": v["n"], "accuracy": v["ok"] / v["n"] if v["n"] else 0.0}
                         for k, v in sorted(per_scenario.items())},
        "outcome_by_truth": {k: dict(v) for k, v in sorted(outcome_by_truth.items())},
        "ai_calls_total": ai_calls,
        "ai_calls_per_1000": (ai_calls / n * 1000) if n else 0.0,
        "wall_clock_seconds": wall,
        "p50_latency_ms": latencies[len(latencies) // 2] if latencies else 0.0,
        "p95_latency_ms": latencies[min(int(len(latencies) * 0.95), len(latencies) - 1)] if latencies else 0.0,
    }


def print_report(r: dict, backend: str) -> None:
    print(f"\nSettlement-presence discrimination (development set, backend={backend})")
    print(f"{r['examples']} scenarios | overall {r['accuracy']:.1%} | "
          f"wrong-record matches {r['false_match_count']} | {r['ai_calls_per_1000']:.0f} model calls/1k")

    print(f"\n{'ground truth':<14}{'n':>5}{'correct':>10}   outcomes produced")
    print("-" * 76)
    for truth, stats in r["per_truth"].items():
        outcomes = r["outcome_by_truth"].get(truth, {})
        rendered = "  ".join(f"{k.replace('HUMAN_REVIEW','HUMAN')}={v}" for k, v in sorted(outcomes.items()))
        print(f"{truth:<14}{stats['n']:>5}{stats['accuracy']:>9.0%}   {rendered}")

    print(f"\n{'scenario':<46}{'n':>5}{'correct':>10}")
    print("-" * 61)
    for name, stats in r["per_scenario"].items():
        print(f"{name:<46}{stats['n']:>5}{stats['accuracy']:>9.0%}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=["heuristic", "gemini"], default="heuristic")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()

    examples = load()
    verifier = make_verifier(args.backend)
    report = run(examples, verifier, PolicyConfig())
    print_report(report, args.backend)

    if args.json:
        args.json.write_text(json.dumps({"backend": args.backend, **report}, indent=2))
        print(f"\nWrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
