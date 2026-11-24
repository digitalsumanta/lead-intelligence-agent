"""Shared in-memory database fixtures."""
import os
import sys

# Keep the developer's local .env (live dispatch, cloud AI providers, API keys) out of tests.
# Must be set before anything imports app.config.
os.environ["LEAD_AGENT_DISABLE_ENV_FILE"] = "1"
# Deterministic email-compliance configuration (unsubscribe links + postal address) for tests.
os.environ.setdefault("OUTREACH_UNSUBSCRIBE_SECRET", "test-unsubscribe-secret-0123456789abcdef")
os.environ.setdefault("OUTREACH_UNSUBSCRIBE_BASE_URL", "https://leads.test.invalid")
os.environ.setdefault("OUTREACH_SENDER_BUSINESS_NAME", "Test Sender Co")
os.environ.setdefault("OUTREACH_SENDER_POSTAL_ADDRESS", "1 Test Street, Test City 000000")
# Fixed Fernet key so stored provider credentials are encrypted in tests too.
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", "dGVzdC1jcmVkZW50aWFsLWVuY3J5cHRpb24ta2V5MDE=")
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool
from app.models import Base

class _TrackingSessionFactory:
    """async_sessionmaker wrapper that keeps every session alive until test teardown.

    Many tests override get_db with ``lambda: session_factory()`` and never close the session.
    If such a session is garbage-collected mid-test, SQLAlchemy terminates its connection -
    with StaticPool that is the one connection holding the in-memory database, which then
    vanishes ("no such table"). Holding strong references prevents that timing-dependent
    failure; all sessions are closed when the test ends.
    """

    def __init__(self, maker: async_sessionmaker) -> None:
        self._maker = maker
        self.sessions: list = []

    def __call__(self, *args, **kwargs):
        session = self._maker(*args, **kwargs)
        self.sessions.append(session)
        return session

    def __getattr__(self, name):
        return getattr(self._maker, name)

    async def close_all(self) -> None:
        for session in self.sessions:
            try:
                await session.close()
            except Exception:
                pass
        self.sessions.clear()


@pytest_asyncio.fixture
async def session_factory():
    """Create an isolated SQLite database per test."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn: await conn.run_sync(Base.metadata.create_all)
    factory = _TrackingSessionFactory(async_sessionmaker(engine, expire_on_commit=False))
    yield factory
    await factory.close_all()
    await engine.dispose()


@pytest.fixture(autouse=True)
def reset_dependency_overrides():
    """Ensure FastAPI dependency overrides do not leak across test boundaries."""
    from app.main import app
    yield
    app.dependency_overrides.clear()
