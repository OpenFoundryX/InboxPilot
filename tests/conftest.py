"""Test harness.

Every test runs inside a transaction that is rolled back afterwards, so the
suite can point at the ordinary development database without leaving rows
behind. The session joins the outer transaction as a SAVEPOINT, which is what
keeps service code that calls `commit()` from ending the isolation early.
"""

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from core.config import settings
from tests.factories import make_user


@pytest_asyncio.fixture(scope="session")
async def engine():
    # poolclass=None keeps SQLAlchemy's default pooled connection pool. That's
    # a deliberate choice, not an oversight: this engine is session-scoped and
    # long-lived (unlike core/database.py's worker_session, which uses
    # NullPool because it builds a fresh, short-lived engine per Celery task
    # to avoid handing a pooled connection to a different event loop). The
    # whole pytest session shares one event loop (see
    # asyncio_default_test_loop_scope in pyproject.toml), so pooling here is
    # safe and lets `connect()` reuse connections across tests instead of
    # opening a new one every time.
    eng = create_async_engine(settings.DATABASE_URL, poolclass=None)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def db(engine) -> AsyncSession:
    async with engine.connect() as connection:
        transaction = await connection.begin()
        factory = async_sessionmaker(
            bind=connection,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
            join_transaction_mode="create_savepoint",
        )
        async with factory() as session:
            yield session
        await transaction.rollback()


@pytest_asyncio.fixture
async def user(db):
    return await make_user(db)
