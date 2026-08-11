from collections.abc import AsyncGenerator
from functools import lru_cache

import redis as sync_redis_lib
from redis.asyncio import Redis, from_url

from core.config import settings

redis_client: Redis = from_url(settings.REDIS_URL, decode_responses=True)


async def get_redis() -> AsyncGenerator[Redis, None]:
    """FastAPI dependency yielding the shared Redis client."""
    yield redis_client


@lru_cache(maxsize=1)
def sync_redis() -> sync_redis_lib.Redis:
    """The blocking Redis client, for Celery tasks and the integration layer.

    The async client above is bound to the API's event loop and cannot be used
    from synchronous worker code. Built lazily so a forked Celery child opens
    its own connection rather than inheriting the parent's socket.
    """
    return sync_redis_lib.Redis.from_url(settings.REDIS_URL, decode_responses=True)
