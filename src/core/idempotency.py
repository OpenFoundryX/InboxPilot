"""Redis guards for new-mail processing.

Three distinct hazards, three key spaces:

* `evt:` — mail arrives at-least-once. The Gmail history cursor can be replayed
  (two pollers racing, a retried task), and one message legitimately appears in
  several history records. Claiming an event id makes redelivery a no-op.
* `ours:` — our own replies land back in the mailbox as new messages the poller
  will see. Gmail label propagation is too slow to rely on as the loop guard, so
  we record every message id we create the moment the send returns.
* `reply:` — a circuit breaker. If both guards above somehow fail, the blast
  radius is unbounded outbound email; this caps it.

Both an async and a sync client live here: the API process is async, while the
poller and every Celery task are not. Errors are deliberately NOT swallowed —
callers choose fail-open or fail-closed per path.
"""

from redis.asyncio import Redis as AsyncRedis

from core.redis import redis_client, sync_redis

EVENT_TTL = 24 * 60 * 60
OURS_TTL = 60 * 60
REPLY_WINDOW = 60 * 60
MAX_REPLIES_PER_THREAD = 5


def _aio() -> AsyncRedis:
    return redis_client


def _sync():
    return sync_redis()


async def claim_event(user_id: str, message_id: str) -> bool:
    """Claim a new-mail event. False means it was already handled."""
    return bool(await _aio().set(f"evt:{user_id}:{message_id}", "1", nx=True, ex=EVENT_TTL))


async def is_ours(message_id: str) -> bool:
    """Whether this message is one InboxOS itself sent."""
    return await _aio().get(f"ours:{message_id}") is not None


def claim_event_sync(user_id: str, message_id: str) -> bool:
    """`claim_event` for synchronous callers (the mailbox poller, Celery tasks).

    Same key space and same TTL as the async version, deliberately: the whole
    point is that whichever process sees a message first claims it for both.
    """
    return bool(_sync().set(f"evt:{user_id}:{message_id}", "1", nx=True, ex=EVENT_TTL))


def is_ours_sync(message_id: str) -> bool:
    """`is_ours` for synchronous callers."""
    return _sync().get(f"ours:{message_id}") is not None


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
