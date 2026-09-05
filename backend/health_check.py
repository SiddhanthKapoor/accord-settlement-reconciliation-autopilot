#!/usr/bin/env python
"""
Probe every configured LLM provider with one real structured request and
print what came back.

Run it before a demo, after rotating a key, or the moment the UI's status
light goes amber:

    cd backend && ../.venv/bin/python health_check.py

It does a real call rather than checking that a key is non-empty, because
"the key is set" and "the provider will answer" are different claims and
only the second one matters. Exit code is 0 if at least one provider
answered, 1 if none did, 2 if none is configured.

It never prints a key: values are read from the environment and the
provider layer scrubs anything credential-shaped out of error text before
it gets here.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent / ".env")

from app.engine.providers import (  # noqa: E402
    GeminiProvider,
    GroqProvider,
    ProviderError,
    ProviderHealth,
    build_chain,
)

_COLUMNS = [
    ("PROVIDER", 10),
    ("AVAILABLE", 10),
    ("MODEL", 26),
    ("LATENCY", 10),
    ("ERROR KIND", 20),
]


def _row(cells: list[str]) -> str:
    return "  ".join(cell[:width].ljust(width) for cell, (_, width) in zip(cells, _COLUMNS))


def _fmt(health: ProviderHealth) -> list[str]:
    return [
        health.provider,
        "yes" if health.available else "NO",
        health.model or "-",
        f"{health.latency_ms:.0f} ms" if health.latency_ms is not None else "-",
        health.error_kind.value if health.error_kind else "-",
    ]


def main() -> int:
    print("Accord — LLM provider health check")
    print()

    configured = []
    for label, factory, key_env in (
        ("gemini", GeminiProvider, "GEMINI_API_KEY"),
        ("groq", GroqProvider, "GROQ_API_KEY"),
    ):
        try:
            configured.append(factory())
        except ProviderError as exc:
            print(f"  {label}: not configured ({exc.detail}) — set {key_env} in backend/.env to enable it.")

    if not configured:
        print()
        print("No provider is configured. Reconciliation will run on the offline")
        print("heuristic verifier (deterministic, clearly labeled, never confident).")
        return 2

    if len(configured) < 2:
        print()

    chain = build_chain()
    # Probe the providers we constructed here so the table reflects this
    # run even when build_chain() sees a different environment.
    results = [p.health() for p in configured]
    for result in results:  # keep the chain's view in sync so `status` is real
        chain._observed[result.provider] = result.available  # noqa: SLF001 — same package, deliberate

    print(_row([label for label, _ in _COLUMNS]))
    print("  ".join("-" * width for _, width in _COLUMNS))
    for result in results:
        print(_row(_fmt(result)))

    print()
    for result in results:
        if result.detail:
            print(f"  {result.provider}: {result.detail}")

    print()
    print(f"chain status: {chain.status}")
    if os.environ.get("ACCORD_AI_DISABLED"):
        print("note: ACCORD_AI_DISABLED is set — the engine will use the offline heuristic regardless.")

    return 0 if any(r.available for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
