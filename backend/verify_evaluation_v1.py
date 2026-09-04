#!/usr/bin/env python
"""
Integrity and reproducibility check for the frozen Evaluation V1.

    python verify_evaluation_v1.py            # checksum + metric integrity (fast)
    python verify_evaluation_v1.py --rerun    # also re-run V1 at its pinned commit

V1 is the first held-out evaluation of this system. It is frozen: the
dataset bytes, the reports, and the commit that produced them are all
pinned in evaluations/v1/FROZEN.json. Nothing in later development is
allowed to change those numbers retroactively, so this script exists to
prove they haven't been.

--rerun checks out the pinned commit into a throwaway git worktree,
feeds it the frozen dataset, and re-runs the evaluation with the
heuristic backend (the deterministic one -- the Gemini path calls a
hosted model and is not byte-reproducible, which is stated in
FROZEN.json rather than papered over). If the recomputed metrics differ
from the frozen report at all, this exits non-zero.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

V1_DIR = Path(__file__).resolve().parent / "evaluations" / "v1"

# Metrics compared on a --rerun. Latency and throughput are wall-clock
# measurements of the machine, not properties of the evaluation, so they
# are deliberately excluded.
REPRODUCIBLE_METRICS = (
    "reconciliation_accuracy",
    "exception_precision",
    "exception_recall",
    "false_auto_reconciliation_rate",
    "false_exception_rate",
    "pct_auto_reconciled",
    "pct_human_review",
    "pct_exception",
    "ai_invocation_rate",
    "outcome_counts",
    "outcome_by_ground_truth_case",
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_integrity(frozen: dict) -> list[str]:
    failures = []

    for name, expected in frozen["checksums_sha256"].items():
        path = V1_DIR / name
        if not path.exists():
            failures.append(f"missing frozen file: {name}")
            continue
        actual = sha256(path)
        if actual != expected:
            failures.append(f"{name}: checksum changed\n    frozen:  {expected}\n    on disk: {actual}")

    # The headline metrics recorded in FROZEN.json must still agree with
    # the reports they were copied from -- catches a report being edited
    # without the summary being updated, or vice versa.
    for backend, report_name in (("gemini", "report_gemini.json"), ("heuristic", "report_heuristic.json")):
        report_path = V1_DIR / report_name
        if not report_path.exists():
            continue
        report = json.loads(report_path.read_text())
        for metric, frozen_value in frozen["headline_metrics"][backend].items():
            actual = report["metrics"][metric]
            if actual != frozen_value:
                failures.append(f"{backend}.{metric}: FROZEN.json says {frozen_value}, report says {actual}")

    return failures


def rerun_at_pinned_commit(frozen: dict) -> list[str]:
    commit = frozen["code_commit"]
    repo_root = Path(__file__).resolve().parent.parent
    workdir = Path(tempfile.mkdtemp(prefix="eval_v1_rerun_"))
    worktree = workdir / "repo"

    try:
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(worktree), commit],
            cwd=repo_root, check=True, capture_output=True, text=True,
        )

        # Feed the pinned code the frozen dataset, not a regenerated one.
        datasets = worktree / "backend" / "data" / "datasets"
        datasets.mkdir(parents=True, exist_ok=True)
        shutil.copy(V1_DIR / "dataset_manifest.json", datasets / "manifest.json")
        for name in ("holdout", "razorpay_pool"):
            with gzip.open(V1_DIR / f"{name}.jsonl.gz", "rb") as src, (datasets / f"{name}.jsonl").open("wb") as dst:
                shutil.copyfileobj(src, dst)

        env = {
            "PATH": __import__("os").environ["PATH"],
            "HOME": __import__("os").environ.get("HOME", ""),
            "GEMINI_API_KEY": "",  # force the deterministic backend
        }
        proc = subprocess.run(
            [sys.executable, "evaluate.py", "--dataset", "holdout"],
            cwd=worktree / "backend", env=env, capture_output=True, text=True,
        )
        if proc.returncode != 0:
            return [f"re-run failed at commit {commit[:12]}:\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"]

        rerun_report = json.loads((worktree / "backend" / "data" / "eval_reports" / "latest_holdout.json").read_text())
        frozen_report = json.loads((V1_DIR / "report_heuristic.json").read_text())

        failures = []
        if rerun_report["dataset_version"] != frozen_report["dataset_version"]:
            failures.append("dataset_version mismatch -- the frozen dataset is not what was evaluated")
        for metric in REPRODUCIBLE_METRICS:
            if rerun_report["metrics"][metric] != frozen_report["metrics"][metric]:
                failures.append(
                    f"{metric}: frozen {frozen_report['metrics'][metric]!r} != re-run {rerun_report['metrics'][metric]!r}"
                )
        return failures
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(worktree)],
                       cwd=repo_root, capture_output=True, text=True)
        shutil.rmtree(workdir, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rerun", action="store_true",
                        help="also re-run V1 at its pinned commit and compare metrics exactly")
    args = parser.parse_args()

    frozen = json.loads((V1_DIR / "FROZEN.json").read_text())

    print("Evaluation V1 verification")
    print(f"  pinned commit   {frozen['code_commit']}")
    print(f"  dataset version {frozen['dataset']['version']}")
    print(f"  frozen at       {frozen['frozen_at']}")

    failures = check_integrity(frozen)
    print(f"\n[{'ok' if not failures else 'FAIL'}] frozen file + metric integrity")

    if args.rerun:
        print("\nRe-running V1 at its pinned commit (heuristic backend, frozen dataset)...")
        rerun_failures = rerun_at_pinned_commit(frozen)
        print(f"[{'ok' if not rerun_failures else 'FAIL'}] deterministic re-run reproduces the frozen metrics")
        failures += rerun_failures

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("\nV1 is intact.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
