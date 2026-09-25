"""Opt-in checks against an already migrated, real PostgreSQL database."""
import os
import asyncio
import pytest
from sqlalchemy import text, delete
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.models import ApiKey, Lead, ProcessingJob, JobStatus
from app.services.key_pool import ApiKeyPool
from app.services.jobs import claim_jobs

pytestmark = pytest.mark.postgres_integration

@pytest.mark.asyncio
async def test_postgresql_schema_is_migrated_to_head():
    url = os.getenv("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set TEST_POSTGRES_URL to an explicitly provisioned PostgreSQL database")
    if not url.startswith("postgresql+asyncpg://"):
        pytest.fail("TEST_POSTGRES_URL must use the asyncpg PostgreSQL dialect")
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            version = await connection.scalar(text("SELECT version_num FROM alembic_version"))
            assert version == "0006_resumable_enrichment_stages"
            tables = set(await connection.run_sync(lambda conn: __import__("sqlalchemy").inspect(conn).get_table_names()))
            assert {"leads", "lead_processing_jobs", "lead_batches", "lead_events", "enrichment_runs", "evidence", "provider_operation_state"}.issubset(tables)
            indexes = await connection.run_sync(lambda conn: __import__("sqlalchemy").inspect(conn).get_indexes("evidence"))
            assert {"ix_evidence_lead", "ix_evidence_enrichment_run", "ix_evidence_type", "ix_evidence_provider_source", "ix_evidence_observed_at", "ix_evidence_fetched_at", "ix_evidence_provider_reference"}.issubset({index["name"] for index in indexes})
            api_key_columns = await connection.run_sync(lambda conn: {column["name"] for column in __import__("sqlalchemy").inspect(conn).get_columns("api_keys")})
            usage_columns = await connection.run_sync(lambda conn: {column["name"] for column in __import__("sqlalchemy").inspect(conn).get_columns("provider_usage")})
            assert {"enabled", "operation_scope", "lease_token", "lease_expires_at", "quota_exhausted_until"}.issubset(api_key_columns)
            assert {"operation", "key_fingerprint", "outcome", "retry_count", "error_category", "rate_limit_reset_at"}.issubset(usage_columns)
    finally:
        await engine.dispose()

@pytest.mark.asyncio
async def test_postgresql_concurrent_workers_only_claim_job_once():
    url = os.getenv("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set TEST_POSTGRES_URL to an explicitly provisioned PostgreSQL database")
    if not url.startswith("postgresql+asyncpg://"):
        pytest.fail("TEST_POSTGRES_URL must use the asyncpg PostgreSQL dialect")
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    lead_id = job_id = None
    try:
        async with factory() as session:
            lead = Lead(company_name="PostgreSQL claim integration")
            session.add(lead); await session.flush()
            job = ProcessingJob(lead_id=lead.id, status=JobStatus.QUEUED)
            session.add(job); await session.commit()
            lead_id, job_id = lead.id, job.id
        async def claim():
            async with factory() as session:
                return await claim_jobs(session, 1)
        claims = await asyncio.gather(claim(), claim())
        assert sum(job_id in claimed for claimed in claims) == 1
    finally:
        async with factory() as session:
            if job_id: await session.execute(delete(ProcessingJob).where(ProcessingJob.id == job_id))
            if lead_id: await session.execute(delete(Lead).where(Lead.id == lead_id))
            await session.commit()
        await engine.dispose()

@pytest.mark.asyncio
async def test_postgresql_concurrent_provider_requests_only_lease_key_once():
    url = os.getenv("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set TEST_POSTGRES_URL to an explicitly provisioned PostgreSQL database")
    if not url.startswith("postgresql+asyncpg://"):
        pytest.fail("TEST_POSTGRES_URL must use the asyncpg PostgreSQL dialect")
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    key_id = None
    try:
        async with factory() as session:
            key = ApiKey(provider="lease-integration", key="integration-secret")
            session.add(key)
            await session.commit()
            key_id = key.id

        async def lease():
            async with factory() as session:
                return await ApiKeyPool().lease(session, "lease-integration", "lookup")

        first, second = await asyncio.gather(lease(), lease())
        assert (first is None) != (second is None)
    finally:
        if key_id is not None:
            async with factory() as session:
                await session.execute(delete(ApiKey).where(ApiKey.id == key_id))
                await session.commit()
        await engine.dispose()
