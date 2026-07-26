import asyncio
from collections.abc import AsyncGenerator, Coroutine
from typing import Any, TypeVar

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from core.config import settings

T = TypeVar("T")

# FastAPI engine: long-lived, pooled (the app runs a single event loop).
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.SQL_ECHO,
    pool_pre_ping=True,
)

SessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding an async session with commit/rollback."""
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def run_async(coro: Coroutine[Any, Any, T]) -> T:
    """Run an async coroutine to completion from sync code (e.g. a Celery task).

    Celery tasks are synchronous, but our DB layer is async. Each call gets its
    own event loop; DB access inside must use `worker_session`, which builds a
    NullPool engine bound to *this* loop and disposes it — otherwise a pooled
    connection from an earlier loop leaks in and asyncpg raises "attached to a
    different loop".
    """
    return asyncio.run(coro)


async def with_worker_session(fn: "Any") -> T:
    """Run `fn(session)` against a fresh, loop-local NullPool session.

    Creates and disposes a dedicated engine per call so no connection is ever
    reused across event loops. Commits on success, rolls back on error.
    """
    worker_engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool)
    factory = async_sessionmaker(
        bind=worker_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    try:
        async with factory() as session:
            try:
                result = await fn(session)
                await session.commit()
                return result
            except Exception:
                await session.rollback()
                raise
    finally:
        await worker_engine.dispose()
