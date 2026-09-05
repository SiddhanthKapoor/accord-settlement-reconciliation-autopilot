#!/usr/bin/env python3
"""Freeze a set of evaluation reports against the commit that produced them.

A frozen evaluation is only worth anything if you can tell, later, whether
the code still produces it. This repository learned that the hard way: a
freeze once stamped the commit at *freeze* time rather than at *run* time,
so `--rerun` reproduced a different number and the frozen file looked like
a lie. Reports now carry their own `code_commit` and this script refuses to
freeze a set that disagrees about which commit produced it.

Usage:
    python freeze_evaluation.py <evaluation_id> <report.json> [report.json ...]
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
EVALUATIONS = ROOT / "evaluations"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT).decode().strip()


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2

    evaluation_id, report_paths = argv[1], [Path(p) for p in argv[2:]]
    missing = [p for p in report_paths if not p.exists()]
    if missing:
        print(f"error: no such report(s): {', '.join(str(p) for p in missing)}")
        return 1

    reports = {p: json.loads(p.read_text()) for p in report_paths}

    # Every report in one freeze must come from one commit. Mixing them is
    # how a set of numbers stops meaning anything as a set.
    commits = {r.get("code_commit") for r in reports.values()}
    if len(commits) != 1 or None in commits:
        print("error: reports disagree about the commit that produced them:")
        for p, r in reports.items():
            print(f"    {p.name}: {r.get('code_commit')}")
        return 1
    commit = commits.pop()

    dirty_reports = [p.name for p, r in reports.items() if r.get("working_tree_dirty")]
    if dirty_reports:
        print("error: these reports were produced against a dirty working tree, so they do not\n"
              "       describe any commit: " + ", ".join(dirty_reports) + "\n"
              "       Commit the code and re-run the evaluation.")
        return 1

    if git("status", "--porcelain"):
        print("error: working tree is dirty. Commit first, then freeze —\n"
              "       a freeze that points at a commit the code no longer matches is worse\n"
              "       than no freeze at all.")
        return 1

    head = git("rev-parse", "HEAD")
    if head != commit:
        print(f"error: reports were produced on {commit[:12]} but HEAD is {head[:12]}.\n"
              "       Re-run the evaluation on this commit rather than re-labelling it.")
        return 1

    target = EVALUATIONS / evaluation_id
    target.mkdir(parents=True, exist_ok=True)

    checksums: dict[str, str] = {}
    configurations: dict[str, dict] = {}
    for path, report in reports.items():
        dest = target / path.name
        dest.write_text(json.dumps(report, indent=2, default=str))
        checksums[dest.name] = sha256(dest)
        metrics = report.get("metrics", {})
        configurations[path.stem] = {
            "report_file": dest.name,
            "semantic_backend": report.get("semantic_backend"),
            "reconciliation_accuracy": metrics.get("reconciliation_accuracy"),
            "false_auto_reconciliation_rate": metrics.get("false_auto_reconciliation_rate"),
            "exception_precision": metrics.get("exception_precision"),
            "exception_recall": metrics.get("exception_recall"),
            "pct_human_review": metrics.get("pct_human_review"),
            "ai_invocation_rate": metrics.get("ai_invocation_rate"),
            "provider_errors": metrics.get("provider_errors"),
        }

    # The verifier reads this block, and it is what makes a frozen number
    # attributable to a dataset rather than just to a commit. Reports must
    # agree on it for the same reason they must agree on the commit.
    versions = {r.get("dataset_version") for r in reports.values()}
    seeds = {r.get("seed") for r in reports.values()}
    splits = {r.get("dataset_split") for r in reports.values()}
    if len(versions) != 1 or len(seeds) != 1 or len(splits) != 1:
        print("error: reports disagree about the dataset they were run on:")
        for path, r in reports.items():
            print(f"    {path.name}: version={str(r.get('dataset_version'))[:12]} "
                  f"seed={r.get('seed')} split={r.get('dataset_split')}")
        return 1

    frozen = {
        "evaluation_id": evaluation_id,
        "dataset": {
            "version": versions.pop(),
            "seed": seeds.pop(),
            "split": splits.pop(),
            "record_count": next(iter(reports.values())).get("record_count"),
        },
        "status": "FROZEN — do not modify any file in this directory",
        "frozen_at": datetime.now(timezone.utc).isoformat(),
        "code_commit": commit,
        "configurations": configurations,
        "checksums_sha256": checksums,
    }
    (target / "FROZEN.json").write_text(json.dumps(frozen, indent=2))
    print(f"froze {len(reports)} report(s) as {evaluation_id} at {commit[:12]}")
    for name, digest in checksums.items():
        print(f"    {name}  {digest[:16]}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
