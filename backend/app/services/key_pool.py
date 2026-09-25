"""Atomic provider-key leases with safe credential handling."""

from dataclasses import dataclass, field
from datetime import timedelta, timezone
import hashlib
import uuid

from sqlalchemy import case, literal, not_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.domain.enrichment import ProviderErrorType
from app.models import ApiKey, ProviderOperationState
from app.models.entities import now_utc


@dataclass(frozen=True)
class ApiKeyLease:
    """A time-bounded key claim; credential is excluded from repr/logs."""

    id: uuid.UUID
    provider: str
    operation: str
    fingerprint: str
    token: str
    credential: str = field(repr=False)


class ApiKeyPool:
    """Lease provider credentials atomically and track per-key health state."""

    @staticmethod
    def fingerprint(credential: str) -> str:
        """Return a stable, non-reversible identifier suitable for usage logs."""
        return hashlib.sha256(credential.encode("utf-8")).hexdigest()

    @staticmethod
    def _available_at(value, now) -> bool:
        if value is None:
            return True
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value <= now

    async def lease(self, session: AsyncSession, provider: str, operation: str, lease_seconds: int = 300) -> ApiKeyLease | None:
        """Atomically lease one eligible provider/operation key.

        PostgreSQL uses row-level ``SKIP LOCKED`` in the candidate subquery.
        Other supported dialects use one conditional UPDATE ... RETURNING,
        making candidate selection and lease acquisition a single write.
        """
        now = now_utc()
        eligible = [
            ApiKey.provider == provider,
            ApiKey.enabled.is_(True),
            ApiKey.status == "ACTIVE",
            or_(ApiKey.operation_scope.is_(None), ApiKey.operation_scope == operation),
            or_(ApiKey.cooldown_until.is_(None), ApiKey.cooldown_until <= now),
            or_(ApiKey.quota_exhausted_until.is_(None), ApiKey.quota_exhausted_until <= now),
            or_(ApiKey.lease_expires_at.is_(None), ApiKey.lease_expires_at <= now),
            not_(
                select(literal(1))
                .select_from(ProviderOperationState)
                .where(
                    ProviderOperationState.provider == provider,
                    ProviderOperationState.operation == operation,
                    or_(
                        ProviderOperationState.cooldown_until > now,
                        ProviderOperationState.quota_exhausted_until > now,
                    ),
                )
                .exists()
            ),
        ]
        candidate_query = select(ApiKey.id).where(*eligible)
        if session.get_bind().dialect.name == "postgresql":
            candidate_query = candidate_query.with_for_update(skip_locked=True)
        if operation:
            candidate_query = candidate_query.order_by(
                case((ApiKey.operation_scope == operation, 0), else_=1),
                ApiKey.last_used_at.asc().nullsfirst(),
                ApiKey.id,
            )
        else:
            candidate_query = candidate_query.order_by(ApiKey.last_used_at.asc().nullsfirst(), ApiKey.id)
        candidate_id = candidate_query.limit(1).scalar_subquery()
        token = str(uuid.uuid4())
        statement = (
            update(ApiKey)
            .where(ApiKey.id == candidate_id, *eligible)
            .values(
                lease_token=token,
                lease_expires_at=now + timedelta(seconds=max(1, lease_seconds)),
                last_used_at=now,
            )
            .returning(ApiKey.id)
        )
        key_id = (await session.execute(statement)).scalar_one_or_none()
        await session.commit()
        if key_id is None:
            return None
        key = await session.get(ApiKey, key_id)
        if key is None:
            return None
        return ApiKeyLease(
            id=key.id,
            provider=key.provider,
            operation=operation,
            fingerprint=self.fingerprint(key.key),
            token=token,
            credential=key.key,
        )

    async def select(self, session: AsyncSession, provider: str, operation: str | None = None) -> ApiKey | None:
        """Compatibility selection API; returned row now holds a lease."""
        lease = await self.lease(session, provider, operation or "")
        if lease is None:
            return None
        key = await session.get(ApiKey, lease.id)
        if key is not None:
            await session.refresh(key)
        return key

    async def release_success(self, session: AsyncSession, lease: ApiKeyLease) -> None:
        """Record success and release only the lease owned by this request."""
        await session.execute(
            update(ApiKey)
            .where(ApiKey.id == lease.id, ApiKey.lease_token == lease.token)
            .values(
                success_count=ApiKey.success_count + 1,
                last_error=None,
                cooldown_until=None,
                quota_exhausted_until=None,
                lease_token=None,
                lease_expires_at=None,
            )
        )
        await session.commit()

    async def release_failure(
        self,
        session: AsyncSession,
        lease: ApiKeyLease,
        error_type: ProviderErrorType,
        *,
        cooldown_seconds: float = 0,
        retry_after_seconds: float | None = None,
        quota_cooldown_seconds: int = 3600,
        apply_key_cooldown: bool = True,
    ) -> None:
        """Record a categorized failure without retaining exception text."""
        now = now_utc()
        cooldown = max(cooldown_seconds, retry_after_seconds or 0)
        values = {
            "failure_count": ApiKey.failure_count + 1,
            "last_failure_at": now,
            "last_error": error_type.value,
            "lease_token": None,
            "lease_expires_at": None,
        }
        if error_type == ProviderErrorType.AUTHENTICATION:
            values.update(enabled=False, status="DISABLED")
        elif error_type == ProviderErrorType.QUOTA_EXHAUSTED and apply_key_cooldown:
            values.update(quota_exhausted_until=now + timedelta(seconds=quota_cooldown_seconds))
        elif error_type == ProviderErrorType.RATE_LIMITED and apply_key_cooldown:
            values.update(cooldown_until=now + timedelta(seconds=cooldown))
        elif cooldown > 0 and apply_key_cooldown:
            values.update(cooldown_until=now + timedelta(seconds=cooldown))
        await session.execute(
            update(ApiKey)
            .where(ApiKey.id == lease.id, ApiKey.lease_token == lease.token)
            .values(**values)
        )
        await session.commit()

    async def provider_operation_hold(self, session: AsyncSession, provider: str, operation: str):
        """Return a provider-wide rate/quota hold, if active."""
        state = await session.get(ProviderOperationState, (provider, operation))
        if state is None:
            return None
        now = now_utc()
        if not self._available_at(state.quota_exhausted_until, now):
            until = state.quota_exhausted_until
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
            return ProviderErrorType.QUOTA_EXHAUSTED, max(0, (until - now).total_seconds())
        if not self._available_at(state.cooldown_until, now):
            until = state.cooldown_until
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
            return ProviderErrorType.RATE_LIMITED, max(0, (until - now).total_seconds())
        return None

    async def set_provider_operation_hold(
        self,
        session: AsyncSession,
        provider: str,
        operation: str,
        error_type: ProviderErrorType,
        *,
        cooldown_seconds: float = 0,
        retry_after_seconds: float | None = None,
        quota_cooldown_seconds: int = 3600,
    ) -> None:
        """Atomically extend shared provider/operation rate-limit or quota state."""
        now = now_utc()
        cooldown_until = None
        quota_until = None
        if error_type == ProviderErrorType.RATE_LIMITED:
            cooldown_until = now + timedelta(seconds=max(cooldown_seconds, retry_after_seconds or 0))
        elif error_type == ProviderErrorType.QUOTA_EXHAUSTED:
            quota_until = now + timedelta(seconds=quota_cooldown_seconds)
        else:
            return
        values = {
            "provider": provider,
            "operation": operation,
            "cooldown_until": cooldown_until,
            "quota_exhausted_until": quota_until,
            "last_failure_at": now,
            "created_at": now,
            "updated_at": now,
        }
        dialect = session.get_bind().dialect.name
        if dialect == "postgresql":
            insert_stmt = pg_insert(ProviderOperationState).values(**values)
        elif dialect == "sqlite":
            insert_stmt = sqlite_insert(ProviderOperationState).values(**values)
        else:
            state = await session.get(ProviderOperationState, (provider, operation))
            if state is None:
                session.add(ProviderOperationState(**values))
            else:
                state.cooldown_until = cooldown_until or state.cooldown_until
                state.quota_exhausted_until = quota_until or state.quota_exhausted_until
                state.last_failure_at = now
                state.updated_at = now
            await session.commit()
            return

        table = ProviderOperationState.__table__
        excluded = insert_stmt.excluded
        updates = {"last_failure_at": now, "updated_at": now}
        for name in ("cooldown_until", "quota_exhausted_until"):
            incoming = getattr(excluded, name)
            existing = getattr(table.c, name)
            if name == "cooldown_until" and cooldown_until is None:
                continue
            if name == "quota_exhausted_until" and quota_until is None:
                continue
            updates[name] = case((or_(existing.is_(None), existing < incoming), incoming), else_=existing)
        await session.execute(insert_stmt.on_conflict_do_update(index_elements=["provider", "operation"], set_=updates))
        await session.commit()

    async def mark_success(self, session: AsyncSession, key: ApiKey) -> None:
        """Legacy ORM-row success hook retained for existing callers."""
        key.success_count += 1
        key.last_error = None
        key.cooldown_until = None
        key.quota_exhausted_until = None
        key.lease_token = None
        key.lease_expires_at = None
        await session.commit()

    async def mark_failure(self, session: AsyncSession, key: ApiKey, error: str, cooldown_seconds: int = 60) -> None:
        """Legacy failure hook; persist only a safe category, never exception text."""
        key.failure_count += 1
        key.last_failure_at = now_utc()
        key.last_error = ProviderErrorType.TRANSIENT.value
        key.cooldown_until = now_utc() + timedelta(seconds=cooldown_seconds)
        key.lease_token = None
        key.lease_expires_at = None
        await session.commit()

    async def health(self, session: AsyncSession, provider: str) -> dict:
        """Return counts only; never serialize credential or lease data."""
        keys = (await session.scalars(select(ApiKey).where(ApiKey.provider == provider))).all()
        now = now_utc()
        healthy = sum(
            key.enabled
            and key.status == "ACTIVE"
            and self._available_at(key.cooldown_until, now)
            and self._available_at(key.quota_exhausted_until, now)
            for key in keys
        )
        return {"configured": len(keys), "healthy": healthy}

    async def execute_with_rotation(self, session: AsyncSession, provider: str, operation, max_keys: int = 3):
        """Backward-compatible bounded rotation helper."""
        failures = []
        for _ in range(max_keys):
            lease = await self.lease(session, provider, "legacy", lease_seconds=300)
            if lease is None:
                # Existing callers historically used unscoped keys.
                lease = await self.lease(session, provider, "", lease_seconds=300)
            if lease is None:
                break
            try:
                result = await operation(lease.credential)
                await self.release_success(session, lease)
                return result
            except Exception:
                failures.append("transient")
                await self.release_failure(session, lease, ProviderErrorType.TRANSIENT, cooldown_seconds=0)
        raise RuntimeError("Provider key operation failed" if failures else "No healthy provider keys are configured")
