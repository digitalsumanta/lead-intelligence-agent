"""Shared in-memory database fixtures."""
import sys
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
