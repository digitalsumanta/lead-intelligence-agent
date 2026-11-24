"""Isolated PostgreSQL test for migration 0044: app_users and auth_sessions (upgrade/downgrade/re-upgrade)."""
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
            return bool((await conn.execute(text("SELECT to_regclass('public.auth_sessions') IS NOT NULL"))).scalar())
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_migration_0044_upgrade_downgrade_reupgrade_cycle():
    db_name = f"test_mig_0044_{uuid.uuid4().hex[:12]}"
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

        await asyncio.to_thread(command.upgrade, cfg, "0044_app_users_and_sessions")
        assert await _table_exists(target_url)

        engine = create_async_engine(target_url)
        try:
            uid = uuid.uuid4()
            async with engine.begin() as c:
                await c.execute(text("INSERT INTO app_users (id, email, role, password_hash) VALUES (:id, 'a@b.example', 'admin', 'x')"), {"id": uid})
                await c.execute(text("INSERT INTO auth_sessions (id, token_hash, user_id, expires_at) VALUES (:id, :h, :u, now())"),
                                {"id": uuid.uuid4(), "h": "c" * 64, "u": uid})
            for sql in (
                "INSERT INTO app_users (id, email, role, password_hash) VALUES (:id, 'a@b.example', 'admin', 'x')",   # duplicate email
                "INSERT INTO app_users (id, email, role, password_hash) VALUES (:id, 'Upper@b.example', 'admin', 'x')",  # not lower-case
                "INSERT INTO app_users (id, email, role, password_hash) VALUES (:id, 'r@b.example', 'root', 'x')",  # bad role
            ):
                with pytest.raises(IntegrityError):
                    async with engine.begin() as c:
                        await c.execute(text(sql), {"id": uuid.uuid4()})
            async with engine.begin() as c:  # sessions cascade with their user
                await c.execute(text("DELETE FROM app_users WHERE id = :id"), {"id": uid})
                assert (await c.execute(text("SELECT count(*) FROM auth_sessions"))).scalar() == 0
        finally:
            await engine.dispose()

        await asyncio.to_thread(command.downgrade, cfg, "0043_email_suppressions")
        assert not await _table_exists(target_url)

        await asyncio.to_thread(command.upgrade, cfg, "0044_app_users_and_sessions")
        assert await _table_exists(target_url)
    finally:
        settings.database_url = old_db_url
        admin_conn = await asyncpg.connect(PG_ADMIN_URL)
        await admin_conn.execute(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{db_name}'")
        await admin_conn.execute(f"DROP DATABASE IF EXISTS {db_name}")
        await admin_conn.close()
