#!/usr/bin/env python
"""
Held-out evaluation for the Settlement Reconciliation Autopilot.

    python evaluate.py                  # evaluate backend/data/datasets/holdout.jsonl
    python evaluate.py --dataset dev    # evaluate the dev set instead (for iteration, not scoring)

Every number this prints is computed from an actual run in this process,
right now — nothing here is hardcoded or remembered. This script is the
only place a held-out split is read for scoring, and the implementation
is not changed in response to what it prints. See
docs/EVALUATION_METHODOLOGY.md for how the splits are built and what
"held-out" is actually guaranteeing.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from app.domain.models import (  # noqa: E402
    GroundTruth, MerchantRecord, PolicyConfig, RazorpaySettlementRecord, ReconciliationOutcome, ReconciliationRecord,
)
from app.engine.batch import process_batch  # noqa: E402
from app.engine.semantic import get_semantic_verifier  # noqa: E402

DEFAULT_DATA_DIR = Path(__file__).parent / "data" / "datasets"
DATA_DIR = DEFAULT_DATA_DIR
REPORTS_DIR = Path(__file__).parent / "data" / "eval_reports"


def load_pool() -> list[RazorpaySettlementRecord]:
    records = []
    with (DATA_DIR / "razorpay_pool.jsonl").open() as f:
        for line in f:
            records.append(RazorpaySettlementRecord.model_validate_json(line))
    return records


def load_split(name: str) -> list[ReconciliationRecord]:
    records = []
    with (DATA_DIR / f"{name}.jsonl").open() as f:
        for line in f:
            row = json.loads(line)
            merchant = MerchantRecord.model_validate(row["merchant"])
            gt = GroundTruth(case=row["ground_truth_case"], expected_outcome=ReconciliationOutcome(row["ground_truth_outcome"]))
            records.append(ReconciliationRecord(record_id=row["record_id"], merchant=merchant, ground_truth=gt))
    return records


def compute_metrics(records: list[ReconciliationRecord], results) -> dict:
    n = len(records)
    predicted = [r.outcome.value for r in results]
    truth = [rec.ground_truth.expected_outcome.value for rec in records]

    correct = sum(1 for p, t in zip(predicted, truth) if p == t)
    accuracy = correct / n if n else 0.0

    def outcome_stats(label: str) -> dict:
        tp = sum(1 for p, t in zip(predicted, truth) if p == label and t == label)
        fp = sum(1 for p, t in zip(predicted, truth) if p == label and t != label)
        fn = sum(1 for p, t in zip(predicted, truth) if p != label and t == label)
        precision = tp / (tp + fp) if (tp + fp) else None
        recall = tp / (tp + fn) if (tp + fn) else None
        return {"precision": precision, "recall": recall, "predicted_count": tp + fp, "true_count": tp + fn}

    exception_stats = outcome_stats("EXCEPTION")

    n_predicted_reconciled = sum(1 for p in predicted if p == "RECONCILED")
    false_auto_reconciled = sum(1 for p, t in zip(predicted, truth) if p == "RECONCILED" and t != "RECONCILED")
    false_auto_reconciliation_rate = (false_auto_reconciled / n_predicted_reconciled) if n_predicted_reconciled else 0.0

    n_true_reconciled = sum(1 for t in truth if t == "RECONCILED")
    false_exceptions = sum(1 for p, t in zip(predicted, truth) if p == "EXCEPTION" and t == "RECONCILED")
    false_exception_rate = (false_exceptions / n_true_reconciled) if n_true_reconciled else 0.0

    latencies = sorted(r.latency_ms for r in results)

    def percentile(data: list[float], p: float) -> float:
        if not data:
            return 0.0
        k = (len(data) - 1) * p
        f, c = int(k), min(int(k) + 1, len(data) - 1)
        return data[f] if f == c else data[f] + (data[c] - data[f]) * (k - f)

    outcome_counts = {o: predicted.count(o) for o in ("RECONCILED", "EXCEPTION", "HUMAN_REVIEW")}

    by_case: dict[str, dict[str, int]] = {}
    for rec, res in zip(records, results):
        case = rec.ground_truth.case
        by_case.setdefault(case, {"RECONCILED": 0, "EXCEPTION": 0, "HUMAN_REVIEW": 0})
        by_case[case][res.outcome.value] += 1

    ai_invoked_count = sum(1 for r in results if r.ai_invoked)
    ai_calls_total = sum(r.ai_calls for r in results)

    return {
        "record_count": n,
        "reconciliation_accuracy": accuracy,
        "exception_precision": exception_stats["precision"],
        "exception_recall": exception_stats["recall"],
        "false_auto_reconciliation_rate": false_auto_reconciliation_rate,
        "false_auto_reconciliation_rate_definition": "share of records PREDICTED RECONCILED whose ground truth was NOT RECONCILED",
        "false_exception_rate": false_exception_rate,
        "false_exception_rate_definition": "share of TRULY RECONCILED records that were predicted EXCEPTION",
        "pct_auto_reconciled": outcome_counts["RECONCILED"] / n if n else 0.0,
        "pct_human_review": outcome_counts["HUMAN_REVIEW"] / n if n else 0.0,
        "pct_exception": outcome_counts["EXCEPTION"] / n if n else 0.0,
        "ai_invocation_rate": ai_invoked_count / n if n else 0.0,
        "ai_calls_total": ai_calls_total,
        "ai_calls_per_1000_records": (ai_calls_total / n * 1000) if n else 0.0,
        "p50_latency_ms": percentile(latencies, 0.50),
        "p95_latency_ms": percentile(latencies, 0.95),
        "outcome_counts": outcome_counts,
        "outcome_by_ground_truth_case": by_case,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", choices=["holdout", "dev"], default="holdout")
    parser.add_argument("--dataset-dir", type=Path, default=DEFAULT_DATA_DIR,
                        help="Directory holding the split to evaluate. Defaults to the working dataset.")
    parser.add_argument("--label", default=None,
                        help="Name for the report file, e.g. 'v2'. Defaults to the split name.")
    args = parser.parse_args()

    global DATA_DIR
    DATA_DIR = args.dataset_dir

    manifest = json.loads((DATA_DIR / "manifest.json").read_text())
    pool = load_pool()
    records = load_split(args.dataset)

    print(f"Settlement Reconciliation Autopilot — evaluation ({args.dataset} set)")
    print(f"dataset_version={manifest['dataset_version']}  seed={manifest['seed']}  records={len(records)}")

    policy = PolicyConfig()
    semantic_verifier = get_semantic_verifier()
    backend_name = type(semantic_verifier).__name__
    print(f"semantic backend: {backend_name}")

    started = time.perf_counter()
    results = process_batch(records, pool, policy=policy, semantic_verifier=semantic_verifier)
    wall_seconds = time.perf_counter() - started
    throughput = len(records) / wall_seconds if wall_seconds > 0 else 0.0

    metrics = compute_metrics(records, results)
    metrics["throughput_records_per_sec"] = throughput
    metrics["wall_clock_seconds"] = wall_seconds

    print("\n--- Metrics ---")
    print(f"{'Reconciliation accuracy':<38} {metrics['reconciliation_accuracy']:.1%}")
    print(f"{'Exception precision':<38} {metrics['exception_precision']:.1%}" if metrics['exception_precision'] is not None else f"{'Exception precision':<38} n/a")
    print(f"{'Exception recall':<38} {metrics['exception_recall']:.1%}" if metrics['exception_recall'] is not None else f"{'Exception recall':<38} n/a")
    print(f"{'False auto-reconciliation rate':<38} {metrics['false_auto_reconciliation_rate']:.1%}")
    print(f"{'False exception rate':<38} {metrics['false_exception_rate']:.1%}")
    print(f"{'Auto-reconciled':<38} {metrics['pct_auto_reconciled']:.1%}")
    print(f"{'Routed to human review':<38} {metrics['pct_human_review']:.1%}")
    print(f"{'Flagged as exception':<38} {metrics['pct_exception']:.1%}")
    print(f"{'AI invocation rate':<38} {metrics['ai_invocation_rate']:.1%}")
    print(f"{'Model calls per 1,000 records':<38} {metrics['ai_calls_per_1000_records']:.0f}")
    print(f"{'Throughput':<38} {throughput:.1f} records/sec")
    print(f"{'p50 latency':<38} {metrics['p50_latency_ms']:.2f} ms")
    print(f"{'p95 latency':<38} {metrics['p95_latency_ms']:.2f} ms")

    print("\n--- Outcome by ground-truth case ---")
    print(f"{'CASE':<45} {'RECONCILED':>11} {'EXCEPTION':>10} {'HUMAN_REV':>10}")
    for case, counts in sorted(metrics["outcome_by_ground_truth_case"].items()):
        print(f"{case:<45} {counts['RECONCILED']:>11} {counts['EXCEPTION']:>10} {counts['HUMAN_REVIEW']:>10}")

    # The commit that produced these numbers, recorded by the run itself.
    # Stamping it at freeze time instead let a freeze point at code that no
    # longer reproduced its own report — caught by verify_evaluation_v1.py
    # --rerun, which is what that check is for.
    try:
        code_commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parent,
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:  # noqa: BLE001 — a report outside a checkout is still valid
        code_commit = None

    report = {
        "code_commit": code_commit,
        "dataset_version": manifest["dataset_version"],
        "dataset_split": args.dataset,
        "seed": manifest["seed"],
        "record_count": len(records),
        "semantic_backend": backend_name,
        "policy": policy.model_dump(),
        "metrics": metrics,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = REPORTS_DIR / f"latest_{args.label or args.dataset}.json"
    report_path.write_text(json.dumps(report, indent=2, default=str))
    print(f"\nFull report written to {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
