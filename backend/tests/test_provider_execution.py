"""Provider request execution controls using local SQLite and mocked calls."""

import asyncio
from datetime import timedelta, timezone
import json
import logging
import importlib.util
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.domain.enrichment import ProviderErrorType
from app.models import ApiKey, Base, ProviderUsage
from app.models.entities import now_utc
from app.services.key_pool import ApiKeyPool
from app.services.provider_execution import (
    ProviderExecutionError,
    ProviderExecutionService,
    QuotaScope,
    RequestOutcome,
    RetryPolicy,
)
from app.database import SCHEMA_REVISION


def retry_policy(**overrides):
    values = {
        "max_retries": 2,
        "backoff_base_seconds": 0,
        "backoff_max_seconds": 0,
        "jitter_seconds": 0,
        "max_concurrency": 5,
        "key_lease_seconds": 60,
        "failure_cooldown_seconds": 0,
        "rate_limit_cooldown_seconds": 30,
        "quota_cooldown_seconds": 600,
    }
    values.update(overrides)
    return RetryPolicy(**values)


def test_provider_execution_migration_follows_enrichment_head():
    migration_path = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0005_provider_execution_controls.py"
    spec = importlib.util.spec_from_file_location("0005_provider_execution_controls", migration_path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.revision == "0005_provider_execution_controls"
    assert migration.down_revision == "0004_enrichment_evidence"
    assert SCHEMA_REVISION == "0010_outreach_preparation"


async def add_key(session_factory, provider="mock", credential="mock-secret", **values):
    async with session_factory() as session:
        key = ApiKey(provider=provider, key=credential, **values)
        session.add(key)
        await session.commit()
        return key.id


async def get_key(session_factory, key_id):
    async with session_factory() as session:
        return await session.get(ApiKey, key_id)


async def usage_rows(session_factory):
    async with session_factory() as session:
        return (await session.scalars(select(ProviderUsage).order_by(ProviderUsage.retry_count, ProviderUsage.created_at))).all()


@pytest.mark.asyncio
async def test_concurrent_workers_atomically_lease_different_keys(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'lease-race.db'}", connect_args={"timeout": 10})
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await add_key(factory, credential="race-secret")
    pool = ApiKeyPool()

    async def lease_one():
        async with factory() as session:
            return await pool.lease(session, "mock", "lookup", lease_seconds=60)

    first, second = await asyncio.gather(lease_one(), lease_one())
    assert (first is None) != (second is None)
    assert first is not None or second is not None
    await engine.dispose()


@pytest.mark.asyncio
async def test_disabled_cooldown_and_operation_scoped_keys_are_filtered(session_factory):
    disabled_id = await add_key(session_factory, credential="disabled", enabled=False)
    cooling_id = await add_key(session_factory, credential="cooling", cooldown_until=now_utc() + timedelta(minutes=2))
    scoped_id = await add_key(session_factory, credential="specific", operation_scope="details")
    available_id = await add_key(session_factory, credential="generic")
    pool = ApiKeyPool()

    async with session_factory() as session:
        first = await pool.lease(session, "mock", "search")
        assert first is not None and first.id == available_id
        async with session_factory() as other_session:
            exact_scope = await pool.lease(other_session, "mock", "details")
            assert exact_scope is not None and exact_scope.id == scoped_id
        assert disabled_id != cooling_id


@pytest.mark.asyncio
async def test_expired_cooldown_makes_key_available_again(session_factory):
    key_id = await add_key(session_factory, credential="cooling", cooldown_until=now_utc() + timedelta(minutes=2))
    async with session_factory() as session:
        assert await ApiKeyPool().lease(session, "mock", "lookup") is None
    async with session_factory() as session:
        key = await session.get(ApiKey, key_id)
        key.cooldown_until = now_utc() - timedelta(seconds=1)
        await session.commit()
    async with session_factory() as session:
        lease = await ApiKeyPool().lease(session, "mock", "lookup")
    assert lease is not None and lease.id == key_id


@pytest.mark.asyncio
async def test_transient_failure_increments_state_then_retries_within_limit(session_factory):
    key_id = await add_key(session_factory, credential="retry-secret")
    service = ProviderExecutionService(session_factory, retry_policy=retry_policy())
    calls = []

    async def request(credential):
        calls.append(credential)
        if len(calls) < 3:
            raise OSError("temporary network issue")
        return "done"

    assert await service.execute("mock", "lookup", request, quota_units=2.5, cost_units=0.25) == "done"
    assert len(calls) == 3
    key = await get_key(session_factory, key_id)
    assert key.failure_count == 2 and key.success_count == 1
    assert key.last_failure_at is not None and key.last_error is None
    rows = await usage_rows(session_factory)
    assert [row.outcome for row in rows] == ["transient", "transient", "success"]
    assert [row.retry_count for row in rows] == [0, 1, 2]
    assert rows[-1].key_fingerprint == ApiKeyPool.fingerprint("retry-secret")
    assert rows[-1].quota_units == 2.5 and rows[-1].cost_units == 0.25
    assert rows[-1].started_at is not None and rows[-1].completed_at is not None


@pytest.mark.asyncio
async def test_retry_after_cools_rate_limited_key_and_rotates_to_another(session_factory):
    await add_key(session_factory, credential="limited-secret")
    await add_key(session_factory, credential="other-secret")
    service = ProviderExecutionService(session_factory, retry_policy=retry_policy())
    seen = []

    async def request(credential):
        seen.append(credential)
        if len(seen) == 1:
            return httpx.Response(429, headers={"Retry-After": "90", "X-RateLimit-Limit": "100", "X-RateLimit-Remaining": "0"})
        return httpx.Response(200, json={"ok": True})

    result = await service.execute("mock", "lookup", request)
    assert result.status_code == 200
    assert seen[0] != seen[1]
    async with session_factory() as session:
        keys = (await session.scalars(select(ApiKey))).all()
        limited = next(key for key in keys if key.key == seen[0])
        assert limited.cooldown_until.replace(tzinfo=timezone.utc) >= now_utc() + timedelta(seconds=89)
    async with session_factory() as session:
        remaining = await ApiKeyPool().lease(session, "mock", "lookup")
        assert remaining is not None and remaining.id != limited.id
    rows = await usage_rows(session_factory)
    assert rows[0].outcome == "rate_limited"
    assert rows[0].http_status == 429 and rows[0].rate_limit_remaining == 0
    assert rows[0].rate_limit_reset_at is not None
    assert rows[1].key_fingerprint == ApiKeyPool.fingerprint(seen[1])


@pytest.mark.asyncio
async def test_quota_exhaustion_is_separate_from_rate_limit(session_factory):
    key_id = await add_key(session_factory, credential="quota-secret")
    service = ProviderExecutionService(session_factory, retry_policy=retry_policy())
    calls = 0

    async def request(_credential):
        nonlocal calls
        calls += 1
        return httpx.Response(403, json={"error": "daily quota exceeded"})

    with pytest.raises(ProviderExecutionError) as caught:
        await service.execute("mock", "lookup", request)
    assert caught.value.outcome == RequestOutcome.QUOTA_EXHAUSTED
    assert calls == 1
    key = await get_key(session_factory, key_id)
    assert key.quota_exhausted_until is not None and key.cooldown_until is None
    async with session_factory() as session:
        assert await ApiKeyPool().lease(session, "mock", "lookup") is None
    rows = await usage_rows(session_factory)
    assert rows[0].error_category == ProviderErrorType.QUOTA_EXHAUSTED.value


@pytest.mark.asyncio
async def test_provider_operation_quota_scope_holds_all_keys_for_that_operation(session_factory):
    await add_key(session_factory, credential="shared-quota-one")
    await add_key(session_factory, credential="shared-quota-two")
    service = ProviderExecutionService(session_factory, retry_policy=retry_policy())
    calls = 0

    async def request(_credential):
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "45"})

    with pytest.raises(ProviderExecutionError) as caught:
        await service.execute("mock", "shared-op", request, quota_scope=QuotaScope.PROVIDER_OPERATION)
    assert caught.value.outcome == RequestOutcome.RATE_LIMITED
    assert calls == 1
    async with session_factory() as session:
        assert await ApiKeyPool().lease(session, "mock", "shared-op") is None
        assert await ApiKeyPool().lease(session, "mock", "different-op") is not None
    rows = await usage_rows(session_factory)
    assert rows[0].outcome == "rate_limited"
    assert rows[1].outcome == "rate_limited"


@pytest.mark.asyncio
async def test_provider_operation_quota_exhaustion_is_distinct_and_persisted(session_factory):
    await add_key(session_factory, credential="shared-quota")
    service = ProviderExecutionService(session_factory, retry_policy=retry_policy())
    calls = 0

    async def request(_credential):
        nonlocal calls
        calls += 1
        return httpx.Response(402, json={"error": "billing limit"})

    with pytest.raises(ProviderExecutionError) as caught:
        await service.execute("mock", "billing-op", request, quota_scope=QuotaScope.PROVIDER_OPERATION)
    assert caught.value.outcome == RequestOutcome.QUOTA_EXHAUSTED and calls == 1
    with pytest.raises(ProviderExecutionError) as held:
        await service.execute("mock", "billing-op", request, quota_scope=QuotaScope.PROVIDER_OPERATION)
    assert held.value.outcome == RequestOutcome.QUOTA_EXHAUSTED and calls == 1
    async with session_factory() as session:
        hold = await ApiKeyPool().provider_operation_hold(session, "mock", "billing-op")
        assert hold is not None and hold[0] == ProviderErrorType.QUOTA_EXHAUSTED


@pytest.mark.asyncio
async def test_authentication_failure_disables_key_and_is_not_retried(session_factory):
    key_id = await add_key(session_factory, credential="invalid-secret")
    service = ProviderExecutionService(session_factory, retry_policy=retry_policy())
    calls = 0

    async def request(_credential):
        nonlocal calls
        calls += 1
        return httpx.Response(401)

    with pytest.raises(ProviderExecutionError) as caught:
        await service.execute("mock", "lookup", request)
    assert caught.value.outcome == RequestOutcome.AUTHENTICATION
    assert calls == 1
    key = await get_key(session_factory, key_id)
    assert not key.enabled and key.status == "DISABLED" and key.failure_count == 1


@pytest.mark.asyncio
async def test_permanent_and_invalid_response_failures_are_not_retried(session_factory):
    service = ProviderExecutionService(session_factory, retry_policy=retry_policy())
    for exception, outcome in [
        (ValueError("bad request"), RequestOutcome.PERMANENT),
        (json.JSONDecodeError("invalid", "x", 0), RequestOutcome.INVALID_RESPONSE),
    ]:
        calls = 0

        async def request(_credential):
            nonlocal calls
            calls += 1
            raise exception

        with pytest.raises(ProviderExecutionError) as caught:
            await service.execute("mock", "parse", request, requires_api_key=False)
        assert caught.value.outcome == outcome and calls == 1


@pytest.mark.asyncio
async def test_timeout_is_classified_and_retried(session_factory):
    service = ProviderExecutionService(session_factory, retry_policy=retry_policy(max_retries=1))
    calls = 0

    async def request(_credential):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ReadTimeout("timeout")
        return "recovered"

    assert await service.execute("mock", "read", request, requires_api_key=False) == "recovered"
    rows = await usage_rows(session_factory)
    assert calls == 2 and rows[0].outcome == "timeout" and rows[1].outcome == "success"


@pytest.mark.asyncio
async def test_credentials_are_absent_from_error_messages_logs_and_usage(session_factory, caplog):
    secret = "top-secret-provider-token"
    key_id = await add_key(session_factory, credential=secret)
    service = ProviderExecutionService(session_factory, retry_policy=retry_policy(max_retries=0))

    async def request(credential):
        raise RuntimeError(f"provider echoed {credential}")

    with caplog.at_level(logging.WARNING, logger="lead_agent.provider_execution"):
        with pytest.raises(ProviderExecutionError) as caught:
            await service.execute("mock", "inspect", request)

    assert secret not in str(caught.value)
    assert secret not in caplog.text
    key = await get_key(session_factory, key_id)
    rows = await usage_rows(session_factory)
    assert secret not in key.last_error
    assert secret not in str(rows[0].key_fingerprint)
    assert rows[0].key_fingerprint == ApiKeyPool.fingerprint(secret)


@pytest.mark.asyncio
async def test_keyless_provider_execution_records_usage_for_ollama_compatibility(session_factory):
    service = ProviderExecutionService(session_factory, retry_policy=retry_policy())

    async def request(credential):
        assert credential is None
        return "local response"

    result = await service.execute("ollama", "generate", request, requires_api_key=False, model="qwen-local")
    assert result == "local response"
    rows = await usage_rows(session_factory)
    assert rows[0].provider == "ollama" and rows[0].operation == "generate"
    assert rows[0].model == "qwen-local" and rows[0].key_fingerprint is None
