"""
Isolated Database Test for Migration 0042: canary_flight_authorizations.

Phase 12J - D2 - Gate D-D3:
Validates that migration 0042 successfully applies upgrade, downgrade, and re-upgrade
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
async def test_migration_0042_upgrade_downgrade_reupgrade_cycle():
    """
    Validates Alembic migration 0042 on a disposable PostgreSQL database:
    1. Create disposable database
    2. Upgrade to 0042
    3. Verify table columns and constraints
    4. Downgrade to 0041
    5. Verify table is dropped
    6. Re-upgrade to 0042
    7. Drop disposable database
    """
    db_name = f"test_mig_0042_{uuid.uuid4().hex[:12]}"
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

        # 1. Upgrade to 0042
        await asyncio.to_thread(command.upgrade, cfg, "0042_canary_flight_authorizations")

        engine = create_async_engine(target_url, echo=False)
        async with engine.connect() as check_conn:
            # 2. Verify table exists and has expected columns
            res = await check_conn.execute(
                text(
                    "SELECT column_name, data_type, is_nullable "
                    "FROM information_schema.columns "
                    "WHERE table_name = 'canary_flight_authorizations'"
                )
            )
            columns = {row[0]: (row[1], row[2]) for row in res.fetchall()}
            assert "flight_name" in columns
            assert "environment" in columns
            assert "provider_name" in columns
            assert "allowed_domains_json" in columns
            assert "max_messages" in columns
            assert "dispatched_count" in columns
            assert "scope_fingerprint" in columns
            assert "status" in columns
            assert "execution_authorized" in columns
            assert "signer_1_user_id" in columns
            assert "signer_2_user_id" in columns
            assert "rejection_reason" in columns
            assert "revocation_reason" in columns

            # Verify indexes
            idx_res = await check_conn.execute(
                text(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE tablename = 'canary_flight_authorizations'"
                )
            )
            indexes = {row[0] for row in idx_res.fetchall()}
            assert "ix_canary_flight_status_expires" in indexes
            assert "ix_canary_flight_fingerprint" in indexes

        await engine.dispose()

        # 3. Downgrade to 0041
        await asyncio.to_thread(command.downgrade, cfg, "0041_supervisor_dispatch_delivery_events")

        engine2 = create_async_engine(target_url, echo=False)
        async with engine2.connect() as check_conn2:
            res_down = await check_conn2.execute(
                text(
                    "SELECT EXISTS ("
                    "  SELECT FROM information_schema.tables "
                    "  WHERE table_name = 'canary_flight_authorizations'"
                    ")"
                )
            )
            assert res_down.scalar() is False
        await engine2.dispose()

        # 4. Re-upgrade to 0042
        await asyncio.to_thread(command.upgrade, cfg, "0042_canary_flight_authorizations")

        engine3 = create_async_engine(target_url, echo=False)
        async with engine3.connect() as check_conn3:
            res_up = await check_conn3.execute(
                text(
                    "SELECT EXISTS ("
                    "  SELECT FROM information_schema.tables "
                    "  WHERE table_name = 'canary_flight_authorizations'"
                    ")"
                )
            )
            assert res_up.scalar() is True
        await engine3.dispose()

    finally:
        settings.database_url = old_db_url
        admin_conn = await asyncpg.connect(PG_ADMIN_URL)
        await admin_conn.execute(
            f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{db_name}'"
        )
        await admin_conn.execute(f"DROP DATABASE IF EXISTS {db_name}")
        await admin_conn.close()
