"""Redis guards for webhook-driven email processing.

Three distinct hazards, three key spaces:

* `evt:` — Composio delivers at-least-once, and a message that gets re-labelled
  (e.g. Mailman releasing a batch adds INBOX back) can re-match the trigger
  query. Claiming an event id makes redelivery a no-op.
* `ours:` — our own replies land back in the mailbox as new messages matching
  the trigger query. Gmail label propagation is too slow to rely on as the loop
  guard, so we record every message id we create the moment the send returns.
* `reply:` — a circuit breaker. If both guards above somehow fail, the blast
  radius is unbounded outbound email; this caps it.

Reads happen in the async API process, writes in sync Celery workers, so both
clients are here. Errors are deliberately NOT swallowed: the webhook layer
chooses fail-open or fail-closed per path.
"""

from functools import lru_cache

import redis
from redis.asyncio import Redis as AsyncRedis

from core.config import settings
from core.redis import redis_client

EVENT_TTL = 24 * 60 * 60
OURS_TTL = 60 * 60
REPLY_WINDOW = 60 * 60
MAX_REPLIES_PER_THREAD = 5


def _aio() -> AsyncRedis:
    return redis_client


@lru_cache(maxsize=1)
def _sync() -> redis.Redis:
    return redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)


async def claim_event(user_id: str, message_id: str) -> bool:
    """Claim a trigger event. False means it was already handled."""
    return bool(await _aio().set(f"evt:{user_id}:{message_id}", "1", nx=True, ex=EVENT_TTL))


async def is_ours(message_id: str) -> bool:
    """Whether this message is one InboxOS itself sent."""
    return await _aio().get(f"ours:{message_id}") is not None


def remember_ours(message_id: str) -> None:
    """Record a message we just sent, so its trigger event is ignored."""
    _sync().set(f"ours:{message_id}", "1", ex=OURS_TTL)


def allow_reply(thread_id: str) -> bool:
    """Whether we may still reply in this thread, or have hit the hourly cap."""
    key = f"reply:{thread_id}"
    count = _sync().incr(key)
    if count == 1:
        _sync().expire(key, REPLY_WINDOW)
    return count <= MAX_REPLIES_PER_THREAD
