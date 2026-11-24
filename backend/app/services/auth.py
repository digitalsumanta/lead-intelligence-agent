"""Dashboard user accounts and server-side sessions.

* Passwords: stdlib ``hashlib.scrypt`` (memory-hard), per-user random salt, constant-time compare.
* Sessions: random 256-bit token in an HttpOnly cookie; only its SHA-256 hash is stored, so a
  database leak does not reveal usable session tokens.
* Brute force: per-account lockout after repeated failures plus a per-IP attempt limiter.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.entities import USER_ROLES, AppUser, AuthSession, now_utc

MIN_PASSWORD_LENGTH = 12
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _SCRYPT_LEN = 2**14, 8, 1, 32
_LAST_SEEN_WRITE_INTERVAL = timedelta(minutes=5)


class AuthError(Exception):
    code = "AUTH_ERROR"

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class InvalidCredentialsError(AuthError):
    code = "INVALID_CREDENTIALS"


class AccountLockedError(AuthError):
    code = "ACCOUNT_LOCKED"


class TooManyAttemptsError(AuthError):
    code = "TOO_MANY_ATTEMPTS"


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------

def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P,
                            dklen=_SCRYPT_LEN, maxmem=64 * 1024 * 1024)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = encoded.split("$")
        if scheme != "scrypt":
            return False
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(digest_b64)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=int(n), r=int(r), p=int(p),
                                dklen=len(expected), maxmem=64 * 1024 * 1024)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# Used to spend equal time on unknown e-mail addresses (avoids user enumeration by timing).
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def validate_password_strength(password: str, email: str | None = None) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(password) > 256:
        raise ValueError("Password must be at most 256 characters")
    if email and password.lower() == email.lower():
        raise ValueError("Password must not be the e-mail address")
    if len(set(password)) < 5:
        raise ValueError("Password is too simple")


def normalize_user_email(email: str) -> str:
    value = (email or "").strip().lower()
    if "@" not in value or len(value) > 320 or " " in value:
        raise ValueError("A valid e-mail address is required")
    return value


def validate_role(role: str) -> str:
    value = (role or "").strip().lower()
    if value not in USER_ROLES:
        raise ValueError(f"Role must be one of: {', '.join(USER_ROLES)}")
    return value


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

async def get_user_by_email(session: AsyncSession, email: str) -> Optional[AppUser]:
    try:
        normalized = normalize_user_email(email)
    except ValueError:
        return None
    return await session.scalar(select(AppUser).where(AppUser.email == normalized))


async def create_user(session: AsyncSession, *, email: str, password: str, role: str = "viewer",
                      display_name: str | None = None) -> AppUser:
    normalized = normalize_user_email(email)
    validate_password_strength(password, normalized)
    if await get_user_by_email(session, normalized):
        raise ValueError("A user with this e-mail address already exists")
    user = AppUser(email=normalized, display_name=(display_name or None), role=validate_role(role),
                   password_hash=hash_password(password), is_active=True, created_at=now_utc(),
                   password_changed_at=now_utc())
    session.add(user)
    await session.flush()
    return user


async def set_password(session: AsyncSession, user: AppUser, password: str) -> None:
    """Change a password and sign the user out everywhere."""
    validate_password_strength(password, user.email)
    user.password_hash = hash_password(password)
    user.password_changed_at = now_utc()
    user.failed_login_count = 0
    user.locked_until = None
    await revoke_user_sessions(session, user.id)


# ---------------------------------------------------------------------------
# Login attempts
# ---------------------------------------------------------------------------

class LoginRateLimiter:
    """In-process sliding window limiter keyed by client IP (defense in depth on top of lockout)."""

    def __init__(self, max_attempts: int = 20, window_seconds: float = 600.0) -> None:
        self.max_attempts, self.window = max_attempts, window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            hits = self._hits.setdefault(key or "unknown", deque())
            while hits and now - hits[0] > self.window:
                hits.popleft()
            if len(hits) >= self.max_attempts:
                raise TooManyAttemptsError("Too many sign-in attempts. Please wait a few minutes and try again.")
            hits.append(now)
            if len(self._hits) > 10_000:  # bound memory
                self._hits = {k: v for k, v in self._hits.items() if v and now - v[-1] <= self.window}

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


login_rate_limiter = LoginRateLimiter()


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def authenticate(session: AsyncSession, email: str, password: str, *, now: datetime | None = None) -> AppUser:
    """Verify credentials, applying account lockout. Commits failure counters itself."""
    now = now or now_utc()
    user = await get_user_by_email(session, email)
    if user is None or not user.is_active:
        verify_password(password, _DUMMY_HASH)
        raise InvalidCredentialsError("Incorrect e-mail address or password")
    locked_until = _aware(user.locked_until)
    if locked_until and locked_until > now:
        raise AccountLockedError("This account is temporarily locked after repeated failed sign-ins. Try again later.")
    if not verify_password(password, user.password_hash):
        user.failed_login_count = (user.failed_login_count or 0) + 1
        if user.failed_login_count >= settings.auth_max_failed_logins:
            user.locked_until = now + timedelta(minutes=settings.auth_lockout_minutes)
            user.failed_login_count = 0
        await session.commit()
        raise InvalidCredentialsError("Incorrect e-mail address or password")
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = now
    return user


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ResolvedSession:
    user: AppUser
    session: AuthSession


async def create_session(session: AsyncSession, user: AppUser, *, user_agent: str | None = None,
                         ip_address: str | None = None, now: datetime | None = None) -> tuple[str, AuthSession]:
    now = now or now_utc()
    token = secrets.token_urlsafe(32)
    row = AuthSession(token_hash=_token_hash(token), user_id=user.id, created_at=now, last_seen_at=now,
                      expires_at=now + timedelta(hours=settings.auth_session_ttl_hours),
                      user_agent=(user_agent or None) and user_agent[:300], ip_address=(ip_address or None) and ip_address[:64])
    session.add(row)
    await session.flush()
    return token, row


async def resolve_session(session: AsyncSession, token: str, *, now: datetime | None = None) -> Optional[ResolvedSession]:
    if not token or len(token) > 200:
        return None
    now = now or now_utc()
    row = await session.scalar(select(AuthSession).where(AuthSession.token_hash == _token_hash(token)))
    if row is None or row.revoked_at is not None or _aware(row.expires_at) <= now:
        return None
    user = await session.get(AppUser, row.user_id)
    if user is None or not user.is_active:
        return None
    # Sessions created before the last password change are invalid.
    if _aware(user.password_changed_at) and _aware(row.created_at) < _aware(user.password_changed_at):
        return None
    if now - _aware(row.last_seen_at) > _LAST_SEEN_WRITE_INTERVAL:
        row.last_seen_at = now
        await session.commit()
    return ResolvedSession(user=user, session=row)


async def revoke_session(session: AsyncSession, token: str) -> None:
    row = await session.scalar(select(AuthSession).where(AuthSession.token_hash == _token_hash(token)))
    if row is not None and row.revoked_at is None:
        row.revoked_at = now_utc()


async def revoke_user_sessions(session: AsyncSession, user_id) -> None:
    await session.execute(
        update(AuthSession).where(AuthSession.user_id == user_id, AuthSession.revoked_at.is_(None))
        .values(revoked_at=now_utc())
    )
