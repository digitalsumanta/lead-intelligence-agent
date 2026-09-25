"""Database-backed round-robin key selection with per-key cooldown."""
from datetime import timedelta
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import ApiKey
from app.models.entities import now_utc

class ApiKeyPool:
    """Rotate provider credentials without ever returning them to API payloads."""
    async def select(self, session: AsyncSession, provider: str) -> ApiKey | None:
        """Select and mark the next healthy key for a provider."""
        now = now_utc()
        keys = (await session.scalars(select(ApiKey).where(ApiKey.provider == provider, ApiKey.status == "ACTIVE").order_by(ApiKey.last_used_at.asc().nullsfirst(), ApiKey.id))).all()
        eligible = [key for key in keys if key.cooldown_until is None or key.cooldown_until <= now]
        if not eligible: return None
        key = eligible[0]; key.last_used_at = now
        await session.commit()
        return key

    async def mark_success(self, session: AsyncSession, key: ApiKey) -> None:
        """Clear failure state after successful provider use."""
        key.success_count += 1; key.last_error = None; key.cooldown_until = None
        await session.commit()

    async def mark_failure(self, session: AsyncSession, key: ApiKey, error: str, cooldown_seconds: int = 60) -> None:
        """Cool a failing credential while allowing the pool to rotate."""
        key.failure_count += 1; key.last_error = error[:500]
        key.cooldown_until = now_utc() + timedelta(seconds=cooldown_seconds)
        await session.commit()

    async def health(self, session: AsyncSession, provider: str) -> dict:
        """Return key pool counts only; credential strings are never serialized."""
        keys = (await session.scalars(select(ApiKey).where(ApiKey.provider == provider))).all()
        now = now_utc()
        healthy = sum(k.status == "ACTIVE" and (k.cooldown_until is None or k.cooldown_until <= now) for k in keys)
        return {"configured": len(keys), "healthy": healthy}

    async def execute_with_rotation(self, session: AsyncSession, provider: str, operation, max_keys: int = 3):
        """Try distinct active keys in round-robin order, cooling each failure."""
        failures = []
        for _ in range(max_keys):
            key = await self.select(session, provider)
            if key is None: break
            try:
                result = await operation(key.key)
                await self.mark_success(session, key)
                return result
            except Exception as exc:
                failures.append(type(exc).__name__)
                await self.mark_failure(session, key, type(exc).__name__)
        raise RuntimeError("All available provider keys failed" if failures else "No healthy provider keys are configured")
