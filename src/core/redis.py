from collections.abc import AsyncGenerator

from redis.asyncio import Redis, from_url

from core.config import settings

redis_client: Redis = from_url(settings.REDIS_URL, decode_responses=True)


async def get_redis() -> AsyncGenerator[Redis, None]:
    """FastAPI dependency yielding the shared Redis client."""
    yield redis_client
