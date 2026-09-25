"""Idempotently register configured provider keys in the protected key pool."""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.config import settings
from app.models import ApiKey

async def sync_environment_keys(session: AsyncSession) -> None:
    """Insert configured API keys without returning or logging their values."""
    for provider, keys in settings.provider_key_map.items():
        existing = set((await session.scalars(select(ApiKey.key).where(ApiKey.provider == provider))).all())
        session.add_all(ApiKey(provider=provider, key=key) for key in keys if key not in existing)
    await session.commit()
