import asyncio
from collections.abc import AsyncGenerator, Callable, Coroutine
from concurrent.futures import ThreadPoolExecutor
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


def run_worker_session(fn: "Callable[[AsyncSession], Coroutine[Any, Any, T]]") -> T:
    """`with_worker_session(fn)` from synchronous code, in any context.

    `run_async` is `asyncio.run`, which refuses to start when a loop is already
    running in this thread. That is not a hypothetical: most Celery tasks here
    are shaped as `run_async(with_worker_session(_handle))`, and `_handle` —
    already inside a loop — then calls the *synchronous* Gmail and Calendar
    wrappers. Those wrappers have to load the user's credentials, so the moment
    that lookup needed the database it began raising

        RuntimeError: asyncio.run() cannot be called from a running event loop

    from deep inside code with no idea it was under a loop at all.

    So: if no loop is running, take the cheap path. If one is, hand the work to
    a thread that has no loop of its own and block on the result.

    `fn` is passed rather than a coroutine deliberately. Building the coroutine
    here would create it in *this* thread and, on the threaded path, leave the
    original un-awaited — the "coroutine was never awaited" warning that
    accompanied the original failure.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return run_async(with_worker_session(fn))

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(with_worker_session(fn))).result()


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
