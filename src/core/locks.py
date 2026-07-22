"""Lightweight Redis-based mutual-exclusion for Celery beat tasks.

Beat can fire a task again before the previous run finishes (a sweep taking
>interval, or a manual run overlapping the scheduled one). For tasks with
external side effects (sending email, creating filters) that double-execution is
harmful, so they run inside `single_run` to guarantee only one at a time.
"""

from contextlib import contextmanager
from functools import lru_cache

import redis

from core.config import settings


@lru_cache(maxsize=1)
def _redis() -> redis.Redis:
    return redis.Redis.from_url(settings.REDIS_URL)


@contextmanager
def single_run(name: str, ttl: int = 300):
    """Run the block only if the named lock is free; else yield False.

    The lock auto-expires after `ttl` seconds so a crashed run can't wedge it.
    """
    key = f"lock:{name}"
    token = _redis().set(key, "1", nx=True, ex=ttl)
    if not token:
        yield False
        return
    try:
        yield True
    finally:
        _redis().delete(key)
