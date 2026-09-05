"""
AI provider health, for the UI's status indicator.

Two things shape this endpoint. First, it is polled: something in the UI
wants to show whether the model layer is up, and a naive implementation
would spend a live model call per poll per open tab — which is how a
status light quietly burns the quota it is reporting on. So the result is
cached for 60 seconds and only an explicit `?refresh=true` bypasses it.

Second, an honest "available" cannot be inferred from the presence of a
key. It requires actually calling the provider, which is what
ProviderHealth records — including the latency, and including the
error *kind* when it is down, so the UI can say "quota exhausted" rather
than the useless "AI unavailable".
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Query

from app.engine import semantic
from app.engine.providers import (
    AI_AVAILABLE,
    AI_FALLBACK_ACTIVE,
    AI_UNAVAILABLE,
    FallbackChain,
    GeminiProvider,
    GroqProvider,
    ProviderErrorKind,
    ProviderHealth,
    build_chain,
    scrub_secrets,
)
from app.ledger import store

router = APIRouter(tags=["ai"])

HEALTH_CACHE_TTL_SECONDS = 60.0

_lock = threading.Lock()
_chain: FallbackChain | None = None
_cache: dict[str, Any] | None = None
_cached_at: float = 0.0

# When each provider was last observed to answer, in this process. Only a
# probe that actually returned writes here, and only on an uncached
# probe — replaying a cached result as a fresh success would turn one
# real call into a clock.
_last_success: dict[str, str] = {}


def _get_chain() -> FallbackChain:
    """One chain per process, so `status` reflects the outcomes the running
    system actually observed rather than a fresh object's blank slate."""
    global _chain
    if _chain is None:
        _chain = build_chain()
    return _chain


def _serialize(health: ProviderHealth) -> dict[str, Any]:
    return {
        "provider": health.provider,
        "available": health.available,
        "model": health.model,
        "latency_ms": health.latency_ms,
        "error_kind": health.error_kind.value if health.error_kind else None,
        # Scrubbed again at the boundary. The provider layer already does
        # this; doing it twice costs nothing and means a future provider
        # that forgets cannot leak a key through the API.
        "detail": scrub_secrets(health.detail or ""),
    }


def _build_report() -> dict[str, Any]:
    if semantic.ai_disabled():
        return {
            "status": AI_UNAVAILABLE,
            "providers": [],
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "detail": f"{semantic.AI_DISABLED_ENV} is set — the offline heuristic verifier is in use.",
        }

    chain = _get_chain()
    reports = chain.health()
    return {
        "status": chain.status,
        "providers": [_serialize(h) for h in reports],
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "detail": (
            "No LLM provider is configured; the offline heuristic verifier is in use."
            if not reports else
            f"{sum(1 for h in reports if h.available)}/{len(reports)} providers responded."
        ),
    }


def _health_report(refresh: bool = False) -> dict[str, Any]:
    global _cache, _cached_at
    with _lock:
        fresh_enough = (
            _cache is not None and (time.monotonic() - _cached_at) < HEALTH_CACHE_TTL_SECONDS
        )
        if fresh_enough and not refresh:
            return {**_cache, "cached": True}

        report = _build_report()
        _cache = report
        _cached_at = time.monotonic()
        # A provider that answered this probe answered a real structured
        # request, just now. That is the only thing recorded as a success.
        for provider in report.get("providers", []):
            if provider.get("available"):
                _last_success[provider["provider"]] = report["checked_at"]
        return {**report, "cached": False}


def _reset_cache_for_tests() -> None:
    """Drop the memoised chain, cached probe and observed successes. Tests only."""
    global _cache, _cached_at, _chain
    with _lock:
        _cache = None
        _cached_at = 0.0
        _chain = None
        _last_success.clear()


# ---------------------------------------------------------------------------
# Product-facing status
#
# /ai/health is the diagnostic view: every provider, its model, its
# latency, the error kind when it is down. This is the other one — what a
# person running a reconciliation needs to know about the model layer,
# which is whether it is answering and when it last did. No model ids, no
# probe payloads, and nothing that has been anywhere near a key.
# ---------------------------------------------------------------------------

#: The designed chain, read off the provider classes so this file does not
#: keep its own copy of the order build_chain() uses. Constructing a
#: provider needs a key; the class attribute does not.
PRIMARY_NAME: str = GeminiProvider.name
FALLBACK_NAME: str = GroqProvider.name

#: An error kind, said the way an operator would say it. Deliberately
#: derived from the taxonomy rather than from the provider's own message,
#: so this surface cannot carry text that came back off the wire.
_STATUS_DETAIL: dict[ProviderErrorKind, str] = {
    ProviderErrorKind.AUTH_FAILURE: "Credentials were rejected.",
    ProviderErrorKind.MODEL_NOT_FOUND: "The configured model is not available.",
    ProviderErrorKind.RATE_LIMIT: "Rate limited — requests are being throttled.",
    ProviderErrorKind.QUOTA_EXHAUSTED: "Quota for the current period is used up.",
    ProviderErrorKind.TIMEOUT: "Did not respond in time.",
    ProviderErrorKind.CONFIGURATION_ERROR: "Not configured on this deployment.",
    ProviderErrorKind.PROVIDER_ERROR: "Did not return a usable response.",
}


def _last_success_at(name: str, from_ledger: dict[str, str]) -> str | None:
    """The most recent moment this provider is known to have answered.

    Two independent observations, both of them things that happened: a
    health probe that returned in this process, and the newest decision
    in the ledger that a provider actually served. Whichever is later
    wins. No observation means `null` — a status line with no timestamp
    is honest, and an invented one is not.
    """
    candidates = [t for t in (_last_success.get(name), from_ledger.get(name)) if t]
    return max(candidates) if candidates else None


def _provider_status(name: str, health: dict[str, Any] | None, from_ledger: dict[str, str]) -> dict[str, Any]:
    if health is None:
        return {
            "name": name,
            "available": False,
            "configured": False,
            "detail": "Not configured on this deployment.",
            "last_success": _last_success_at(name, from_ledger),
        }
    kind = health.get("error_kind")
    detail = "Responding."
    if not health.get("available"):
        try:
            detail = _STATUS_DETAIL[ProviderErrorKind(kind)]
        except (ValueError, KeyError):
            detail = "Did not return a usable response."
    return {
        "name": name,
        "available": bool(health.get("available")),
        "configured": True,
        "detail": detail,
        "last_success": _last_success_at(name, from_ledger),
    }


async def _ai_status(refresh: bool = Query(False, description="Bypass the 60s cache and re-probe.")):
    """Provider availability, for the product's own status line."""
    report = _health_report(refresh=refresh)
    by_name = {p["provider"]: p for p in report.get("providers", [])}
    try:
        from_ledger = store.last_semantic_success_by_provider()
    except Exception:  # noqa: BLE001 — a status line must never 500 on a database hiccup
        from_ledger = {}

    primary = _provider_status(PRIMARY_NAME, by_name.get(PRIMARY_NAME), from_ledger)
    fallback = _provider_status(FALLBACK_NAME, by_name.get(FALLBACK_NAME), from_ledger)

    if semantic.ai_disabled():
        status = AI_UNAVAILABLE
        detail = "Running with the offline verifier; no model provider is called."
    elif primary["available"]:
        status = AI_AVAILABLE
        detail = "The primary provider is answering."
    elif fallback["available"]:
        status = AI_FALLBACK_ACTIVE
        detail = "The primary provider is not answering; the fallback is serving."
    else:
        status = AI_UNAVAILABLE
        detail = (
            "No provider is answering. Ambiguous records go to human review rather than "
            "being decided without evidence."
        )

    return {
        "status": status,
        "detail": detail,
        "primary": primary,
        "fallback": fallback,
        "checked_at": report.get("checked_at"),
        "cached": bool(report.get("cached")),
    }


async def _ai_health(refresh: bool = Query(False, description="Bypass the 60s cache and re-probe.")):
    return _health_report(refresh=refresh)


# Registered on both paths on purpose: the dev server proxies `/api/*` to
# the backend root (see frontend/vite.config.js), while the contract names
# the public path `/api/ai/health`. Both resolve to the same handler so
# neither the proxied nor the direct call is a 404.
router.add_api_route("/ai/health", _ai_health, methods=["GET"], name="ai_health")
router.add_api_route("/api/ai/health", _ai_health, methods=["GET"], name="ai_health_prefixed")
router.add_api_route("/ai/status", _ai_status, methods=["GET"], name="ai_status")
router.add_api_route("/api/ai/status", _ai_status, methods=["GET"], name="ai_status_prefixed")
