"""Test fixtures.

Chat models use Postgres-specific column types (JSONB, UUID), so DB-backed
tests need a real Postgres — the one from docker-compose is fine. Tables are
created directly from the metadata (no Alembic) against a dedicated database,
and everything is dropped afterwards.

When no server is reachable the DB fixtures skip rather than fail, so the
DB-free unit tests (engine, describe, sources) still run anywhere.
"""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from core.config import settings
from models.base import Base
from models.users import User

# Import every model module so Base.metadata is complete before create_all.
import models.auth  # noqa: F401
import models.chat  # noqa: F401
import models.mailman  # noqa: F401
import models.reminders  # noqa: F401
import models.routines  # noqa: F401


def _test_db_url() -> str:
    """The configured database with a `_test` suffix."""
    base, _, name = settings.DATABASE_URL.rpartition("/")
    return f"{base}/{name}_test"


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine(_test_db_url(), poolclass=NullPool)
    try:
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except Exception as exc:  # server down, or the _test database is missing
        await eng.dispose()
        pytest.skip(
            f"Postgres test database unavailable ({exc.__class__.__name__}). "
            f"Run: docker compose up -d db && "
            f'docker compose exec db psql -U inboxos_user -d inboxos '
            f'-c "CREATE DATABASE inboxos_test OWNER inboxos_user;"'
        )
    yield eng
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await eng.dispose()


@pytest_asyncio.fixture
async def db(engine):
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    async with factory() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture
async def user(db) -> User:
    row = User(email=f"user-{uuid.uuid4().hex[:8]}@example.com", full_name="Test User")
    db.add(row)
    await db.flush()
    return row


@pytest_asyncio.fixture
async def other_user(db) -> User:
    row = User(email=f"other-{uuid.uuid4().hex[:8]}@example.com", full_name="Other User")
    db.add(row)
    await db.flush()
    return row
