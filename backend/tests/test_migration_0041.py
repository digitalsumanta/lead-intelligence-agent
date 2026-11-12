"""
Isolated Database Test for Migration 0041: supervisor_dispatch_delivery_events.

Phase 12J - D2 - Gate D-C4:
Validates that migration 0041 successfully applies upgrade, downgrade, and re-upgrade
against an isolated disposable PostgreSQL test database.
"""
from __future__ import annotations

import asyncio
import os
import uuid
import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings

PG_ADMIN_URL = "postgresql://postgres:postgres@localhost:5432/postgres"



@pytest.mark.asyncio
async def test_migration_0041_upgrade_downgrade_reupgrade_cycle():
    """
    Validates Alembic migration 0041 on a disposable PostgreSQL database:
    1. Create disposable database
    2. Upgrade to 0041
    3. Verify table columns and constraints
    4. Downgrade to 0040
    5. Verify table is dropped
    6. Re-upgrade to 0041
    7. Drop disposable database
    """
    db_name = f"test_mig_0041_{uuid.uuid4().hex[:12]}"
    conn = await asyncpg.connect(PG_ADMIN_URL)
    await conn.execute(f"CREATE DATABASE {db_name}")
    await conn.close()

    # Pre-create alembic_version with VARCHAR(128) to accommodate long migration names
    db_conn = await asyncpg.connect(f"postgresql://postgres:postgres@localhost:5432/{db_name}")
    await db_conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(128) PRIMARY KEY)")
    await db_conn.close()

    target_url = f"postgresql+asyncpg://postgres:postgres@localhost:5432/{db_name}"
    old_db_url = settings.database_url
    settings.database_url = target_url


    try:
        cfg = Config("backend/alembic.ini")
        cfg.set_main_option("script_location", "backend/migrations")

        # 1. Upgrade to 0041
        await asyncio.to_thread(command.upgrade, cfg, "0041_supervisor_dispatch_delivery_events")


        # 2. Inspect table
        engine = create_async_engine(target_url)
        async with engine.connect() as db_conn:
            res = await db_conn.execute(
                text(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_name = 'supervisor_dispatch_delivery_events'"
                )
            )
            cols = {row[0]: row[1] for row in res.fetchall()}
            assert "provider_name" in cols
            assert "provider_event_id" in cols
            assert "recipient_masked" in cols
            assert "metadata_json" in cols

        await engine.dispose()

        # 3. Downgrade back to 0040
        await asyncio.to_thread(command.downgrade, cfg, "0040_supervisor_approval_dispatches")

        # 4. Verify table is dropped
        engine = create_async_engine(target_url)
        async with engine.connect() as db_conn:
            res = await db_conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_name = 'supervisor_dispatch_delivery_events'"
                )
            )
            assert res.scalar_one_or_none() is None
        await engine.dispose()

        # 5. Re-upgrade to 0041
        await asyncio.to_thread(command.upgrade, cfg, "0041_supervisor_dispatch_delivery_events")

        engine = create_async_engine(target_url)
        async with engine.connect() as db_conn:
            res = await db_conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_name = 'supervisor_dispatch_delivery_events'"
                )
            )
            assert res.scalar_one_or_none() == "supervisor_dispatch_delivery_events"
        await engine.dispose()

    finally:
        settings.database_url = old_db_url
        # Teardown: terminate active connections and drop disposable test database
        teardown_conn = await asyncpg.connect(PG_ADMIN_URL)
        await teardown_conn.execute(
            f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{db_name}' AND pid <> pg_backend_pid()"
        )
        await teardown_conn.execute(f"DROP DATABASE {db_name}")
        await teardown_conn.close()

