"""
LLM provider layer: two concrete providers, a precise error taxonomy, and
a fallback chain.

The point of this module is the taxonomy. A reconciliation run that
degrades has to be able to say *why* — "the key is wrong" and "we are out
of quota for the day" and "the model name no longer exists" are three
different operational problems with three different fixes, and collapsing
them into a single PROVIDER_ERROR turns an operator's five-minute fix
into an afternoon of guessing. So every failure that can be distinguished
from the wire is distinguished here, once, and every layer above reads
the same enum.

Two hard rules, both enforced rather than documented:
- A ProviderError.detail never contains an API key. Provider SDKs echo
  request context into exception strings; anything propagated through
  this module is scrubbed against every secret-shaped environment value
  before it becomes a detail string.
- A missing key is not an error. It means that provider is simply absent
  from the chain — the system is designed to run with one provider, two,
  or none (semantic.py falls back to the offline heuristic).
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol

import httpx

# ---------------------------------------------------------------------------
# Taxonomy
# ---------------------------------------------------------------------------


class ProviderErrorKind(str, Enum):
    """Why a provider call failed, at the granularity an operator can act on."""

    AUTH_FAILURE = "AUTH_FAILURE"
    MODEL_NOT_FOUND = "MODEL_NOT_FOUND"
    RATE_LIMIT = "RATE_LIMIT"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    TIMEOUT = "TIMEOUT"
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
    PROVIDER_ERROR = "PROVIDER_ERROR"


#: Kinds that mean "this provider cannot serve right now, try the next one".
_TRANSIENT_KINDS = frozenset({
    ProviderErrorKind.RATE_LIMIT,
    ProviderErrorKind.QUOTA_EXHAUSTED,
    ProviderErrorKind.TIMEOUT,
    ProviderErrorKind.PROVIDER_ERROR,
    ProviderErrorKind.MODEL_NOT_FOUND,
})

#: Kinds that mean "somebody has to fix the deployment". Still falls
#: through to the next provider — a misconfigured primary must not take
#: the run down — but it is logged as a configuration problem rather than
#: quietly absorbed as noise.
_CONFIG_KINDS = frozenset({
    ProviderErrorKind.AUTH_FAILURE,
    ProviderErrorKind.CONFIGURATION_ERROR,
})


class ProviderError(Exception):
    """A provider call failed. `detail` is safe to log and to show a user."""

    def __init__(self, kind: ProviderErrorKind, provider: str, detail: str) -> None:
        self.kind = kind
        self.provider = provider
        self.detail = scrub_secrets(detail)
        super().__init__(f"[{provider}] {kind.value}: {self.detail}")


@dataclass
class ProviderHealth:
    provider: str
    available: bool
    model: str | None
    latency_ms: float | None
    error_kind: ProviderErrorKind | None
    detail: str


class LLMProvider(Protocol):
    name: str
    model: str

    def health(self) -> ProviderHealth: ...

    def complete_json(
        self, *, system: str, user: str, schema: dict, timeout_s: float = 30.0
    ) -> dict: ...


# ---------------------------------------------------------------------------
# Secret scrubbing
# ---------------------------------------------------------------------------

_SECRET_ENV_PATTERN = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL)", re.IGNORECASE)

#: Shapes that are unmistakably credentials even if they never passed
#: through this process's environment (e.g. a key echoed back by a proxy).
_SECRET_SHAPES = (
    re.compile(r"AIza[0-9A-Za-z_\-]{20,}"),        # Google API keys
    re.compile(r"gsk_[0-9A-Za-z]{20,}"),            # Groq API keys
    re.compile(r"sk-[0-9A-Za-z_\-]{20,}"),          # OpenAI-shaped keys
    re.compile(r"(?i)bearer\s+[0-9A-Za-z._\-]{20,}"),
)

REDACTED = "<redacted>"


def _known_secrets() -> list[str]:
    """Every secret-shaped value currently in the environment.

    Read live rather than cached: tests and the health CLI mutate the
    environment, and a stale snapshot is how a key leaks.
    """
    out: list[str] = []
    for name, value in os.environ.items():
        if not value or len(value) < 8:
            continue
        if _SECRET_ENV_PATTERN.search(name):
            out.append(value)
    # Longest first, so a key that contains another key's prefix is
    # replaced whole rather than leaving a tail behind.
    return sorted(set(out), key=len, reverse=True)


def scrub_secrets(text: str, extra: list[str] | None = None) -> str:
    """Replace anything credential-shaped with a redaction marker.

    Defensive on purpose: provider SDKs put request context (sometimes
    including the URL, sometimes headers) into exception strings, and this
    is the last line before that text becomes a log entry or an API
    response.
    """
    if not text:
        return text
    for secret in list(extra or []) + _known_secrets():
        if secret and len(secret) >= 8 and secret in text:
            text = text.replace(secret, REDACTED)
    for pattern in _SECRET_SHAPES:
        text = pattern.sub(REDACTED, text)
    return text


# ---------------------------------------------------------------------------
# Shared classification helpers
# ---------------------------------------------------------------------------

_AUTH_HINTS = (
    "api key not valid", "api_key_invalid", "invalid api key", "invalid_api_key",
    "unauthorized", "unauthenticated", "permission denied", "permission_denied",
    "invalid authentication", "authentication_error", "forbidden",
)
_NOT_FOUND_HINTS = (
    "model not found", "does not exist", "model_not_found", "not found for api version",
    "is not found", "no such model", "decommissioned",
)
#: A 429 means "too many"; these say the "too many" is a hard ceiling for
#: the billing period rather than a burst you can wait out.
_QUOTA_HINTS = (
    "per day", "perday", "per-day", "daily", "quota exceeded", "out of quota",
    "insufficient_quota", "insufficient quota", "billing", "credit", "credits",
    "exceeded your current quota", "free_tier", "requests per day", "tokens per day",
    "tpd", "rpd", "upgrade",
)
_TIMEOUT_HINTS = ("deadline exceeded", "deadline_exceeded", "timed out", "timeout")


def _contains(haystack: str, needles: tuple[str, ...]) -> bool:
    low = haystack.lower()
    return any(n in low for n in needles)


def _classify_429(blob: str) -> ProviderErrorKind:
    """RATE_LIMIT unless the payload says the ceiling is daily/billing."""
    return ProviderErrorKind.QUOTA_EXHAUSTED if _contains(blob, _QUOTA_HINTS) else ProviderErrorKind.RATE_LIMIT


def _classify_status(status: int, blob: str) -> ProviderErrorKind:
    """Map an HTTP status plus the response body to a kind."""
    if status in (401, 403):
        return ProviderErrorKind.AUTH_FAILURE
    if status == 404:
        return ProviderErrorKind.MODEL_NOT_FOUND
    if status == 429:
        return _classify_429(blob)
    if status == 400 and _contains(blob, _NOT_FOUND_HINTS):
        # Both providers can answer "that model does not exist" with a 400.
        return ProviderErrorKind.MODEL_NOT_FOUND
    if status == 408 or (status == 504 and _contains(blob, _TIMEOUT_HINTS)):
        return ProviderErrorKind.TIMEOUT
    if _contains(blob, _AUTH_HINTS):
        return ProviderErrorKind.AUTH_FAILURE
    if _contains(blob, _NOT_FOUND_HINTS):
        return ProviderErrorKind.MODEL_NOT_FOUND
    return ProviderErrorKind.PROVIDER_ERROR


def retry_delay_seconds(exc: Exception, default: float, cap: float = 5.0) -> float:
    """The delay the provider actually asked for, bounded.

    Google returns a RetryInfo on a 429 saying how long to wait. Honouring
    it is right; honouring it unboundedly is not — a 60s retryDelay
    multiplied across a batch turns a degraded run into a hung one, and
    the chain has a second provider sitting right there. So the wait is
    capped and we fall through instead.
    """
    value = default
    try:
        details = exc.details.get("error", {}).get("details", [])  # type: ignore[attr-defined]
        for d in details:
            if d.get("@type", "").endswith("RetryInfo"):
                value = float(str(d.get("retryDelay", "")).rstrip("s") or default)
                break
    except Exception:
        value = default
    return max(0.0, min(value, cap))


def _parse_json_payload(provider: str, text: str | None) -> dict:
    """Model output must be a JSON object or it is a provider failure.

    Deliberately not lenient. A half-parsed verdict is worse than no
    verdict: the caller routes a missing verdict to human review, but a
    silently coerced one becomes a reconciliation decision.
    """
    if not text or not text.strip():
        raise ProviderError(ProviderErrorKind.PROVIDER_ERROR, provider, "model returned an empty response")
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError) as exc:
        raise ProviderError(
            ProviderErrorKind.PROVIDER_ERROR, provider,
            f"model returned unparseable JSON ({exc}); first 200 chars: {text[:200]!r}",
        ) from exc
    if not isinstance(parsed, dict):
        raise ProviderError(
            ProviderErrorKind.PROVIDER_ERROR, provider,
            f"model returned a {type(parsed).__name__}, expected a JSON object",
        )
    return parsed


_PROBE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


def _probe(provider: LLMProvider, timeout_s: float = 12.0) -> ProviderHealth:
    """One minimal structured call — the only honest way to say 'available'."""
    started = time.perf_counter()
    try:
        payload = provider.complete_json(
            system="You are a health probe. Answer with JSON only.",
            user='Return exactly {"ok": true}',
            schema=_PROBE_SCHEMA,
            timeout_s=timeout_s,
        )
    except ProviderError as exc:
        return ProviderHealth(
            provider=provider.name, available=False, model=provider.model,
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
            error_kind=exc.kind, detail=exc.detail,
        )
    except Exception as exc:  # noqa: BLE001 — a health probe must never raise
        return ProviderHealth(
            provider=provider.name, available=False, model=provider.model,
            latency_ms=round((time.perf_counter() - started) * 1000, 1),
            error_kind=ProviderErrorKind.PROVIDER_ERROR,
            detail=scrub_secrets(f"{type(exc).__name__}: {exc}"),
        )
    latency = round((time.perf_counter() - started) * 1000, 1)
    return ProviderHealth(
        provider=provider.name, available=True, model=provider.model,
        latency_ms=latency, error_kind=None,
        detail=f"structured probe returned {json.dumps(payload)[:120]}",
    )


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------


def _strip_unsupported(schema: Any) -> Any:
    """Gemini's response schema is a JSON-Schema subset.

    `additionalProperties` and `strict` are OpenAI-side vocabulary; Gemini
    rejects the request rather than ignoring them, so they are removed for
    that provider only. The schema is otherwise passed through unchanged.
    """
    if isinstance(schema, dict):
        return {
            k: _strip_unsupported(v)
            for k, v in schema.items()
            if k not in ("additionalProperties", "strict", "$schema")
        }
    if isinstance(schema, list):
        return [_strip_unsupported(v) for v in schema]
    return schema


class GeminiProvider:
    """Google Gemini via the google-genai SDK."""

    name = "gemini"

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        key = api_key if api_key is not None else os.environ.get("GEMINI_API_KEY")
        if not key:
            raise ProviderError(
                ProviderErrorKind.CONFIGURATION_ERROR, self.name,
                "GEMINI_API_KEY is not set",
            )
        self._api_key = key
        self.model = model or os.environ.get("GEMINI_MODEL") or "gemini-3.5-flash-lite"
        try:
            from google import genai
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(
                ProviderErrorKind.CONFIGURATION_ERROR, self.name,
                f"google-genai is not importable: {type(exc).__name__}: {exc}",
            ) from exc
        try:
            self._client = genai.Client(api_key=key)
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(
                ProviderErrorKind.CONFIGURATION_ERROR, self.name,
                scrub_secrets(f"could not construct the Gemini client: {type(exc).__name__}: {exc}", [key]),
            ) from exc

    # -- classification ----------------------------------------------------

    def _classify(self, exc: Exception) -> ProviderErrorKind:
        code = getattr(exc, "code", None)
        status = str(getattr(exc, "status", "") or "")
        message = str(getattr(exc, "message", "") or "")
        details = getattr(exc, "details", None)
        try:
            details_blob = json.dumps(details) if details is not None else ""
        except (TypeError, ValueError):
            details_blob = str(details)
        blob = f"{status} {message} {details_blob} {exc}"

        if status in ("UNAUTHENTICATED", "PERMISSION_DENIED"):
            return ProviderErrorKind.AUTH_FAILURE
        if status == "NOT_FOUND":
            return ProviderErrorKind.MODEL_NOT_FOUND
        if status == "DEADLINE_EXCEEDED":
            return ProviderErrorKind.TIMEOUT
        if status == "RESOURCE_EXHAUSTED" or code == 429:
            # RESOURCE_EXHAUSTED covers both a per-minute burst limit and a
            # blown daily allowance. The difference lives in the QuotaFailure
            # violations, so inspect them rather than guessing.
            return self._classify_resource_exhausted(details, blob)
        if isinstance(code, int):
            return _classify_status(code, blob)
        if _contains(blob, _TIMEOUT_HINTS):
            return ProviderErrorKind.TIMEOUT
        if _contains(blob, _AUTH_HINTS):
            return ProviderErrorKind.AUTH_FAILURE
        if _contains(blob, _NOT_FOUND_HINTS):
            return ProviderErrorKind.MODEL_NOT_FOUND
        return ProviderErrorKind.PROVIDER_ERROR

    @staticmethod
    def _classify_resource_exhausted(details: Any, blob: str) -> ProviderErrorKind:
        violations: list[dict] = []
        try:
            for d in details.get("error", {}).get("details", []):
                if d.get("@type", "").endswith("QuotaFailure"):
                    violations.extend(d.get("violations", []) or [])
        except Exception:  # noqa: BLE001 — shape is provider-controlled
            violations = []
        for v in violations:
            metric = f"{v.get('quotaId', '')} {v.get('quotaMetric', '')} {v.get('quotaDimensions', '')}"
            if _contains(metric, ("perday", "per_day", "per day", "daily")):
                return ProviderErrorKind.QUOTA_EXHAUSTED
        return _classify_429(blob)

    def _wrap(self, exc: Exception) -> ProviderError:
        kind = self._classify(exc)
        message = str(getattr(exc, "message", "") or "") or str(exc)
        return ProviderError(kind, self.name, scrub_secrets(
            f"{type(exc).__name__}: {message}", [self._api_key]
        ))

    # -- calls -------------------------------------------------------------

    def complete_json(self, *, system: str, user: str, schema: dict, timeout_s: float = 30.0) -> dict:
        from google.genai import errors as genai_errors
        from google.genai import types

        config = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_json_schema=_strip_unsupported(schema),
            temperature=0.0,
            http_options=types.HttpOptions(timeout=int(timeout_s * 1000)),
            # No tools are ever passed, so automatic function calling has
            # nothing to do but emit a warning on every single call.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

        last: Exception | None = None
        for attempt in range(2):
            try:
                response = self._client.models.generate_content(
                    model=self.model, contents=user, config=config
                )
                return _parse_json_payload(self.name, getattr(response, "text", None))
            except ProviderError:
                raise
            except genai_errors.APIError as exc:
                last = exc
                kind = self._classify(exc)
                # Exactly one retry, and only for a burst rate limit. A blown
                # daily quota will not clear in five seconds, and Groq is
                # already configured behind us.
                if kind is ProviderErrorKind.RATE_LIMIT and attempt == 0:
                    time.sleep(retry_delay_seconds(exc, default=1.0, cap=5.0))
                    continue
                raise self._wrap(exc) from exc
            except httpx.TimeoutException as exc:
                raise ProviderError(
                    ProviderErrorKind.TIMEOUT, self.name,
                    f"request timed out after {timeout_s}s ({type(exc).__name__})",
                ) from exc
            except Exception as exc:  # noqa: BLE001
                raise self._wrap(exc) from exc
        raise self._wrap(last) from last  # pragma: no cover — loop always returns or raises

    def health(self) -> ProviderHealth:
        return _probe(self)


# ---------------------------------------------------------------------------
# Groq
# ---------------------------------------------------------------------------

GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
GROQ_DEFAULT_MODEL = "openai/gpt-oss-120b"


class GroqProvider:
    """Groq's OpenAI-compatible endpoint, over plain httpx.

    No vendor SDK on purpose: this is one POST with one response shape,
    and httpx is already a dependency. A second SDK would be a second
    thing to keep current for no behaviour we do not have here.
    """

    name = "groq"

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        key = api_key if api_key is not None else os.environ.get("GROQ_API_KEY")
        if not key:
            raise ProviderError(
                ProviderErrorKind.CONFIGURATION_ERROR, self.name,
                "GROQ_API_KEY is not set",
            )
        self._api_key = key.strip()
        self.model = model or os.environ.get("GROQ_MODEL") or GROQ_DEFAULT_MODEL

    def _wrap(self, kind: ProviderErrorKind, detail: str) -> ProviderError:
        return ProviderError(kind, self.name, scrub_secrets(detail, [self._api_key]))

    def complete_json(self, *, system: str, user: str, schema: dict, timeout_s: float = 30.0) -> dict:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.0,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "verdict", "schema": schema, "strict": True},
            },
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        try:
            response = httpx.post(GROQ_ENDPOINT, json=body, headers=headers, timeout=timeout_s)
        except httpx.TimeoutException as exc:
            raise self._wrap(
                ProviderErrorKind.TIMEOUT,
                f"request timed out after {timeout_s}s ({type(exc).__name__})",
            ) from exc
        except httpx.HTTPError as exc:
            raise self._wrap(
                ProviderErrorKind.PROVIDER_ERROR,
                f"transport failure: {type(exc).__name__}: {exc}",
            ) from exc

        if response.status_code >= 400:
            raise self._wrap(
                _classify_status(response.status_code, _safe_body(response)),
                f"HTTP {response.status_code}: {_safe_body(response)[:400]}",
            )

        try:
            envelope = response.json()
            content = envelope["choices"][0]["message"]["content"]
        except Exception as exc:  # noqa: BLE001 — any deviation is a provider failure
            raise self._wrap(
                ProviderErrorKind.PROVIDER_ERROR,
                f"unexpected response envelope ({type(exc).__name__}: {exc}): {_safe_body(response)[:200]}",
            ) from exc

        return _parse_json_payload(self.name, content)

    def health(self) -> ProviderHealth:
        return _probe(self)


def _safe_body(response: Any) -> str:
    try:
        return scrub_secrets(response.text or "")
    except Exception:  # noqa: BLE001
        return "<unreadable response body>"


# ---------------------------------------------------------------------------
# Chain
# ---------------------------------------------------------------------------

AI_AVAILABLE = "AI_AVAILABLE"
AI_FALLBACK_ACTIVE = "AI_FALLBACK_ACTIVE"
AI_UNAVAILABLE = "AI_UNAVAILABLE"


class FallbackChain:
    """Tries providers in order and records which one actually answered.

    Every failure kind falls through to the next provider — including
    AUTH_FAILURE and CONFIGURATION_ERROR, because a misconfigured primary
    is exactly the case where the secondary earns its keep. The
    difference is that those two are recorded as configuration problems
    rather than transient noise, so `config_problems` can surface them
    instead of letting a dead key look like weather.

    When everything fails the *last* error is raised: it describes the
    provider that was actually asked most recently, and callers upstream
    (matching.py) turn any raised exception into a PROVIDER_ERROR
    classification routed to human review.
    """

    def __init__(self, providers: list[LLMProvider]) -> None:
        self._providers = list(providers)
        # None = not yet observed. Only real call outcomes write here.
        self._observed: dict[str, bool | None] = {p.name: None for p in self._providers}
        self._last_served_by: str | None = None
        self.config_problems: list[tuple[str, ProviderErrorKind, str]] = []
        self._config_problem_counts: dict[tuple[str, ProviderErrorKind, str], int] = {}

    @property
    def providers(self) -> list[LLMProvider]:
        return list(self._providers)

    @property
    def last_served_by(self) -> str | None:
        return self._last_served_by

    def complete_json(
        self, *, system: str, user: str, schema: dict, timeout_s: float = 30.0
    ) -> tuple[dict, str]:
        if not self._providers:
            raise ProviderError(
                ProviderErrorKind.CONFIGURATION_ERROR, "chain",
                "no LLM provider is configured (set GEMINI_API_KEY and/or GROQ_API_KEY)",
            )

        last_error: ProviderError | None = None
        for provider in self._providers:
            try:
                payload = provider.complete_json(
                    system=system, user=user, schema=schema, timeout_s=timeout_s
                )
            except ProviderError as exc:
                self._observed[provider.name] = False
                last_error = exc
                if exc.kind in _CONFIG_KINDS:
                    self._note_config_problem(exc)
                continue
            except Exception as exc:  # noqa: BLE001 — a rogue SDK error is still a provider failure
                self._observed[provider.name] = False
                last_error = ProviderError(
                    ProviderErrorKind.PROVIDER_ERROR, provider.name,
                    scrub_secrets(f"{type(exc).__name__}: {exc}"),
                )
                continue

            self._observed[provider.name] = True
            self._last_served_by = provider.name
            return payload, provider.name

        assert last_error is not None  # loop body always sets it before falling out
        raise last_error

    def _note_config_problem(self, exc: ProviderError) -> None:
        """Record a deployment problem, and say it once.

        A dead key fails identically on every record. Logging it per call
        buried a 1,000-record evaluation under 204 copies of one line,
        which is not more information — it is less, because the run's real
        output scrolled away. The count is kept so the scale is still
        recoverable, and `config_problems` remains the structured answer.
        """
        import logging

        entry = (exc.provider, exc.kind, exc.detail)
        self._config_problem_counts[entry] = self._config_problem_counts.get(entry, 0) + 1
        if entry in self.config_problems:
            return
        self.config_problems.append(entry)
        logging.getLogger(__name__).error(
            "LLM provider %s is misconfigured (%s): %s — falling through to the next provider. "
            "This is logged once; see FallbackChain.config_problem_counts for how often it recurred.",
            exc.provider, exc.kind.value, exc.detail,
        )

    @property
    def config_problem_counts(self) -> dict[tuple[str, ProviderErrorKind, str], int]:
        """How many times each distinct configuration problem was hit."""
        return dict(self._config_problem_counts)

    def health(self) -> list[ProviderHealth]:
        report: list[ProviderHealth] = []
        for provider in self._providers:
            result = provider.health()
            self._observed[provider.name] = result.available
            if result.available:
                self._last_served_by = self._last_served_by or provider.name
            elif result.error_kind in _CONFIG_KINDS:
                self._note_config_problem(
                    ProviderError(result.error_kind, provider.name, result.detail)
                )
            report.append(result)
        return report

    @property
    def status(self) -> str:
        """Derived from observed call outcomes only.

        `None` means "configured, nothing observed to the contrary" and is
        treated as able to serve — the alternative is reporting a chain
        unavailable purely because nobody has called it yet, which is a
        guess in the more alarming direction.
        """
        if not self._providers:
            return AI_UNAVAILABLE
        primary = self._providers[0].name
        if self._observed.get(primary) is not False:
            return AI_AVAILABLE
        for provider in self._providers[1:]:
            if self._observed.get(provider.name) is not False:
                return AI_FALLBACK_ACTIVE
        return AI_UNAVAILABLE


def build_chain() -> FallbackChain:
    """Gemini primary, Groq secondary. A missing key means absent, not broken."""
    providers: list[LLMProvider] = []
    for factory in (GeminiProvider, GroqProvider):
        try:
            providers.append(factory())
        except ProviderError:
            # CONFIGURATION_ERROR from a missing key is the designed way to
            # say "not configured" — the provider is simply left out.
            continue
    return FallbackChain(providers)
