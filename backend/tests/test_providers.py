"""
The provider layer's failure behaviour, pinned.

Everything here is offline and deterministic. No test in this file opens
a socket: the Gemini path is exercised through fake SDK exceptions and a
stubbed `generate_content`, the Groq path through a stubbed `httpx.post`,
and the chain through hand-written providers. A test suite that needs a
live API key to tell you whether your error handling works is a test
suite that stops working exactly when you need it.

What is worth pinning here is the taxonomy. Anyone can catch an exception
and call it PROVIDER_ERROR; the reason this layer exists is that
"the key is wrong", "the model was renamed", "you are being throttled"
and "you are out of quota until tomorrow" have four different fixes, and
the operator reading the status panel is the person who has to pick one.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
import pytest

from app.domain.models import (
    MatchClassification, MerchantRecord, PolicyConfig, RazorpaySettlementRecord,
    ReconciliationOutcome, ReconciliationRecord,
)
from app.engine import matching, providers, semantic
from app.engine.batch import process_batch
from app.engine.providers import (
    FallbackChain, GeminiProvider, GroqProvider, ProviderError, ProviderErrorKind,
    ProviderHealth, build_chain, scrub_secrets,
)
from app.engine.semantic import (
    CandidateComparison, ChainSemanticVerifier, HeuristicSemanticVerifier, RecordSide,
    SemanticVerdictResult,
)

NOW = datetime(2026, 3, 1, tzinfo=timezone.utc)

FAKE_GEMINI_KEY = "AIzaFAKEKEYFORTESTSONLY0000000000000000"
FAKE_GROQ_KEY = "gsk_FAKEKEYFORTESTSONLY000000000000000000"


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    """Every test declares the keys it wants. A developer's real .env must
    never change what these tests assert."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_MODEL", raising=False)
    monkeypatch.delenv("ACCORD_AI_DISABLED", raising=False)


class FakeProvider:
    """A provider that does exactly what the test tells it to."""

    def __init__(self, name, model="fake-model", *, error=None, payload=None):
        self.name, self.model = name, model
        self._error, self._payload = error, payload or {"relationship": "SAME", "confidence": 0.9, "reason": "ok"}
        self.calls = 0

    def complete_json(self, *, system, user, schema, timeout_s=30.0):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return dict(self._payload)

    def health(self):
        if self._error is not None:
            kind = self._error.kind if isinstance(self._error, ProviderError) else ProviderErrorKind.PROVIDER_ERROR
            return ProviderHealth(self.name, False, self.model, 1.0, kind, "down")
        return ProviderHealth(self.name, True, self.model, 1.0, None, "ok")


class FakeGeminiError(Exception):
    """Shaped like google.genai.errors.APIError: code / status / message / details."""

    def __init__(self, code, status=None, message="", details=None):
        self.code, self.status, self.message = code, status, message
        self.details = details if details is not None else {}
        super().__init__(f"{code} {status}. {self.details}")


class FakeHttpResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        return json.loads(self.text)


def gemini_provider(monkeypatch, model="gemini-3.5-flash-lite"):
    """A GeminiProvider whose SDK client is never actually used."""
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_GEMINI_KEY)
    provider = GeminiProvider(model=model)
    return provider


def groq_provider(monkeypatch, model=None):
    monkeypatch.setenv("GROQ_API_KEY", FAKE_GROQ_KEY)
    return GroqProvider(model=model)


def comparison(**overrides) -> CandidateComparison:
    defaults = dict(
        merchant=RecordSide("ORD-1", "Order ORD1 Premium Plan", 100000, NOW),
        candidate=RecordSide("ORD-2", "Settlement premium plan", 100000, NOW + timedelta(days=1)),
        amount_exact_match=True, amount_delta_minor=0, days_apart=1,
        shared_reference_core=False, text_similarity=0.4,
    )
    defaults.update(overrides)
    return CandidateComparison(**defaults)


# ---------------------------------------------------------------------------
# Error taxonomy — Gemini
# ---------------------------------------------------------------------------

QUOTA_FAILURE_DAILY = {
    "error": {
        "code": 429,
        "status": "RESOURCE_EXHAUSTED",
        "message": "You exceeded your current quota, please check your plan and billing details.",
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [{
                    "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
                    "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                }],
            },
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "36s"},
        ],
    }
}

QUOTA_FAILURE_PER_MINUTE = {
    "error": {
        "code": 429,
        "status": "RESOURCE_EXHAUSTED",
        "message": "Resource has been exhausted (e.g. check quota).",
        "details": [
            {
                "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [{
                    "quotaMetric": "generativelanguage.googleapis.com/generate_content_requests",
                    "quotaId": "GenerateRequestsPerMinutePerProjectPerModel",
                }],
            },
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "4s"},
        ],
    }
}


@pytest.mark.parametrize("exc,expected", [
    (FakeGeminiError(401, "UNAUTHENTICATED", "API key not valid. Please pass a valid API key."),
     ProviderErrorKind.AUTH_FAILURE),
    (FakeGeminiError(403, "PERMISSION_DENIED", "The caller does not have permission"),
     ProviderErrorKind.AUTH_FAILURE),
    (FakeGeminiError(404, "NOT_FOUND", "models/gemini-nope is not found for API version v1beta"),
     ProviderErrorKind.MODEL_NOT_FOUND),
    (FakeGeminiError(400, "INVALID_ARGUMENT", "Model gemini-nope does not exist"),
     ProviderErrorKind.MODEL_NOT_FOUND),
    (FakeGeminiError(429, "RESOURCE_EXHAUSTED", "Resource has been exhausted", QUOTA_FAILURE_PER_MINUTE),
     ProviderErrorKind.RATE_LIMIT),
    (FakeGeminiError(429, "RESOURCE_EXHAUSTED", "You exceeded your current quota", QUOTA_FAILURE_DAILY),
     ProviderErrorKind.QUOTA_EXHAUSTED),
    (FakeGeminiError(504, "DEADLINE_EXCEEDED", "Deadline exceeded"),
     ProviderErrorKind.TIMEOUT),
    (FakeGeminiError(500, "INTERNAL", "An internal error has occurred"),
     ProviderErrorKind.PROVIDER_ERROR),
    (FakeGeminiError(503, "UNAVAILABLE", "The model is overloaded. Please try again later."),
     ProviderErrorKind.PROVIDER_ERROR),
])
def test_gemini_error_kinds_are_distinguished(monkeypatch, exc, expected):
    assert gemini_provider(monkeypatch)._classify(exc) is expected


def test_gemini_daily_quota_is_not_reported_as_a_rate_limit(monkeypatch):
    """Both arrive as 429 RESOURCE_EXHAUSTED. Only one of them clears if you
    wait thirty seconds, so they must not be the same error to the operator."""
    p = gemini_provider(monkeypatch)
    minute = FakeGeminiError(429, "RESOURCE_EXHAUSTED", "exhausted", QUOTA_FAILURE_PER_MINUTE)
    daily = FakeGeminiError(429, "RESOURCE_EXHAUSTED", "exhausted", QUOTA_FAILURE_DAILY)
    assert p._classify(minute) is ProviderErrorKind.RATE_LIMIT
    assert p._classify(daily) is ProviderErrorKind.QUOTA_EXHAUSTED


def test_missing_gemini_key_is_a_configuration_error_not_a_crash():
    with pytest.raises(ProviderError) as exc:
        GeminiProvider()
    assert exc.value.kind is ProviderErrorKind.CONFIGURATION_ERROR


def test_gemini_retries_a_rate_limit_exactly_once_then_gives_up(monkeypatch):
    p = gemini_provider(monkeypatch)
    sleeps: list[float] = []
    monkeypatch.setattr(providers.time, "sleep", lambda s: sleeps.append(s))

    attempts = {"n": 0}

    def always_429(**kwargs):
        attempts["n"] += 1
        raise _as_genai_error(429, "RESOURCE_EXHAUSTED", "exhausted", QUOTA_FAILURE_PER_MINUTE)

    monkeypatch.setattr(p._client.models, "generate_content", always_429)
    with pytest.raises(ProviderError) as exc:
        p.complete_json(system="s", user="u", schema={"type": "object"})

    assert attempts["n"] == 2, "one retry, not a retry storm"
    assert exc.value.kind is ProviderErrorKind.RATE_LIMIT
    assert sleeps == [4.0]


def test_gemini_retry_sleep_is_capped_so_it_cannot_stall_a_batch(monkeypatch):
    """Google can ask for a 60s wait. Honouring that per record turns a
    degraded run into a hung one when there is a second provider right
    behind it — so the wait is bounded and we fall through instead."""
    long_wait = json.loads(json.dumps(QUOTA_FAILURE_PER_MINUTE))
    long_wait["error"]["details"][1]["retryDelay"] = "63s"
    exc = FakeGeminiError(429, "RESOURCE_EXHAUSTED", "exhausted", long_wait)
    assert providers.retry_delay_seconds(exc, default=1.0, cap=5.0) == 5.0


def test_gemini_unparseable_output_is_a_provider_error(monkeypatch):
    p = gemini_provider(monkeypatch)

    class Resp:
        text = "not json at all {{{"

    monkeypatch.setattr(p._client.models, "generate_content", lambda **kw: Resp())
    with pytest.raises(ProviderError) as exc:
        p.complete_json(system="s", user="u", schema={"type": "object"})
    assert exc.value.kind is ProviderErrorKind.PROVIDER_ERROR


def _as_genai_error(code, status, message, details):
    """Build a real google.genai APIError without touching the network."""
    from google.genai import errors as genai_errors

    payload = dict(details)
    payload.setdefault("error", {})
    payload["error"].setdefault("status", status)
    payload["error"].setdefault("message", message)
    return genai_errors.APIError(code, payload)


# ---------------------------------------------------------------------------
# Error taxonomy — Groq
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status,body,expected", [
    (401, {"error": {"message": "Invalid API Key", "code": "invalid_api_key"}}, ProviderErrorKind.AUTH_FAILURE),
    (403, {"error": {"message": "Forbidden"}}, ProviderErrorKind.AUTH_FAILURE),
    (404, {"error": {"message": "The model `nope` does not exist", "code": "model_not_found"}},
     ProviderErrorKind.MODEL_NOT_FOUND),
    (429, {"error": {"message": "Rate limit reached: 30 requests per minute", "code": "rate_limit_exceeded"}},
     ProviderErrorKind.RATE_LIMIT),
    (429, {"error": {"message": "Rate limit reached for model on tokens per day (TPD): Limit 100000. "
                                "Need more? Upgrade to Dev Tier", "code": "rate_limit_exceeded"}},
     ProviderErrorKind.QUOTA_EXHAUSTED),
    (500, {"error": {"message": "Internal Server Error"}}, ProviderErrorKind.PROVIDER_ERROR),
    (503, {"error": {"message": "Service Unavailable"}}, ProviderErrorKind.PROVIDER_ERROR),
])
def test_groq_error_kinds_are_distinguished(monkeypatch, status, body, expected):
    p = groq_provider(monkeypatch)
    monkeypatch.setattr(providers.httpx, "post", lambda *a, **kw: FakeHttpResponse(status, body))
    with pytest.raises(ProviderError) as exc:
        p.complete_json(system="s", user="u", schema={"type": "object"})
    assert exc.value.kind is expected
    assert exc.value.provider == "groq"


def test_groq_timeout_maps_to_timeout(monkeypatch):
    p = groq_provider(monkeypatch)

    def boom(*a, **kw):
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(providers.httpx, "post", boom)
    with pytest.raises(ProviderError) as exc:
        p.complete_json(system="s", user="u", schema={"type": "object"}, timeout_s=1.0)
    assert exc.value.kind is ProviderErrorKind.TIMEOUT


def test_groq_transport_failure_is_a_provider_error(monkeypatch):
    p = groq_provider(monkeypatch)

    def boom(*a, **kw):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(providers.httpx, "post", boom)
    with pytest.raises(ProviderError) as exc:
        p.complete_json(system="s", user="u", schema={"type": "object"})
    assert exc.value.kind is ProviderErrorKind.PROVIDER_ERROR


def test_groq_malformed_body_is_a_provider_error(monkeypatch):
    """A 200 whose content is not JSON is still a failure. Coercing it into
    a verdict would turn provider noise into a reconciliation decision."""
    p = groq_provider(monkeypatch)
    body = {"choices": [{"message": {"content": "sure! here you go: SAME"}}]}
    monkeypatch.setattr(providers.httpx, "post", lambda *a, **kw: FakeHttpResponse(200, body))
    with pytest.raises(ProviderError) as exc:
        p.complete_json(system="s", user="u", schema={"type": "object"})
    assert exc.value.kind is ProviderErrorKind.PROVIDER_ERROR


def test_groq_sends_strict_json_schema_structured_output(monkeypatch):
    p = groq_provider(monkeypatch)
    captured = {}

    def capture(url, *, json=None, headers=None, timeout=None):
        captured["url"], captured["body"], captured["headers"] = url, json, headers
        return FakeHttpResponse(200, {"choices": [{"message": {"content": '{"ok": true}'}}]})

    monkeypatch.setattr(providers.httpx, "post", capture)
    assert p.complete_json(system="s", user="u", schema={"type": "object"}) == {"ok": True}

    assert captured["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert captured["body"]["model"] == "openai/gpt-oss-120b"
    rf = captured["body"]["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["name"] == "verdict"
    assert rf["json_schema"]["strict"] is True
    assert captured["headers"]["Authorization"].startswith("Bearer ")


def test_missing_groq_key_is_a_configuration_error():
    with pytest.raises(ProviderError) as exc:
        GroqProvider()
    assert exc.value.kind is ProviderErrorKind.CONFIGURATION_ERROR


# ---------------------------------------------------------------------------
# Key redaction
# ---------------------------------------------------------------------------

def test_a_key_echoed_in_a_provider_exception_never_reaches_the_detail(monkeypatch):
    """Provider SDKs put request context into exception strings. This is the
    last place that text can be stopped before it becomes a log line, an
    audit payload or an HTTP response body."""
    p = gemini_provider(monkeypatch)

    def leaky(**kwargs):
        raise FakeGeminiError(
            400, "INVALID_ARGUMENT",
            f"request to ?key={FAKE_GEMINI_KEY} failed",
        )

    monkeypatch.setattr(p._client.models, "generate_content", leaky)
    with pytest.raises(ProviderError) as exc:
        p.complete_json(system="s", user="u", schema={"type": "object"})

    assert FAKE_GEMINI_KEY not in exc.value.detail
    assert FAKE_GEMINI_KEY not in str(exc.value)
    assert providers.REDACTED in exc.value.detail


def test_groq_key_in_a_response_body_is_redacted(monkeypatch):
    p = groq_provider(monkeypatch)
    body = {"error": {"message": f"Invalid API Key: {FAKE_GROQ_KEY}"}}
    monkeypatch.setattr(providers.httpx, "post", lambda *a, **kw: FakeHttpResponse(401, body))
    with pytest.raises(ProviderError) as exc:
        p.complete_json(system="s", user="u", schema={"type": "object"})
    assert FAKE_GROQ_KEY not in exc.value.detail
    assert exc.value.kind is ProviderErrorKind.AUTH_FAILURE


def test_scrubbing_covers_keys_that_never_touched_the_environment():
    """Belt and braces: a credential-shaped token is redacted on shape alone,
    so a key echoed by a proxy or copied into a prompt is still caught."""
    leaked = "AIzaSyNOTREALBUTLOOKSLIKEAKEY000000000"
    assert leaked not in scrub_secrets(f"boom: {leaked}")
    assert scrub_secrets("nothing sensitive here") == "nothing sensitive here"


def test_a_configured_key_is_scrubbed_out_of_arbitrary_text(monkeypatch):
    monkeypatch.setenv("SOME_OTHER_API_KEY", "plain-looking-but-secret-value")
    assert "plain-looking-but-secret-value" not in scrub_secrets(
        "provider said: plain-looking-but-secret-value is bad"
    )


# ---------------------------------------------------------------------------
# FallbackChain
# ---------------------------------------------------------------------------

def test_rate_limited_gemini_falls_through_to_groq(monkeypatch):
    gem = FakeProvider("gemini", "gemini-3.5-flash-lite",
                       error=ProviderError(ProviderErrorKind.RATE_LIMIT, "gemini", "429"))
    groq = FakeProvider("groq", "openai/gpt-oss-120b", payload={"relationship": "SAME", "confidence": 0.8, "reason": "r"})
    chain = FallbackChain([gem, groq])

    payload, served_by = chain.complete_json(system="s", user="u", schema={})
    assert served_by == "groq"
    assert payload["confidence"] == 0.8
    assert chain.status == "AI_FALLBACK_ACTIVE"
    assert chain.last_served_by == "groq"


@pytest.mark.parametrize("kind", [
    ProviderErrorKind.RATE_LIMIT, ProviderErrorKind.QUOTA_EXHAUSTED, ProviderErrorKind.TIMEOUT,
    ProviderErrorKind.PROVIDER_ERROR, ProviderErrorKind.MODEL_NOT_FOUND,
])
def test_every_transient_kind_falls_through(kind):
    gem = FakeProvider("gemini", error=ProviderError(kind, "gemini", "down"))
    groq = FakeProvider("groq")
    payload, served_by = FallbackChain([gem, groq]).complete_json(system="s", user="u", schema={})
    assert served_by == "groq" and payload["relationship"] == "SAME"


def test_auth_failure_on_the_primary_still_falls_through_to_the_secondary(caplog):
    """A dead key on the primary is exactly the situation the secondary
    exists for. It falls through — but it is recorded as a configuration
    problem, because nothing about it will fix itself."""
    gem = FakeProvider("gemini", error=ProviderError(ProviderErrorKind.AUTH_FAILURE, "gemini", "API key not valid"))
    groq = FakeProvider("groq")
    chain = FallbackChain([gem, groq])

    with caplog.at_level("ERROR"):
        _, served_by = chain.complete_json(system="s", user="u", schema={})

    assert served_by == "groq"
    assert chain.config_problems == [("gemini", ProviderErrorKind.AUTH_FAILURE, "API key not valid")]
    assert "misconfigured" in caplog.text


def test_configuration_error_on_the_primary_also_falls_through():
    gem = FakeProvider("gemini", error=ProviderError(ProviderErrorKind.CONFIGURATION_ERROR, "gemini", "bad config"))
    groq = FakeProvider("groq")
    chain = FallbackChain([gem, groq])
    _, served_by = chain.complete_json(system="s", user="u", schema={})
    assert served_by == "groq"
    assert chain.config_problems[0][1] is ProviderErrorKind.CONFIGURATION_ERROR


def test_groq_serves_as_the_primary_when_gemini_has_no_key(monkeypatch):
    """No GEMINI_API_KEY means Gemini is absent from the chain, not broken —
    and a Groq-only chain is a healthy chain, not a degraded one."""
    monkeypatch.setenv("GROQ_API_KEY", FAKE_GROQ_KEY)
    chain = build_chain()
    assert [p.name for p in chain.providers] == ["groq"]

    monkeypatch.setattr(providers.httpx, "post", lambda *a, **kw: FakeHttpResponse(
        200, {"choices": [{"message": {"content": '{"relationship":"SAME","confidence":0.7,"reason":"x"}'}}]}))
    payload, served_by = chain.complete_json(system="s", user="u", schema={})
    assert served_by == "groq"
    assert payload["relationship"] == "SAME"
    assert chain.status == "AI_AVAILABLE"


def test_build_chain_orders_gemini_first_then_groq(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_GEMINI_KEY)
    monkeypatch.setenv("GROQ_API_KEY", FAKE_GROQ_KEY)
    assert [p.name for p in build_chain().providers] == ["gemini", "groq"]


def test_build_chain_with_no_keys_is_empty_and_unavailable():
    chain = build_chain()
    assert chain.providers == []
    assert chain.status == "AI_UNAVAILABLE"
    with pytest.raises(ProviderError) as exc:
        chain.complete_json(system="s", user="u", schema={})
    assert exc.value.kind is ProviderErrorKind.CONFIGURATION_ERROR


def test_when_every_provider_fails_the_last_error_is_raised():
    gem = FakeProvider("gemini", error=ProviderError(ProviderErrorKind.RATE_LIMIT, "gemini", "429"))
    groq = FakeProvider("groq", error=ProviderError(ProviderErrorKind.QUOTA_EXHAUSTED, "groq", "out of tokens"))
    chain = FallbackChain([gem, groq])

    with pytest.raises(ProviderError) as exc:
        chain.complete_json(system="s", user="u", schema={})

    assert exc.value.kind is ProviderErrorKind.QUOTA_EXHAUSTED
    assert exc.value.provider == "groq"
    assert chain.status == "AI_UNAVAILABLE"


def test_status_tracks_observed_outcomes_not_configuration():
    gem = FakeProvider("gemini")
    groq = FakeProvider("groq")
    chain = FallbackChain([gem, groq])

    chain.complete_json(system="s", user="u", schema={})
    assert chain.status == "AI_AVAILABLE"

    gem._error = ProviderError(ProviderErrorKind.TIMEOUT, "gemini", "slow")
    chain.complete_json(system="s", user="u", schema={})
    assert chain.status == "AI_FALLBACK_ACTIVE"

    groq._error = ProviderError(ProviderErrorKind.TIMEOUT, "groq", "slow")
    with pytest.raises(ProviderError):
        chain.complete_json(system="s", user="u", schema={})
    assert chain.status == "AI_UNAVAILABLE"

    gem._error = None
    chain.complete_json(system="s", user="u", schema={})
    assert chain.status == "AI_AVAILABLE"


def test_chain_health_reports_every_provider():
    gem = FakeProvider("gemini", error=ProviderError(ProviderErrorKind.AUTH_FAILURE, "gemini", "bad key"))
    chain = FallbackChain([gem, FakeProvider("groq")])
    report = chain.health()
    assert [h.provider for h in report] == ["gemini", "groq"]
    assert [h.available for h in report] == [False, True]
    assert report[0].error_kind is ProviderErrorKind.AUTH_FAILURE
    assert chain.status == "AI_FALLBACK_ACTIVE"


# ---------------------------------------------------------------------------
# ChainSemanticVerifier
# ---------------------------------------------------------------------------

def test_backend_names_the_provider_that_actually_answered():
    """Downstream reporting — the UI, the audit trail, the evaluation
    report — reads `backend`. If a fallback run is labeled as a primary
    run, every number attributed to the primary model is wrong."""
    gem = FakeProvider("gemini", "gemini-3.5-flash-lite",
                       error=ProviderError(ProviderErrorKind.RATE_LIMIT, "gemini", "429"))
    groq = FakeProvider("groq", "openai/gpt-oss-120b",
                        payload={"relationship": "SAME", "confidence": 0.81, "reason": "same customer"})
    result = ChainSemanticVerifier(FallbackChain([gem, groq])).compare(comparison())

    assert result.backend == "groq:openai/gpt-oss-120b"
    assert result.verdict == "SAME"
    assert result.confidence == 0.81


def test_backend_names_gemini_when_gemini_answers():
    gem = FakeProvider("gemini", "gemini-3.5-flash-lite",
                       payload={"relationship": "AMBIGUOUS", "confidence": 0.4, "reason": "unclear"})
    result = ChainSemanticVerifier(FallbackChain([gem, FakeProvider("groq")])).compare(comparison())
    assert result.backend == "gemini:gemini-3.5-flash-lite"
    assert result.verdict == "AMBIGUOUS"


def test_the_prompt_preamble_is_sent_verbatim_and_carries_the_deterministic_signals():
    """The preamble is evaluated text, not prose — a reworded version is a
    different classifier with different measured accuracy."""
    captured = {}

    class Recorder(FakeProvider):
        def complete_json(self, *, system, user, schema, timeout_s=30.0):
            captured["system"], captured["user"], captured["schema"] = system, user, schema
            return {"relationship": "SAME", "confidence": 0.9, "reason": "ok"}

    ChainSemanticVerifier(FallbackChain([Recorder("gemini")])).compare(comparison())

    assert captured["system"] == semantic._PROMPT_PREAMBLE
    payload = json.loads(captured["user"].split("Input:\n", 1)[1])
    assert set(payload) == {"merchant", "candidate", "deterministic_signals"}
    assert payload["deterministic_signals"]["amount_exact_match"] is True
    assert captured["schema"]["required"] == ["relationship", "confidence", "reason"]


def test_a_verdict_missing_required_fields_is_a_provider_error_not_a_guess():
    bad = FakeProvider("gemini", payload={"confidence": 0.9})
    with pytest.raises(ProviderError) as exc:
        ChainSemanticVerifier(FallbackChain([bad])).compare(comparison())
    assert exc.value.kind is ProviderErrorKind.PROVIDER_ERROR


def test_total_chain_failure_propagates_rather_than_fabricating_a_verdict():
    gem = FakeProvider("gemini", error=ProviderError(ProviderErrorKind.QUOTA_EXHAUSTED, "gemini", "daily quota"))
    groq = FakeProvider("groq", error=ProviderError(ProviderErrorKind.AUTH_FAILURE, "groq", "bad key"))
    with pytest.raises(ProviderError):
        ChainSemanticVerifier(FallbackChain([gem, groq])).compare(comparison())


# ---------------------------------------------------------------------------
# get_semantic_verifier / offline switch
# ---------------------------------------------------------------------------

def test_no_keys_means_the_heuristic_verifier():
    assert isinstance(semantic.get_semantic_verifier(), HeuristicSemanticVerifier)


def test_any_key_gives_the_chain_verifier(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", FAKE_GROQ_KEY)
    v = semantic.get_semantic_verifier()
    assert isinstance(v, ChainSemanticVerifier)
    assert [p.name for p in v.chain.providers] == ["groq"]


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_accord_ai_disabled_forces_the_heuristic(monkeypatch, value):
    """The evaluation harness needs a mode that provably cannot reach the
    network, keys present or not — an accuracy figure produced by a run
    that silently called an API is not reproducible."""
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_GEMINI_KEY)
    monkeypatch.setenv("GROQ_API_KEY", FAKE_GROQ_KEY)
    monkeypatch.setenv("ACCORD_AI_DISABLED", value)
    assert isinstance(semantic.get_semantic_verifier(), HeuristicSemanticVerifier)


def test_accord_ai_disabled_unset_or_falsy_does_not_disable(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", FAKE_GROQ_KEY)
    monkeypatch.setenv("ACCORD_AI_DISABLED", "0")
    assert isinstance(semantic.get_semantic_verifier(), ChainSemanticVerifier)


def test_gemini_semantic_verifier_alias_is_a_gemini_only_chain(monkeypatch):
    """Kept so the benchmark scripts that pin one backend keep importing."""
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_GEMINI_KEY)
    monkeypatch.setenv("GROQ_API_KEY", FAKE_GROQ_KEY)
    v = semantic.GeminiSemanticVerifier()
    assert isinstance(v, ChainSemanticVerifier)
    assert [p.name for p in v.chain.providers] == ["gemini"]


# ---------------------------------------------------------------------------
# Engine-level: a dead provider is a visible outcome, never a silent match
# ---------------------------------------------------------------------------

def _merchant(**overrides) -> MerchantRecord:
    defaults = dict(
        order_id="ORD1", reference_id="NO-EXACT-MATCH", amount_minor=100000, currency="INR",
        order_date=NOW, status="captured", refund_amount_minor=0,
        description="Order ORD1 - Premium Plan checkout",
    )
    defaults.update(overrides)
    return MerchantRecord(**defaults)


def _razorpay(**overrides) -> RazorpaySettlementRecord:
    defaults = dict(
        payment_id="pay_1", order_reference="DIFFERENT-REF", settlement_id="setl_1",
        gross_amount_minor=100000, fee_minor=2000, tax_minor=360, net_amount_minor=97640,
        refund_amount_minor=0, order_date=NOW, settlement_date=NOW + timedelta(days=2),
        currency="INR", status="settled", description="Settlement note order premium plan",
    )
    defaults.update(overrides)
    return RazorpaySettlementRecord(**defaults)


class AlwaysFailingVerifier:
    def compare(self, comparison):
        raise ProviderError(ProviderErrorKind.QUOTA_EXHAUSTED, "groq", "every provider is out of quota")


def test_total_provider_failure_lands_the_record_in_human_review_with_no_match():
    """The safety property the whole fallback chain exists to protect: when
    no model can answer, the record is held for a human. It is never
    auto-reconciled, never matched on a guess, and never allowed to take
    down the rest of the batch."""
    records = [ReconciliationRecord(record_id="R1", merchant=_merchant())]
    pool = [_razorpay()]

    results = process_batch(records, pool, policy=PolicyConfig(), semantic_verifier=AlwaysFailingVerifier())

    assert len(results) == 1
    result = results[0]
    assert result.outcome is ReconciliationOutcome.HUMAN_REVIEW
    assert result.classification is MatchClassification.PROVIDER_ERROR
    assert result.matched_payment_id is None
    assert "provider" in result.reason.lower() or "ai" in result.reason.lower()


def test_a_dead_provider_does_not_stop_the_rest_of_the_batch():
    """One record's provider failure must not cost the other 999 records
    their decisions."""
    records = [
        ReconciliationRecord(record_id="R1", merchant=_merchant()),
        ReconciliationRecord(record_id="R2", merchant=_merchant(
            order_id="ORD2", reference_id="ORD-2", description="Order ORD2 - Premium Plan")),
    ]
    pool = [_razorpay(), _razorpay(payment_id="pay_2", order_reference="ORD-2", settlement_id="setl_2",
                                   description="Settlement for ORD2")]

    results = process_batch(records, pool, policy=PolicyConfig(), semantic_verifier=AlwaysFailingVerifier())

    by_id = {r.record_id: r for r in results}
    assert by_id["R1"].outcome is ReconciliationOutcome.HUMAN_REVIEW
    assert by_id["R2"].outcome is ReconciliationOutcome.RECONCILED
    assert by_id["R2"].matched_payment_id == "pay_2"


def test_the_engine_classifies_a_raised_provider_error_as_provider_error():
    """Pins the contract between this module and matching.py: a raised
    exception from the verifier becomes MatchClassification.PROVIDER_ERROR,
    not an AMBIGUOUS verdict and not a crash."""
    index = matching.ReferenceIndex([_razorpay()])
    outcome = matching.resolve_fuzzy_or_semantic(
        _merchant(), index, PolicyConfig(), AlwaysFailingVerifier()
    )
    assert outcome.method == "semantic_error"
    assert outcome.classification is MatchClassification.PROVIDER_ERROR
    assert outcome.candidate is None


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------

def _client(monkeypatch, chain):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import ai as ai_api

    ai_api._reset_cache_for_tests()
    monkeypatch.setattr(ai_api, "build_chain", lambda: chain)
    app = FastAPI()
    app.include_router(ai_api.router)
    return TestClient(app)


def test_health_endpoint_shape(monkeypatch):
    gem = FakeProvider("gemini", "gemini-3.5-flash-lite")
    groq = FakeProvider("groq", "openai/gpt-oss-120b",
                        error=ProviderError(ProviderErrorKind.QUOTA_EXHAUSTED, "groq", "daily limit"))
    body = _client(monkeypatch, FallbackChain([gem, groq])).get("/ai/health").json()

    assert body["status"] == "AI_AVAILABLE"
    assert body["checked_at"]
    assert [p["provider"] for p in body["providers"]] == ["gemini", "groq"]
    gemini_row, groq_row = body["providers"]
    assert gemini_row == {
        "provider": "gemini", "available": True, "model": "gemini-3.5-flash-lite",
        "latency_ms": 1.0, "error_kind": None, "detail": "ok",
    }
    assert groq_row["available"] is False
    assert groq_row["error_kind"] == "QUOTA_EXHAUSTED"


def test_health_endpoint_is_reachable_on_the_contract_path(monkeypatch):
    client = _client(monkeypatch, FallbackChain([FakeProvider("gemini")]))
    assert client.get("/api/ai/health").status_code == 200


def test_health_endpoint_never_returns_key_material(monkeypatch):
    """The status panel is the most-polled surface in the product and the
    one most likely to end up in a screenshot."""
    leaky = FakeProvider("gemini", "m", error=ProviderError(
        ProviderErrorKind.AUTH_FAILURE, "gemini", f"bad key {FAKE_GEMINI_KEY}"))
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_GEMINI_KEY)
    raw = _client(monkeypatch, FallbackChain([leaky])).get("/ai/health").text

    assert FAKE_GEMINI_KEY not in raw
    assert FAKE_GROQ_KEY not in raw
    assert "api_key" not in raw.lower() and "authorization" not in raw.lower()


def test_health_is_cached_for_a_minute_so_polling_cannot_burn_quota(monkeypatch):
    """A status light that spends a model call per poll per open tab is how
    you exhaust the quota you are reporting on."""
    gem = FakeProvider("gemini")
    client = _client(monkeypatch, FallbackChain([gem]))

    first = client.get("/ai/health").json()
    second = client.get("/ai/health").json()
    assert first["cached"] is False and second["cached"] is True
    assert gem.calls == 0  # FakeProvider.health() does not call complete_json
    assert second["checked_at"] == first["checked_at"]

    forced = client.get("/ai/health?refresh=true").json()
    assert forced["cached"] is False


def test_health_reports_unavailable_when_ai_is_disabled(monkeypatch):
    monkeypatch.setenv("ACCORD_AI_DISABLED", "1")
    body = _client(monkeypatch, FallbackChain([FakeProvider("gemini")])).get("/ai/health").json()
    assert body["status"] == "AI_UNAVAILABLE"
    assert body["providers"] == []
    assert "ACCORD_AI_DISABLED" in body["detail"]


def test_health_with_no_providers_configured(monkeypatch):
    body = _client(monkeypatch, FallbackChain([])).get("/ai/health").json()
    assert body["status"] == "AI_UNAVAILABLE"
    assert body["providers"] == []
