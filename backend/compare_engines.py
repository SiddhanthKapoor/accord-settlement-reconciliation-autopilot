#!/usr/bin/env python
"""
Run the pre-hardening engine and the current engine over the same
dataset, so the difference between them is attributable to the code.

    python compare_engines.py --dataset-dir data/datasets_v2

Comparing V1's reported numbers against V2's is weaker than it looks:
they were measured on different data. Same generator and same category
shares, so the distributions match, but not the same records. This
removes that confound by checking the old commit out into a throwaway
worktree, handing it the *V2* dataset, and scoring both engines on
identical input.

Both sides run the deterministic heuristic backend. That is the point:
it makes the comparison exactly reproducible and isolates the change to
the matching and policy code, rather than mixing in the run-to-run
variation of a hosted model. The model's contribution is measured
separately, by the ablation in benchmark_matching.py.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
V1_COMMIT = json.loads((Path(__file__).resolve().parent / "evaluations" / "v1" / "FROZEN.json").read_text())["code_commit"]

HEADLINE = [
    ("reconciliation_accuracy", "Reconciliation accuracy", True),
    ("exception_precision", "Exception precision", True),
    ("exception_recall", "Exception recall", True),
    ("false_auto_reconciliation_rate", "False auto-reconciliation rate", True),
    ("false_exception_rate", "False exception rate", True),
    ("pct_auto_reconciled", "Auto-reconciled", True),
    ("pct_human_review", "Routed to human review", True),
    ("pct_exception", "Flagged as exception", True),
    ("ai_invocation_rate", "AI invocation rate", True),
]


def run_evaluation(cwd: Path, dataset_dir: Path, label: str, supports_dataset_dir: bool) -> dict:
    env = dict(os.environ)
    env["GEMINI_API_KEY"] = ""  # deterministic backend on both sides

    if supports_dataset_dir:
        cmd = [sys.executable, "evaluate.py", "--dataset", "holdout",
               "--dataset-dir", str(dataset_dir), "--label", label]
    else:
        # The old commit has no --dataset-dir, so the dataset is placed
        # where it expects to find it.
        target = cwd / "data" / "datasets"
        target.mkdir(parents=True, exist_ok=True)
        for name in ("holdout.jsonl", "razorpay_pool.jsonl", "manifest.json"):
            shutil.copy(dataset_dir / name, target / name)
        cmd = [sys.executable, "evaluate.py", "--dataset", "holdout"]

    proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"evaluation failed in {cwd}:\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}")

    report_name = f"latest_{label}.json" if supports_dataset_dir else "latest_holdout.json"
    return json.loads((cwd / "data" / "eval_reports" / report_name).read_text())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-dir", type=Path, default=Path("data/datasets_v2"))
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()

    dataset_dir = args.dataset_dir.resolve()
    workdir = Path(tempfile.mkdtemp(prefix="engine_compare_"))
    worktree = workdir / "old"

    try:
        subprocess.run(["git", "worktree", "add", "--detach", str(worktree), V1_COMMIT],
                       cwd=REPO_ROOT, check=True, capture_output=True, text=True)

        print(f"dataset : {dataset_dir}")
        print(f"old     : {V1_COMMIT[:12]} (pre-hardening)")
        print("new     : working tree")
        print("backend : deterministic heuristic on both sides\n")

        old = run_evaluation(worktree / "backend", dataset_dir, "old_engine", supports_dataset_dir=False)
        new = run_evaluation(Path(__file__).resolve().parent, dataset_dir, "new_engine", supports_dataset_dir=True)

        print(f"{'metric':<34}{'old':>10}{'new':>10}{'change':>11}")
        print("-" * 65)
        for key, label, as_pct in HEADLINE:
            o, n = old["metrics"].get(key), new["metrics"].get(key)
            if o is None or n is None:
                continue
            delta = n - o
            fmt = (lambda v: f"{v:.1%}") if as_pct else (lambda v: f"{v:.3f}")
            arrow = "" if abs(delta) < 1e-9 else ("+" if delta > 0 else "")
            print(f"{label:<34}{fmt(o):>10}{fmt(n):>10}{arrow + fmt(delta):>11}")

        print(f"\n{'category':<46}{'old':>10}{'new':>10}")
        print("-" * 66)
        old_cases = old["metrics"]["outcome_by_ground_truth_case"]
        new_cases = new["metrics"]["outcome_by_ground_truth_case"]
        for case in sorted(set(old_cases) | set(new_cases)):
            o_counts, n_counts = old_cases.get(case, {}), new_cases.get(case, {})
            total = sum(n_counts.values()) or 1
            # Correct outcome for a category is whichever the generator
            # assigns; inferred here from the dominant expected label.
            print(f"{case:<46}{_summary(o_counts):>10}{_summary(n_counts):>10}")

        if args.json:
            args.json.write_text(json.dumps({"old_commit": V1_COMMIT, "dataset_dir": str(dataset_dir),
                                             "old": old, "new": new}, indent=2))
            print(f"\nWrote {args.json}")
        return 0
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(worktree)],
                       cwd=REPO_ROOT, capture_output=True, text=True)
        shutil.rmtree(workdir, ignore_errors=True)


def _summary(counts: dict) -> str:
    if not counts:
        return "-"
    dominant = max(counts, key=counts.get)
    return f"{dominant[:3]} {counts[dominant]}"


if __name__ == "__main__":
    raise SystemExit(main())
