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
    Callers whose run can plausibly take longer than the default should pass a
    larger `ttl` explicitly — `workers.jobs.drafts_sweep.DRAFTS_LOCK` is one:
    its 300s default used to equal `drafts.sweep`'s own 300s beat interval, so
    a pass running close to its full TTL could let the lock expire mid-run and
    the next tick acquire concurrently, both spending the same stale quota
    snapshot.

    Known sharp edge, not fixed here: the `finally` block below deletes the
    key unconditionally, with no check that the caller releasing it is the one
    that acquired it. If a run does outlast its TTL, Redis will already have
    expired the key and let a second run acquire it; when the first (late,
    zombie) run finally finishes, its `delete` removes the *second* run's
    live lock, not its own already-expired one — so a third run could then
    start concurrently with the second. Closing that properly needs a
    fencing token (store a unique value on acquire, delete only if the key
    still holds that same value, e.g. via a Lua script for atomicity) rather
    than an unconditional delete. Not done here because it changes
    `single_run`'s semantics for every one of its callers, not just this one;
    raising the TTL is the narrow fix for the reported exactness bug.
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
