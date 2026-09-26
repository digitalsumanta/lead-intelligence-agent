"""Readiness checks must reflect serviceability without making provider calls."""

import json

import httpx
import pytest
from fastapi.responses import JSONResponse
from sqlalchemy import select

from app.config import Settings
from app.models import ApiKey
from app.providers import get_provider
from app.providers.ollama import OllamaProvider
from app.services.provider_execution import ProviderExecutionService


async def invoke_readiness(monkeypatch, session_factory, provider):
    from app import main

    async def no_op():
        return None

    async def worker_ready():
        return True

    monkeypatch.setattr(main, "verify_database_schema", no_op)
    monkeypatch.setattr(main, "worker_is_available", worker_ready)
    monkeypatch.setattr(main, "SessionLocal", session_factory)
    monkeypatch.setattr(main, "get_provider", lambda: provider)
    result = await main.readiness()
    if isinstance(result, JSONResponse):
        return result.status_code, json.loads(result.body)
    return 200, result


class MockHealthProvider:
    def __init__(self, name, *, healthy=True, model_available=None, configured=True, operation=None):
        self.name = name
        self.operation = operation
        self.health = {"provider": name, "healthy": healthy, "configured": configured}
        if model_available is not None:
            self.health["model_available"] = model_available

    async def health_check(self):
        return dict(self.health)


@pytest.mark.asyncio
@pytest.mark.parametrize(("model_available", "expected"), [(True, True), (False, False)])
async def test_ollama_readiness_requires_configured_model(session_factory, monkeypatch, model_available, expected):
    provider = MockHealthProvider("ollama", healthy=True, model_available=model_available)
    status, payload = await invoke_readiness(monkeypatch, session_factory, provider)
    assert payload["ready"] is expected
    assert status == (200 if expected else 503)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider", "operation", "has_key", "expected"),
    [
        ("gemini", "generate_content", True, True),
        ("gemini", "generate_content", False, False),
        ("openai_compatible", "chat_completions", False, False),
    ],
)
async def test_cloud_readiness_requires_usable_scoped_key(session_factory, monkeypatch, provider, operation, has_key, expected):
    if has_key:
        async with session_factory() as session:
            session.add(ApiKey(provider=provider, key="readiness-mock-key", operation_scope=operation))
            await session.commit()
    selected = MockHealthProvider(provider, healthy=True, configured=True, operation=operation)
    status, payload = await invoke_readiness(monkeypatch, session_factory, selected)
    assert payload["ready"] is expected
    assert status == (200 if expected else 503)
    assert payload["checks"]["provider"]["key_pool"]["healthy"] == int(has_key)
    async with session_factory() as session:
        key = await session.scalar(select(ApiKey).where(ApiKey.provider == provider))
        assert key is None or key.lease_token is None


@pytest.mark.asyncio
async def test_disabled_nonselected_cloud_provider_does_not_fail_ollama_readiness(session_factory, monkeypatch):
    config = Settings(_env_file=None, ai_provider="ollama", ai_gemini_enabled=False)

    async def handler(_request):
        return httpx.Response(200, json={"models": [{"name": config.ollama_model}]})

    def client_factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    selected = get_provider(
        execution_service=ProviderExecutionService(session_factory),
        config=config,
        client_factory=client_factory,
    )
    status, payload = await invoke_readiness(monkeypatch, session_factory, selected)
    assert status == 200 and payload["ready"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(("listed_name", "expected"), [("qwen2.5-coder:latest", True), ("qwen2.5-coder:3b", False)])
async def test_ollama_readiness_requires_exact_configured_model_tag(session_factory, monkeypatch, listed_name, expected):
    config = Settings(_env_file=None)

    async def handler(_request):
        return httpx.Response(200, json={"models": [{"name": listed_name}]})

    def client_factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    provider = OllamaProvider(base_url="http://ollama.test", model=config.ollama_model, client_factory=client_factory)
    status, payload = await invoke_readiness(monkeypatch, session_factory, provider)
    assert payload["ready"] is expected
    assert status == (200 if expected else 503)
