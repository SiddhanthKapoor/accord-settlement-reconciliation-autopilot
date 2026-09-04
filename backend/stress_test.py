#!/usr/bin/env python
"""
Scalability / Throughput Evaluation.

    python stress_test.py                          # 1k, 5k, 10k
    python stress_test.py --sizes 1000 50000       # explicit sizes
    python stress_test.py --json stress.json

This measures how the pipeline behaves as batch size grows. It is NOT an
accuracy evaluation and must never be quoted as one: the records are
freshly generated at each size, and the point is wall-clock behaviour,
memory growth, and the shape of the outcome mix -- not whether the
decisions are right. Accuracy lives in evaluate.py against a held-out
set, and nothing here should be read as evidence about real-world
reconciliation volume.

Runs on the deterministic backend by default. A hosted model call is
network-bound at roughly a second per call, so including it would
measure Google's latency rather than this system's throughput; use
--with-ai to measure the realistic mixed path instead, on small sizes.
"""

from __future__ import annotations

import argparse
import gc
import json
import random
import resource
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "data"))

from app.domain.models import (  # noqa: E402
    GroundTruth, MerchantRecord, PolicyConfig, RazorpaySettlementRecord,
    ReconciliationOutcome, ReconciliationRecord,
)
from app.engine.batch import process_batch  # noqa: E402
from app.engine.semantic import HeuristicSemanticVerifier  # noqa: E402

DEFAULT_SIZES = [1000, 5000, 10000]


def peak_rss_mb() -> float:
    """Peak resident set size for this process. On Linux getrusage
    reports kilobytes, on macOS bytes -- normalise both to MB."""
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw / (1024 * 1024) if sys.platform == "darwin" else raw / 1024


def build_workload(size: int, seed: int) -> tuple[list[ReconciliationRecord], list[RazorpaySettlementRecord]]:
    """Generate a fresh workload at this size using the same generator
    the evaluation datasets come from, so the category mix (and so the
    proportion of records that reach the expensive paths) is realistic."""
    from generate_dataset import Generator  # noqa: PLC0415 - path set above

    gen = Generator(seed)
    gen.generate(size)

    records = [
        ReconciliationRecord(
            record_id=r.record_id,
            merchant=MerchantRecord.model_validate(
                {**r.merchant.__dict__} if hasattr(r.merchant, "__dict__") else r.merchant
            ),
            ground_truth=GroundTruth(case=r.ground_truth_case,
                                     expected_outcome=ReconciliationOutcome(r.ground_truth_outcome)),
        )
        for r in gen.gt_records
    ]
    pool = [RazorpaySettlementRecord.model_validate(p.__dict__) for p in gen.razorpay_pool]
    return records, pool


def percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    k = (len(sorted_values) - 1) * p
    f, c = int(k), min(int(k) + 1, len(sorted_values) - 1)
    return sorted_values[f] if f == c else sorted_values[f] + (sorted_values[c] - sorted_values[f]) * (k - f)


def run_size(size: int, seed: int, verifier) -> dict:
    records, pool = build_workload(size, seed)

    gc.collect()
    rss_before = peak_rss_mb()

    build_started = time.perf_counter()
    started = time.perf_counter()
    results = process_batch(records, pool, policy=PolicyConfig(), semantic_verifier=verifier)
    wall = time.perf_counter() - started

    latencies = sorted(r.latency_ms for r in results)
    outcomes = {o.value: 0 for o in ReconciliationOutcome}
    for r in results:
        outcomes[r.outcome.value] += 1

    n = len(results)
    return {
        "records": n,
        "settlement_pool": len(pool),
        "wall_clock_seconds": round(wall, 3),
        "throughput_per_sec": round(n / wall, 1) if wall else 0.0,
        "p50_latency_ms": round(percentile(latencies, 0.50), 4),
        "p95_latency_ms": round(percentile(latencies, 0.95), 4),
        "p99_latency_ms": round(percentile(latencies, 0.99), 4),
        "max_latency_ms": round(latencies[-1], 4) if latencies else 0.0,
        "peak_rss_mb": round(peak_rss_mb(), 1),
        "rss_growth_mb": round(peak_rss_mb() - rss_before, 1),
        "ai_invocation_rate": round(sum(1 for r in results if r.ai_invoked) / n, 4) if n else 0.0,
        "ai_calls_total": sum(r.ai_calls for r in results),
        "pct_reconciled": round(outcomes["RECONCILED"] / n, 4) if n else 0.0,
        "pct_exception": round(outcomes["EXCEPTION"] / n, 4) if n else 0.0,
        "pct_human_review": round(outcomes["HUMAN_REVIEW"] / n, 4) if n else 0.0,
        "setup_plus_run_seconds": round(time.perf_counter() - build_started, 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", type=int, nargs="+", default=DEFAULT_SIZES)
    parser.add_argument("--seed", type=int, default=4242)
    parser.add_argument("--with-ai", action="store_true",
                        help="use the real model instead of the deterministic backend (network-bound; small sizes only)")
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()

    if args.with_ai:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent / ".env")
        from app.engine.semantic import get_semantic_verifier
        verifier = get_semantic_verifier()
    else:
        verifier = HeuristicSemanticVerifier()

    print("Scalability / Throughput Evaluation")
    print(f"backend: {type(verifier).__name__}   (this measures speed and memory, NOT accuracy)\n")
    print(f"{'records':>9}{'pool':>9}{'seconds':>10}{'rec/sec':>11}{'p50 ms':>9}"
          f"{'p95 ms':>9}{'p99 ms':>9}{'peak MB':>10}{'AI %':>7}")
    print("-" * 83)

    rows = []
    for size in sorted(args.sizes):
        row = run_size(size, args.seed, verifier)
        rows.append(row)
        print(f"{row['records']:>9,}{row['settlement_pool']:>9,}{row['wall_clock_seconds']:>10.2f}"
              f"{row['throughput_per_sec']:>11,.0f}{row['p50_latency_ms']:>9.3f}{row['p95_latency_ms']:>9.3f}"
              f"{row['p99_latency_ms']:>9.3f}{row['peak_rss_mb']:>10.1f}{row['ai_invocation_rate']:>7.1%}")

    print("\nOutcome mix (a property of the generated category shares, not a quality measure)")
    print(f"{'records':>9}{'reconciled':>13}{'exception':>12}{'human review':>15}")
    print("-" * 49)
    for row in rows:
        print(f"{row['records']:>9,}{row['pct_reconciled']:>12.1%}{row['pct_exception']:>12.1%}"
              f"{row['pct_human_review']:>15.1%}")

    if len(rows) > 1:
        print("\nScaling, step by step (a first-to-last ratio would misread this: the smallest batch "
              "runs before the\nwindow-scan bound binds, so it is unrepresentatively fast per record).")
        for previous, current in zip(rows, rows[1:]):
            record_ratio = current["records"] / previous["records"]
            time_ratio = (current["wall_clock_seconds"] / previous["wall_clock_seconds"]
                          if previous["wall_clock_seconds"] else 0)
            verdict = "linear" if time_ratio <= record_ratio * 1.15 else "super-linear"
            print(f"  {previous['records']:>6,} -> {current['records']:>6,}   "
                  f"{record_ratio:.0f}x records, {time_ratio:.1f}x time   ({verdict})")

        throughputs = [r["throughput_per_sec"] for r in rows]
        print(f"\nThroughput across all sizes: {min(throughputs):,.0f}-{max(throughputs):,.0f} records/sec. "
              "Roughly flat\nthroughput as the population grows is the actual evidence of linear scaling.")
        print(f"Peak RSS at the largest size: {rows[-1]['peak_rss_mb']:,.0f} MB. The batch holds every record, "
              "the whole\nsettlement population and every result in memory at once; that is the ceiling to watch.")

    if args.json:
        args.json.write_text(json.dumps({
            "evaluation_type": "scalability_throughput",
            "not_an_accuracy_measurement": True,
            "backend": type(verifier).__name__,
            "seed": args.seed,
            "results": rows,
        }, indent=2))
        print(f"\nWrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
