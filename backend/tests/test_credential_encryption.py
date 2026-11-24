"""Encryption at rest for stored provider credentials (api_keys.key)."""
import argparse

import pytest
from sqlalchemy import text

from app.config import settings
from app.models import ApiKey
from app.services import credential_crypto as cc
from app.services.key_pool import ApiKeyPool

SECRET = "AIzaSy-test-provider-credential-123456"


def test_round_trip_and_ciphertext_hides_value():
    stored = cc.encrypt(SECRET)
    assert stored.startswith("enc:v1:") and SECRET not in stored
    assert cc.decrypt(stored) == SECRET
    assert cc.encrypt(SECRET) != stored  # random IV per encryption
    assert cc.encrypt(stored) == stored  # never double-encrypts


def test_legacy_plaintext_passes_through_until_migrated():
    assert cc.decrypt("legacy-plain-key") == "legacy-plain-key"


def test_missing_or_invalid_key_fails_closed(monkeypatch):
    monkeypatch.setattr(settings, "credential_encryption_key", "")
    with pytest.raises(cc.CredentialEncryptionError, match="not set"):
        cc.encrypt(SECRET)
    monkeypatch.setattr(settings, "credential_encryption_key", "not-a-fernet-key")
    with pytest.raises(cc.CredentialEncryptionError, match="invalid"):
        cc.encrypt(SECRET)


def test_wrong_key_cannot_decrypt():
    stored = cc.encrypt(SECRET, raw_keys=cc.generate_key())
    with pytest.raises(cc.CredentialEncryptionError, match="could not be decrypted"):
        cc.decrypt(stored, raw_keys=cc.generate_key())


def test_rotation_keeps_old_values_readable_and_reencrypts_with_primary():
    old_key, new_key = cc.generate_key(), cc.generate_key()
    stored = cc.encrypt(SECRET, raw_keys=old_key)
    both = f"{new_key},{old_key}"
    assert cc.decrypt(stored, raw_keys=both) == SECRET
    rotated = cc.rotate(stored, raw_keys=both)
    assert cc.decrypt(rotated, raw_keys=new_key) == SECRET  # old key no longer needed
    assert cc.decrypt(cc.rotate("plain", raw_keys=new_key), raw_keys=new_key) == "plain"


@pytest.mark.asyncio
async def test_orm_stores_ciphertext_and_returns_plaintext(session_factory):
    async with session_factory() as db:
        db.add(ApiKey(provider="gemini", key=SECRET))
        await db.commit()
        raw = (await db.execute(text("SELECT key FROM api_keys"))).scalar_one()
        assert raw.startswith("enc:v1:") and SECRET not in raw

    async with session_factory() as db:
        lease = await ApiKeyPool().lease(db, "gemini", "generate")
        assert lease is not None and lease.credential == SECRET
        assert lease.fingerprint == ApiKeyPool.fingerprint(SECRET)


@pytest.mark.asyncio
async def test_environment_key_sync_is_idempotent_with_encryption(session_factory, monkeypatch):
    from app.services.key_config import sync_environment_keys
    monkeypatch.setattr(type(settings), "provider_key_map", property(lambda self: {"gemini": [SECRET]}))
    async with session_factory() as db:
        await sync_environment_keys(db)
        await sync_environment_keys(db)
        assert (await db.execute(text("SELECT count(*) FROM api_keys"))).scalar_one() == 1


@pytest.mark.asyncio
async def test_legacy_plaintext_row_is_readable_then_reencrypted_by_cli(session_factory, monkeypatch, capsys):
    from app import cli
    async with session_factory() as db:
        await db.execute(text("INSERT INTO api_keys (id, provider, key, status, enabled, failure_count, success_count) "
                              "VALUES ('00000000000000000000000000000001', 'gemini', :k, 'ACTIVE', 1, 0, 0)"), {"k": SECRET})
        await db.commit()

    async def run(*argv):
        return await cli._run(cli.build_parser().parse_args(list(argv)), session_factory)

    assert await run("keys", "status") == 0
    assert "0 encrypted, 1 plaintext" in capsys.readouterr().out
    assert await run("keys", "reencrypt") == 0
    assert await run("keys", "status") == 0
    assert "1 encrypted, 0 plaintext" in capsys.readouterr().out

    # Rotate: new primary key, old key kept for decryption, re-encrypt, then drop the old key.
    old_key = settings.credential_encryption_key
    new_key = cc.generate_key()
    monkeypatch.setattr(settings, "credential_encryption_key", f"{new_key},{old_key}")
    assert await run("keys", "reencrypt") == 0
    monkeypatch.setattr(settings, "credential_encryption_key", new_key)
    async with session_factory() as db:
        lease = await ApiKeyPool().lease(db, "gemini", "generate")
        assert lease.credential == SECRET


def test_cli_generate_prints_valid_key(capsys):
    import asyncio
    from app import cli
    assert asyncio.run(cli._run(cli.build_parser().parse_args(["keys", "generate"]), session_factory=object())) == 0
    key = capsys.readouterr().out.strip()
    assert cc.decrypt(cc.encrypt("x", raw_keys=key), raw_keys=key) == "x"
