"""Encryption at rest for stored provider credentials (``api_keys.key``).

Values are encrypted with Fernet (AES-128-CBC + HMAC-SHA256, from ``cryptography``) and stored
as ``enc:v1:<token>``. Configure ``CREDENTIAL_ENCRYPTION_KEY`` with one or more comma-separated
Fernet keys: the first encrypts, all of them decrypt, which allows key rotation:

1. Generate a new key:      python -m app.cli keys generate
2. Prepend it to CREDENTIAL_ENCRYPTION_KEY (keep the old key after it) and restart.
3. Re-encrypt stored keys:  python -m app.cli keys reencrypt
4. Remove the old key from CREDENTIAL_ENCRYPTION_KEY.

The encryption key must live outside the database (environment / secret manager) so that a
database dump or backup does not expose usable provider credentials.
"""
from __future__ import annotations

from typing import Optional

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy.types import Text, TypeDecorator

from app.config import settings

PREFIX = "enc:v1:"


class CredentialEncryptionError(RuntimeError):
    """Raised when credentials cannot be encrypted or decrypted with the configured keys."""


def generate_key() -> str:
    return Fernet.generate_key().decode("ascii")


def _cipher(raw_keys: Optional[str] = None) -> MultiFernet:
    raw = settings.credential_encryption_key if raw_keys is None else raw_keys
    keys = [k.strip() for k in (raw or "").split(",") if k.strip()]
    if not keys:
        raise CredentialEncryptionError(
            "CREDENTIAL_ENCRYPTION_KEY is not set; provider API keys cannot be stored or read. "
            "Generate one with: python -m app.cli keys generate"
        )
    try:
        return MultiFernet([Fernet(k.encode("ascii")) for k in keys])
    except (ValueError, TypeError) as exc:
        raise CredentialEncryptionError("CREDENTIAL_ENCRYPTION_KEY contains an invalid Fernet key") from exc


def is_encrypted(value: Optional[str]) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt(plaintext: str, raw_keys: Optional[str] = None) -> str:
    if is_encrypted(plaintext):
        return plaintext
    return PREFIX + _cipher(raw_keys).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(value: str, raw_keys: Optional[str] = None) -> str:
    if not is_encrypted(value):
        # Legacy plaintext row (encrypted by migration 0045 / `keys reencrypt`).
        return value
    try:
        return _cipher(raw_keys).decrypt(value[len(PREFIX):].encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise CredentialEncryptionError(
            "A stored provider credential could not be decrypted; CREDENTIAL_ENCRYPTION_KEY does not "
            "contain the key it was encrypted with"
        ) from exc


def rotate(value: str, raw_keys: Optional[str] = None) -> str:
    """Re-encrypt with the primary key (also encrypts legacy plaintext)."""
    if not is_encrypted(value):
        return encrypt(value, raw_keys)
    return PREFIX + _cipher(raw_keys).rotate(value[len(PREFIX):].encode("ascii")).decode("ascii")


class EncryptedText(TypeDecorator):
    """Text column that is transparently encrypted on write and decrypted on read."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return encrypt(str(value))

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return decrypt(value)
