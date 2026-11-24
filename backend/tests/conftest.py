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

@pytest_asyncio.fixture
async def session_factory():
    """Create an isolated SQLite database per test."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn: await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture(autouse=True)
def reset_dependency_overrides():
    """Ensure FastAPI dependency overrides do not leak across test boundaries."""
    from app.main import app
    yield
    app.dependency_overrides.clear()
