#!/usr/bin/env python
"""
Ambiguous-matching benchmark and tier ablation.

    python benchmark_matching.py                    # every configuration
    python benchmark_matching.py --config D         # just the Gemini one
    python benchmark_matching.py --json out.json    # machine-readable

Runs the real `policy.reconcile` path — not a reimplementation of it —
against backend/data/datasets/ambiguous_benchmark.jsonl and scores the
matching decision alone: did the engine pick the settlement record that
is genuinely the same payment, pick the wrong one, or correctly pick
none?

This is development instrumentation. The benchmark it reads is a
development set; nothing here is a headline metric, and the held-out
evaluation sets are never touched by this script.

Configurations, which double as the tier ablation:

    A  exact normalized-reference matching only
    B  exact + deterministic corroborated matching
    C  exact + deterministic + heuristic semantic fallback
    D  exact + deterministic + Gemini semantic verifier
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from app.domain.models import MerchantRecord, PolicyConfig, RazorpaySettlementRecord  # noqa: E402
from app.engine import matching  # noqa: E402
from app.engine import policy as policy_engine  # noqa: E402
from app.engine.semantic import GeminiSemanticVerifier, HeuristicSemanticVerifier  # noqa: E402

BENCHMARK_PATH = Path(__file__).parent / "data" / "datasets" / "ambiguous_benchmark.jsonl"

CONFIGS = {
    "A": ("exact reference only", dict(enable_fuzzy_matching=False, enable_semantic_matching=False), None),
    "B": ("+ deterministic corroborated", dict(enable_fuzzy_matching=True, enable_semantic_matching=False), None),
    "C": ("+ heuristic semantic", dict(enable_fuzzy_matching=True, enable_semantic_matching=True), "heuristic"),
    "D": ("+ Gemini semantic", dict(enable_fuzzy_matching=True, enable_semantic_matching=True), "gemini"),
}


def load_examples() -> list[dict]:
    if not BENCHMARK_PATH.exists():
        raise SystemExit(
            f"missing {BENCHMARK_PATH}\nGenerate it first:\n"
            "  python data/generate_ambiguous_benchmark.py --seed 771 --count 240"
        )
    return [json.loads(line) for line in BENCHMARK_PATH.open()]


def make_verifier(kind: str | None):
    if kind == "gemini":
        if not os.environ.get("GEMINI_API_KEY"):
            raise SystemExit("config D needs GEMINI_API_KEY set in backend/.env")
        return GeminiSemanticVerifier()
    if kind == "heuristic":
        return HeuristicSemanticVerifier()

    class _Unused:
        def compare(self, comparison):  # pragma: no cover - never reached when semantics are off
            raise AssertionError("semantic verifier called while disabled")

    return _Unused()


def run_config(examples: list[dict], key: str) -> dict:
    label, overrides, verifier_kind = CONFIGS[key]
    policy = PolicyConfig(**overrides)
    verifier = make_verifier(verifier_kind)

    correct = wrong_match = missed = false_match = correct_reject = 0
    ai_records = ai_calls = 0
    latencies: list[float] = []
    per_variation: dict[str, dict[str, int]] = {}

    started = time.perf_counter()
    for ex in examples:
        merchant = MerchantRecord.model_validate(ex["merchant"])
        candidates = [RazorpaySettlementRecord.model_validate(c) for c in ex["candidates"]]
        index = matching.ReferenceIndex(candidates)
        exact = index.exact_candidates(merchant)

        result = policy_engine.reconcile(merchant, exact, index, policy, verifier, ex["example_id"])
        predicted = result.matched_payment_id
        truth = ex["true_payment_id"]

        latencies.append(result.latency_ms)
        if result.ai_invoked:
            ai_records += 1
        ai_calls += result.ai_calls

        bucket = per_variation.setdefault(ex["variation"], {"n": 0, "correct": 0})
        bucket["n"] += 1

        if truth is None:
            if predicted is None:
                correct += 1
                correct_reject += 1
                bucket["correct"] += 1
            else:
                false_match += 1
        else:
            if predicted == truth:
                correct += 1
                bucket["correct"] += 1
            elif predicted is None:
                missed += 1
            else:
                wrong_match += 1
    wall = time.perf_counter() - started

    n = len(examples)
    n_true = sum(1 for e in examples if e["is_true_match"])
    n_false = n - n_true
    latencies.sort()

    def pct(x: int, total: int) -> float:
        return x / total if total else 0.0

    return {
        "config": key,
        "label": label,
        "examples": n,
        "accuracy": pct(correct, n),
        "true_match_recall": pct(n_true - missed - wrong_match, n_true),
        "correct_rejection_rate": pct(correct_reject, n_false),
        "wrong_match_rate": pct(wrong_match, n_true),
        "false_match_rate": pct(false_match, n_false),
        "missed_rate": pct(missed, n_true),
        "ai_invocation_rate": pct(ai_records, n),
        "ai_calls_total": ai_calls,
        "ai_calls_per_1000_records": (ai_calls / n * 1000) if n else 0.0,
        "wall_clock_seconds": wall,
        "throughput_per_sec": n / wall if wall else 0.0,
        "p50_latency_ms": latencies[len(latencies) // 2] if latencies else 0.0,
        "p95_latency_ms": latencies[min(int(len(latencies) * 0.95), len(latencies) - 1)] if latencies else 0.0,
        "per_variation_accuracy": {
            k: {"n": v["n"], "accuracy": pct(v["correct"], v["n"])} for k, v in sorted(per_variation.items())
        },
    }


def print_report(results: list[dict]) -> None:
    print("\nAmbiguous-matching benchmark (development set)")
    print(f"{'':<4}{'configuration':<32}{'acc':>7}{'recall':>8}{'reject':>8}{'wrong':>7}{'AI/rec':>8}{'calls/1k':>10}")
    print("-" * 84)
    for r in results:
        print(f"{r['config']:<4}{r['label']:<32}{r['accuracy']:>6.1%}{r['true_match_recall']:>8.1%}"
              f"{r['correct_rejection_rate']:>8.1%}{r['wrong_match_rate']:>7.1%}"
              f"{r['ai_invocation_rate']:>8.1%}{r['ai_calls_per_1000_records']:>10.0f}")

    print("\nPer-variation accuracy")
    variations = sorted({v for r in results for v in r["per_variation_accuracy"]})
    header = "".join(f"{r['config']:>9}" for r in results)
    print(f"{'variation':<34}{'n':>4}{header}")
    print("-" * (38 + 9 * len(results)))
    for v in variations:
        n = next(r["per_variation_accuracy"][v]["n"] for r in results if v in r["per_variation_accuracy"])
        cells = "".join(f"{r['per_variation_accuracy'].get(v, {}).get('accuracy', 0):>8.0%} " for r in results)
        print(f"{v:<34}{n:>4}{cells}")

    print("\nLatency and cost")
    for r in results:
        print(f"  {r['config']}  p50 {r['p50_latency_ms']:>8.2f} ms   p95 {r['p95_latency_ms']:>9.2f} ms   "
              f"{r['throughput_per_sec']:>8.1f} rec/s   {r['ai_calls_total']:>4} model calls")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", action="append", choices=sorted(CONFIGS), help="run only these (repeatable)")
    parser.add_argument("--json", type=Path, help="write the full report here")
    args = parser.parse_args()

    examples = load_examples()
    keys = args.config or sorted(CONFIGS)
    print(f"{len(examples)} examples from {BENCHMARK_PATH.name} "
          f"({sum(1 for e in examples if e['is_true_match'])} true matches / "
          f"{sum(1 for e in examples if not e['is_true_match'])} non-matches)")

    results = []
    for key in keys:
        print(f"  running {key}...", flush=True)
        results.append(run_config(examples, key))

    print_report(results)

    if args.json:
        args.json.write_text(json.dumps({"benchmark": BENCHMARK_PATH.name, "results": results}, indent=2))
        print(f"\nWrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
