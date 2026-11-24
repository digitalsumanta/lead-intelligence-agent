"""PostgreSQL test for data migration 0045: encrypt plaintext api_keys.key values."""
from __future__ import annotations

import asyncio
import uuid

import asyncpg
import pytest
from alembic import command
from alembic.config import Config

from app.config import settings
from app.services import credential_crypto as cc

PG = "postgresql://postgres:postgres@localhost:5432"
SECRET = "plaintext-provider-key-for-migration"


async def _keys(db_name: str) -> list[str]:
    conn = await asyncpg.connect(f"{PG}/{db_name}")
    try:
        return [r["key"] for r in await conn.fetch("SELECT key FROM api_keys")]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_migration_0045_encrypts_and_downgrade_decrypts(monkeypatch):
    db_name = f"test_mig_0045_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(f"{PG}/postgres")
    await admin.execute(f"CREATE DATABASE {db_name}")
    await admin.close()
    conn = await asyncpg.connect(f"{PG}/{db_name}")
    await conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(128) PRIMARY KEY)")
    await conn.close()

    old_url = settings.database_url
    settings.database_url = f"postgresql+asyncpg://postgres:postgres@localhost:5432/{db_name}"
    try:
        cfg = Config("backend/alembic.ini")
        cfg.set_main_option("script_location", "backend/migrations")
        await asyncio.to_thread(command.upgrade, cfg, "0044_app_users_and_sessions")
        conn = await asyncpg.connect(f"{PG}/{db_name}")
        await conn.execute("INSERT INTO api_keys (id, provider, key, status, enabled, failure_count, success_count) "
                           "VALUES ($1, 'gemini', $2, 'ACTIVE', true, 0, 0)", uuid.uuid4(), SECRET)
        await conn.close()

        # Refuses to run without a usable encryption key while plaintext rows exist.
        monkeypatch.setattr(settings, "credential_encryption_key", "")
        with pytest.raises(RuntimeError, match="CREDENTIAL_ENCRYPTION_KEY"):
            await asyncio.to_thread(command.upgrade, cfg, "0045_encrypt_api_keys")
        assert await _keys(db_name) == [SECRET]
        monkeypatch.undo()

        await asyncio.to_thread(command.upgrade, cfg, "0045_encrypt_api_keys")
        [stored] = await _keys(db_name)
        assert stored.startswith("enc:v1:") and cc.decrypt(stored) == SECRET

        await asyncio.to_thread(command.downgrade, cfg, "0044_app_users_and_sessions")
        assert await _keys(db_name) == [SECRET]
    finally:
        settings.database_url = old_url
        admin = await asyncpg.connect(f"{PG}/postgres")
        await admin.execute(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '{db_name}'")
        await admin.execute(f"DROP DATABASE IF EXISTS {db_name}")
        await admin.close()
