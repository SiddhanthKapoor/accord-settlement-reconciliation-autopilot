#!/usr/bin/env python
"""
Integrity and reproducibility check for the frozen Evaluation V1.

    python verify_evaluation_v1.py                 # V1 integrity (fast)
    python verify_evaluation_v1.py --rerun         # also re-run at the pinned commit
    python verify_evaluation_v1.py v2 v3 --rerun   # any frozen evaluation

Each held-out evaluation is frozen: the dataset bytes, the reports, and
the commit that produced them are pinned in evaluations/<id>/FROZEN.json.
Nothing in later development is allowed to change those numbers
retroactively, so this script exists to prove they haven't been.

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

EVALUATIONS_DIR = Path(__file__).resolve().parent / "evaluations"

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


def check_integrity(frozen: dict, V1_DIR: Path) -> list[str]:
    failures = []

    # A freeze whose pinned commit did not produce its reports cannot be
    # reproduced from that commit, and the --rerun check will disagree for
    # reasons that look like a bug in the engine. Reports now carry the
    # commit that produced them, so the two can be compared directly.
    for report_name in ("report_gemini.json", "report_deterministic.json", "report_heuristic.json"):
        report_path = V1_DIR / report_name
        if not report_path.exists():
            continue
        produced_by = json.loads(report_path.read_text()).get("code_commit")
        if produced_by and produced_by != frozen["code_commit"]:
            failures.append(
                f"{report_name} was produced by {produced_by[:12]} but the freeze pins "
                f"{frozen['code_commit'][:12]} — the pinned commit will not reproduce it"
            )

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
    # Three shapes across the frozen set: V1 records headline metrics per
    # backend, V2/V3 record one flat set, and the final evaluation records
    # a named configuration per backend. All are checked against the
    # report they were copied from.
    if "configurations" in frozen:
        per_backend = {
            "gemini" if key.endswith("gemini") else "deterministic": metrics
            for key, metrics in frozen["configurations"].items()
        }
    else:
        headline = frozen["headline_metrics"]
        per_backend = headline if all(isinstance(v, dict) for v in headline.values()) else {"gemini": headline}
    for backend, metrics in per_backend.items():
        report_path = V1_DIR / f"report_{backend}.json"
        if not report_path.exists():
            continue
        report = json.loads(report_path.read_text())
        for metric, frozen_value in metrics.items():
            if metric not in report["metrics"]:
                continue          # descriptive fields like `backend`
            actual = report["metrics"][metric]
            if actual != frozen_value:
                failures.append(f"{backend}.{metric}: FROZEN.json says {frozen_value}, report says {actual}")

    return failures


def rerun_at_pinned_commit(frozen: dict, V1_DIR: Path, evaluation: str) -> list[str]:
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
        # V1's engine reads the default dataset directory; later ones
        # accept --dataset-dir. Both are fed the frozen bytes either way.
        supports_dir = "--dataset-dir" in (worktree / "backend" / "evaluate.py").read_text()
        datasets = worktree / "backend" / "data" / ("datasets" if not supports_dir else f"datasets_{evaluation}")
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
        cmd = [sys.executable, "evaluate.py", "--dataset", "holdout"]
        if supports_dir:
            cmd += ["--dataset-dir", str(datasets), "--label", f"{evaluation}_rerun"]
        proc = subprocess.run(cmd, cwd=worktree / "backend", env=env, capture_output=True, text=True)
        if proc.returncode != 0:
            return [f"re-run failed at commit {commit[:12]}:\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"]

        report_name = f"latest_{evaluation}_rerun.json" if supports_dir else "latest_holdout.json"
        rerun_report = json.loads((worktree / "backend" / "data" / "eval_reports" / report_name).read_text())
        # The deterministic report is the reproducible one; a Gemini run
        # calls a hosted model and is not byte-reproducible across time.
        baseline = next(
            (V1_DIR / name for name in ("report_heuristic.json", "report_heuristic_new_engine.json",
                                        "report_deterministic.json", "report_gemini.json")
             if (V1_DIR / name).exists()),
            V1_DIR / "report_gemini.json",
        )
        frozen_report = json.loads(baseline.read_text())

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
    parser.add_argument("evaluations", nargs="*", default=None,
                        help="which frozen evaluations to verify (default: all of them)")
    parser.add_argument("--rerun", action="store_true",
                        help="also re-run each at its pinned commit and compare metrics exactly")
    args = parser.parse_args()

    wanted = args.evaluations or sorted(
        p.name for p in EVALUATIONS_DIR.iterdir() if (p / "FROZEN.json").exists()
    )

    all_failures: list[str] = []
    for evaluation in wanted:
        directory = EVALUATIONS_DIR / evaluation
        frozen = json.loads((directory / "FROZEN.json").read_text())

        print(f"\nEvaluation {evaluation.upper()}")
        print(f"  pinned commit   {frozen['code_commit']}")
        print(f"  dataset version {frozen['dataset']['version']}")
        print(f"  frozen at       {frozen['frozen_at']}")

        failures = [f"{evaluation}: {f}" for f in check_integrity(frozen, directory)]
        print(f"  [{'ok' if not failures else 'FAIL'}] frozen file + metric integrity")

        if args.rerun:
            rerun_failures = rerun_at_pinned_commit(frozen, directory, evaluation)
            print(f"  [{'ok' if not rerun_failures else 'FAIL'}] deterministic re-run reproduces frozen metrics")
            failures += [f"{evaluation}: {f}" for f in rerun_failures]

        all_failures += failures

    if all_failures:
        print("\nFAILURES:")
        for f in all_failures:
            print(f"  - {f}")
        return 1

    print(f"\n{len(wanted)} frozen evaluation(s) intact: {', '.join(w.upper() for w in wanted)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
