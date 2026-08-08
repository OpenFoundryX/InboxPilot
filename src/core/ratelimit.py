"""Fixed-window rate limiting over the shared async Redis client.

Written for the public booking endpoints, which are the first thing in this app
reachable without a session. Every request there costs a Google API call or
sends real mail from a customer's account, so "anyone may call this as fast as
they like" is not an option.

Fixed windows, not a sliding log: a burst straddling a boundary can briefly
pass up to 2x the limit, which is irrelevant when the limit exists to stop
scripted abuse rather than to meter a paid quota, and it costs one INCR instead
of a sorted set per caller.
"""

from core.logging import get_logger
from core.redis import redis_client

log = get_logger(__name__)


async def allow(key: str, *, limit: int, window_seconds: int) -> bool:
    """Count one hit against `key`; False once the window's limit is spent.

    Fails **open**. If Redis is unreachable the choice is between refusing every
    booking on the platform and briefly not rate-limiting, and a scheduling
    product that cannot take a booking is broken in a way that abuse is not.
    The lost protection is logged so it is visible rather than silent.
    """
    try:
        bucket = f"ratelimit:{key}"
        used = await redis_client.incr(bucket)
        if used == 1:
            await redis_client.expire(bucket, window_seconds)
        return used <= limit
    except Exception:
        log.warning("ratelimit.unavailable", key=key)
        return True


async def remaining(key: str, *, limit: int) -> int:
    """Hits left in the current window — for tests and diagnostics."""
    try:
        used = int(await redis_client.get(f"ratelimit:{key}") or 0)
    except Exception:
        return limit
    return max(0, limit - used)
