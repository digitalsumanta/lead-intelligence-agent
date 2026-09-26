"""Mocked contract, adapter, selection, and shared execution coverage."""

import json
from datetime import timedelta, timezone

import httpx
import pytest
from sqlalchemy import select

from app.config import Settings
from app.domain.ai import AIProviderException, AIProviderOutcome, AIRequest
from app.models import ApiKey, ProviderUsage
from app.models.entities import now_utc
from app.providers import AIProviderRegistry, AIProviderSelector, get_provider
from app.providers.ai_http import parse_structured
from app.providers.gemini import GeminiProvider
from app.providers.ollama import OllamaProvider
from app.providers.openai_compatible import OpenAICompatibleProvider
from app.services.provider_execution import ProviderExecutionService, RetryPolicy


def factory_for(handler):
    def client_factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)
    return client_factory


async def build_execution(session_factory, **overrides):
    policy = RetryPolicy(max_retries=overrides.pop("max_retries", 0), backoff_base_seconds=0, backoff_max_seconds=0, jitter_seconds=0, failure_cooldown_seconds=0, rate_limit_cooldown_seconds=30, **overrides)
    return ProviderExecutionService(session_factory, retry_policy=policy, sleep=lambda _: _no_sleep())


async def _no_sleep():
    return None


async def add_key(session_factory, provider, key):
    async with session_factory() as session:
        session.add(ApiKey(provider=provider, key=key))
        await session.commit()


def test_contract_rejects_secret_metadata_and_validates_schema():
    with pytest.raises(ValueError, match="credentials"):
        AIRequest(user_prompt="hello", metadata={"api_key": "secret"})
    request = AIRequest(user_prompt="hello", response_schema={"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}})
    assert request.user_prompt == "hello"


def test_provider_selection_is_explicit_and_disabled_provider_does_not_fall_back(session_factory):
    config = Settings(_env_file=None)
    execution = ProviderExecutionService(session_factory)
    selector = AIProviderSelector(AIProviderRegistry(execution, config), config)
    assert isinstance(selector.select().name, str)
    with pytest.raises(ValueError):
        selector.select("gemini")
    with pytest.raises(ValueError, match="not configured"):
        selector.select("openai")
    enabled = Settings(_env_file=None, ai_gemini_enabled=True)
    cloud_selector = AIProviderSelector(AIProviderRegistry(execution, enabled), enabled)
    assert cloud_selector.select("gemini").name == "gemini"


@pytest.mark.asyncio
async def test_ollama_structured_success_keeps_legacy_and_new_contracts(session_factory):
    async def handler(request):
        assert request.url.path == "/api/generate"
        body = json.loads(request.content)
        assert body["format"]["required"] == ["answer"]
        return httpx.Response(200, json={"response": '{"answer":"ok"}', "prompt_eval_count": 3, "eval_count": 2})
    execution = await build_execution(session_factory)
    provider = OllamaProvider(base_url="http://ollama.test", model="local", execution=execution, client_factory=factory_for(handler))
    response = await provider.generate(AIRequest(user_prompt="return answer", response_schema={"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string"}}}))
    assert response.parsed_json == {"answer": "ok"}
    assert response.usage.total_tokens == 5
    assert await provider.generate_structured("prompt", {"type": "object", "required": ["answer"]}) == '{"answer":"ok"}'


@pytest.mark.asyncio
async def test_gemini_success_leases_key_releases_and_records_usage(session_factory):
    await add_key(session_factory, "gemini", "gem-secret")
    async def handler(request):
        assert request.url.path.endswith("/models/gemini-test:generateContent")
        assert request.headers["x-goog-api-key"] == "gem-secret"
        body = json.loads(request.content)
        assert body["generationConfig"]["responseMimeType"] == "application/json"
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": '{"answer":"ok"}'}]}}], "usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 2}})
    execution = await build_execution(session_factory)
    provider = GeminiProvider(execution, enabled=True, model="gemini-test", client_factory=factory_for(handler))
    response = await provider.generate(AIRequest(user_prompt="say ok", response_schema={"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string"}}}))
    assert response.parsed_json == {"answer": "ok"}
    async with session_factory() as session:
        key = await session.scalar(select(ApiKey).where(ApiKey.provider == "gemini"))
        usage = await session.scalar(select(ProviderUsage).where(ProviderUsage.provider == "gemini"))
        assert key.success_count == 1 and key.lease_token is None
        assert usage.success and usage.key_fingerprint and "gem-secret" not in usage.key_fingerprint


def test_gemini_translates_pydantic_style_schema_refs_and_nullable_fields():
    schema = {
        "$defs": {"Answer": {"type": "string"}},
        "type": "object",
        "properties": {"answer": {"anyOf": [{"$ref": "#/$defs/Answer"}, {"type": "null"}]}},
        "required": ["answer"],
        "additionalProperties": False,
    }
    normalized = GeminiProvider._schema_for(AIRequest(user_prompt="x", response_schema=schema))
    assert normalized["properties"]["answer"] == {"type": "string", "nullable": True}


@pytest.mark.asyncio
async def test_gemini_rejects_unsupported_schema_before_execution_or_lease(session_factory):
    await add_key(session_factory, "gemini", "must-not-be-leased")
    execution = await build_execution(session_factory)

    class ExecutionSpy:
        calls = 0

        async def execute(self, *args, **kwargs):
            self.calls += 1
            return await execution.execute(*args, **kwargs)

    spy = ExecutionSpy()

    async def handler(_request):
        raise AssertionError("unsupported schemas must not send HTTP requests")

    provider = GeminiProvider(spy, enabled=True, client_factory=factory_for(handler))
    unsupported = AIRequest(user_prompt="x", response_schema={"anyOf": [{"type": "string"}, {"type": "integer"}]})
    with pytest.raises(AIProviderException) as caught:
        await provider.generate(unsupported)
    assert caught.value.error.outcome == AIProviderOutcome.UNSUPPORTED_CAPABILITY
    assert spy.calls == 0
    async with session_factory() as session:
        key = await session.scalar(select(ApiKey).where(ApiKey.provider == "gemini"))
        assert key.lease_token is None
        assert await session.scalar(select(ProviderUsage).where(ProviderUsage.provider == "gemini")) is None


@pytest.mark.parametrize(
    ("schema", "content"),
    [
        ({"$defs": {"Answer": {"type": "string"}}, "type": "object", "properties": {"answer": {"$ref": "#/$defs/Answer"}}, "required": ["answer"]}, '{"answer":3}'),
        ({"type": "string", "pattern": "^approved$"}, '"rejected"'),
        ({"type": "string", "format": "email"}, '"not-an-email"'),
        ({"type": "array", "items": {"type": "integer"}}, '[1,"two"]'),
        ({"oneOf": [{"type": "string"}, {"type": "integer"}]}, 'true'),
    ],
)
def test_schema_validation_rejects_invalid_ref_pattern_format_and_composition(schema, content):
    with pytest.raises(AIProviderException) as caught:
        parse_structured(content, AIRequest(user_prompt="x", response_schema=schema))
    assert caught.value.error.outcome == AIProviderOutcome.INVALID_STRUCTURED_OUTPUT


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "string", "minContains": 2},
        {"type": "string", "format": "unknown-format"},
        {"type": "object", "properties": {"value": {"$ref": "#/missing"}}},
    ],
)
def test_schema_validation_rejects_unsupported_keywords_formats_and_refs(schema):
    with pytest.raises(AIProviderException) as caught:
        parse_structured('"anything"', AIRequest(user_prompt="x", response_schema=schema))
    assert caught.value.error.outcome == AIProviderOutcome.UNSUPPORTED_CAPABILITY


@pytest.mark.parametrize(
    ("schema", "content"),
    [
        ({"type": "object", "required": ["answer"]}, "{}"),
        ({"type": "string", "minLength": 2}, '"a"'),
        ({"type": "string", "maxLength": 1}, '"ab"'),
        ({"type": "integer", "minimum": 2}, "1"),
        ({"type": "integer", "maximum": 2}, "3"),
        ({"enum": ["allowed"]}, '"rejected"'),
        ({"const": True}, "1"),
        ({"enum": [1]}, "true"),
        ({"type": "object", "properties": {"x": {"type": "string", "additionalProperties": None}}}, '{"x":"valid"}'),
    ],
)
def test_schema_validation_enforces_required_types_bounds_and_json_equality(schema, content):
    with pytest.raises(AIProviderException) as caught:
        parse_structured(content, AIRequest(user_prompt="x", response_schema=schema))
    assert caught.value.error.outcome in {AIProviderOutcome.INVALID_STRUCTURED_OUTPUT, AIProviderOutcome.UNSUPPORTED_CAPABILITY}


def test_schema_validation_rejects_duplicate_json_keys_and_unsupported_regex_features():
    with pytest.raises(AIProviderException) as invalid_output:
        parse_structured('{"x":1,"x":2}', AIRequest(user_prompt="x", response_schema={"type": "object"}))
    assert invalid_output.value.error.outcome == AIProviderOutcome.INVALID_STRUCTURED_OUTPUT
    with pytest.raises(AIProviderException) as unsupported:
        parse_structured('"anything"', AIRequest(user_prompt="x", response_schema={"type": "string", "pattern": r"\Afoo\Z"}))
    assert unsupported.value.error.outcome == AIProviderOutcome.UNSUPPORTED_CAPABILITY


@pytest.mark.asyncio
async def test_openai_compatible_success_and_endpoint_shape(session_factory):
    await add_key(session_factory, "openai_compatible", "local-secret")
    async def handler(request):
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer local-secret"
        body = json.loads(request.content)
        assert body["messages"][0]["role"] == "system"
        assert body["response_format"]["type"] == "json_schema"
        return httpx.Response(200, json={"model": "compatible-v2", "choices": [{"message": {"content": '{"answer":"yes"}'}}], "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6}})
    provider = OpenAICompatibleProvider(await build_execution(session_factory), enabled=True, base_url="http://compatible.test/v1", model="compatible-v1", client_factory=factory_for(handler))
    response = await provider.generate(AIRequest(system_prompt="be terse", user_prompt="answer", response_schema={"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string"}}}))
    assert response.model == "compatible-v2" and response.parsed_json == {"answer": "yes"}


@pytest.mark.asyncio
async def test_provider_errors_are_typed_and_do_not_leak_credentials(session_factory, caplog):
    secret = "do-not-leak-this"
    await add_key(session_factory, "gemini", secret)
    async def handler(request):
        return httpx.Response(401, json={"error": {"message": secret}})
    provider = GeminiProvider(await build_execution(session_factory, max_retries=2), enabled=True, client_factory=factory_for(handler))
    with pytest.raises(AIProviderException) as caught:
        await provider.generate(AIRequest(user_prompt="hello"))
    assert caught.value.error.outcome == AIProviderOutcome.AUTHENTICATION_FAILURE
    assert secret not in str(caught.value) and secret not in repr(caught.value.error)
    assert secret not in caplog.text
    async with session_factory() as session:
        usage = (await session.scalars(select(ProviderUsage).where(ProviderUsage.provider == "gemini"))).all()
        key = await session.scalar(select(ApiKey).where(ApiKey.provider == "gemini"))
        assert len(usage) == 1 and key.failure_count == 1 and not key.enabled


@pytest.mark.asyncio
async def test_retry_after_uses_next_key_and_malformed_structured_output_is_rejected(session_factory):
    await add_key(session_factory, "gemini", "first")
    await add_key(session_factory, "gemini", "second")
    calls = 0
    async def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "45"}, json={"error": {"message": "too many requests"}})
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": '{"wrong":true}'}]}}]})
    execution = await build_execution(session_factory, max_retries=1)
    provider = GeminiProvider(execution, enabled=True, client_factory=factory_for(handler))
    with pytest.raises(AIProviderException) as caught:
        await provider.generate(AIRequest(user_prompt="hello", response_schema={"type": "object", "required": ["answer"], "properties": {"answer": {"type": "string"}}}))
    assert calls == 2
    assert caught.value.error.outcome == AIProviderOutcome.INVALID_STRUCTURED_OUTPUT
    async with session_factory() as session:
        keys = (await session.scalars(select(ApiKey).where(ApiKey.provider == "gemini"))).all()
        assert any(key.cooldown_until is not None for key in keys)
        retry_cooldown = next(key.cooldown_until for key in keys if key.cooldown_until is not None)
        if retry_cooldown.tzinfo is None:
            retry_cooldown = retry_cooldown.replace(tzinfo=timezone.utc)
        assert retry_cooldown >= now_utc() + timedelta(seconds=40)
        usages = (await session.scalars(select(ProviderUsage).where(ProviderUsage.provider == "gemini"))).all()
        assert len(usages) == 2 and any(item.retry_count == 1 for item in usages)


@pytest.mark.asyncio
async def test_timeout_malformed_response_and_no_key_are_typed(session_factory):
    await add_key(session_factory, "gemini", "key")
    async def timeout(_request):
        raise httpx.ReadTimeout("private")
    provider = GeminiProvider(await build_execution(session_factory), enabled=True, client_factory=factory_for(timeout))
    with pytest.raises(AIProviderException) as caught:
        await provider.generate(AIRequest(user_prompt="timeout"))
    assert caught.value.error.outcome == AIProviderOutcome.TIMEOUT
    no_key = GeminiProvider(await build_execution(session_factory), enabled=True, client_factory=factory_for(timeout))
    async with session_factory() as session:
        await session.execute(ApiKey.__table__.delete().where(ApiKey.provider == "gemini"))
        await session.commit()
    with pytest.raises(AIProviderException) as caught:
        await no_key.generate(AIRequest(user_prompt="missing key"))
    assert caught.value.error.outcome == AIProviderOutcome.KEY_POOL_EXHAUSTED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (200, "not-json", AIProviderOutcome.MALFORMED_RESPONSE),
        (403, {"error": {"status": "QUOTA_EXCEEDED"}}, AIProviderOutcome.QUOTA_EXHAUSTED),
        (400, {"error": {"message": "bad request"}}, AIProviderOutcome.PERMANENT),
    ],
)
async def test_malformed_quota_and_permanent_responses_are_classified(session_factory, status, body, expected):
    await add_key(session_factory, "gemini", f"secret-{status}")
    async def handler(_request):
        return httpx.Response(status, content=body.encode() if isinstance(body, str) else None, json=body if isinstance(body, dict) else None)
    provider = GeminiProvider(await build_execution(session_factory, max_retries=2), enabled=True, client_factory=factory_for(handler))
    with pytest.raises(AIProviderException) as caught:
        await provider.generate(AIRequest(user_prompt="hello"))
    assert caught.value.error.outcome == expected
    assert caught.value.error.attempts == 1


@pytest.mark.asyncio
async def test_transient_server_error_retries_using_shared_execution(session_factory):
    await add_key(session_factory, "openai_compatible", "server-key-a")
    await add_key(session_factory, "openai_compatible", "server-key-b")
    calls = 0
    async def handler(_request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, json={"error": {"message": "temporary"}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ready"}}]})
    provider = OpenAICompatibleProvider(await build_execution(session_factory, max_retries=1), enabled=True, client_factory=factory_for(handler))
    response = await provider.generate(AIRequest(user_prompt="hello"))
    assert response.content == "ready" and calls == 2
