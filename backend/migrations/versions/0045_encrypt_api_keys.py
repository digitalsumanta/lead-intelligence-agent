"""0045_encrypt_api_keys.py

Revision ID: 0045_encrypt_api_keys
Revises: 0044_app_users_and_sessions
Create Date: 2026-11-24 18:00:00.000000

Data migration: encrypt existing plaintext provider credentials in api_keys.key with
CREDENTIAL_ENCRYPTION_KEY. No schema change (the column stays TEXT).
"""
from alembic import op
import sqlalchemy as sa


revision = "0045_encrypt_api_keys"
down_revision = "0044_app_users_and_sessions"
branch_labels = None
depends_on = None

_PREFIX = "enc:v1:"


def upgrade() -> None:
    from app.services.credential_crypto import CredentialEncryptionError, encrypt

    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, key FROM api_keys WHERE key NOT LIKE :p"), {"p": _PREFIX + "%"}).fetchall()
    if not rows:
        return
    try:
        updates = [{"id": row.id, "key": encrypt(row.key)} for row in rows]
    except CredentialEncryptionError as exc:
        raise RuntimeError(
            f"{len(rows)} plaintext provider key(s) must be encrypted but CREDENTIAL_ENCRYPTION_KEY is not usable: {exc}"
        ) from None
    bind.execute(sa.text("UPDATE api_keys SET key = :key WHERE id = :id"), updates)


def downgrade() -> None:
    from app.services.credential_crypto import decrypt

    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, key FROM api_keys WHERE key LIKE :p"), {"p": _PREFIX + "%"}).fetchall()
    if rows:
        bind.execute(sa.text("UPDATE api_keys SET key = :key WHERE id = :id"),
                     [{"id": row.id, "key": decrypt(row.key)} for row in rows])
