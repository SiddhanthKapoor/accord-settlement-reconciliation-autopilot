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
    AI_UNAVAILABLE,
    FallbackChain,
    ProviderHealth,
    build_chain,
    scrub_secrets,
)

router = APIRouter(tags=["ai"])

HEALTH_CACHE_TTL_SECONDS = 60.0

_lock = threading.Lock()
_chain: FallbackChain | None = None
_cache: dict[str, Any] | None = None
_cached_at: float = 0.0


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
        return {**report, "cached": False}


def _reset_cache_for_tests() -> None:
    """Drop the memoised chain and cached probe. Tests only."""
    global _cache, _cached_at, _chain
    with _lock:
        _cache = None
        _cached_at = 0.0
        _chain = None


async def _ai_health(refresh: bool = Query(False, description="Bypass the 60s cache and re-probe.")):
    return _health_report(refresh=refresh)


# Registered on both paths on purpose: the dev server proxies `/api/*` to
# the backend root (see frontend/vite.config.js), while the contract names
# the public path `/api/ai/health`. Both resolve to the same handler so
# neither the proxied nor the direct call is a 404.
router.add_api_route("/ai/health", _ai_health, methods=["GET"], name="ai_health")
router.add_api_route("/api/ai/health", _ai_health, methods=["GET"], name="ai_health_prefixed")
