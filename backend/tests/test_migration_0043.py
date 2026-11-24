"""Isolated PostgreSQL test for migration 0043: email_suppressions (upgrade/downgrade/re-upgrade)."""
from __future__ import annotations

import asyncio
import uuid

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings

PG_ADMIN_URL = "postgresql://postgres:postgres@localhost:5432/postgres"


async def _table_exists(url: str) -> bool:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return bool((await conn.execute(text("SELECT to_regclass('public.email_suppressions') IS NOT NULL"))).scalar())
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migration_0043_upgrade_downgrade_reupgrade_cycle():
    db_name = f"test_mig_0043_{uuid.uuid4().hex[:12]}"
    conn = await asyncpg.connect(PG_ADMIN_URL)
    await conn.execute(f"CREATE DATABASE {db_name}")
    await conn.close()
    db_conn = await asyncpg.connect(f"postgresql://postgres:postgres@localhost:5432/{db_name}")
    await db_conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(128) PRIMARY KEY)")
    await db_conn.close()

    target_url = f"postgresql+asyncpg://postgres:postgres@localhost:5432/{db_name}"
    old_db_url = settings.database_url
    settings.database_url = target_url
    try:
        cfg = Config("backend/alembic.ini")
        cfg.set_main_option("script_location", "backend/migrations")

        await asyncio.to_thread(command.upgrade, cfg, "0043_email_suppressions")
        assert await _table_exists(target_url)

        engine = create_async_engine(target_url)
        try:
            async with engine.begin() as c:
                await c.execute(text(
                    "INSERT INTO email_suppressions (id, email_hash, reason, source) "
                    "VALUES (:id, :h, 'UNSUBSCRIBED', 'test')"), {"id": uuid.uuid4(), "h": "a" * 64})
            with pytest.raises(IntegrityError):  # unique email_hash
                async with engine.begin() as c:
                    await c.execute(text(
                        "INSERT INTO email_suppressions (id, email_hash, reason, source) "
                        "VALUES (:id, :h, 'MANUAL', 'test')"), {"id": uuid.uuid4(), "h": "a" * 64})
            with pytest.raises(IntegrityError):  # reason check constraint
                async with engine.begin() as c:
                    await c.execute(text(
                        "INSERT INTO email_suppressions (id, email_hash, reason, source) "
                        "VALUES (:id, :h, 'BOGUS', 'test')"), {"id": uuid.uuid4(), "h": "b" * 64})
        finally:
            await engine.dispose()

        await asyncio.to_thread(command.downgrade, cfg, "0042_canary_flight_authorizations")
        assert not await _table_exists(target_url)

        await asyncio.to_thread(command.upgrade, cfg, "0043_email_suppressions")
        assert await _table_exists(target_url)
    finally:
        settings.database_url = old_db_url
        admin_conn = await asyncpg.connect(PG_ADMIN_URL)
        await admin_conn.execute(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{db_name}'")
        await admin_conn.execute(f"DROP DATABASE IF EXISTS {db_name}")
        await admin_conn.close()
