"""The mail pipeline's on/off switch, asked from Celery.

`access.may_process_mail` is the rule and stays free of I/O; this is the loader
that puts the two rows in front of it for callers that only have a user id.

Every entry point that would touch a mailbox calls this — the onboarding sync,
the poller, the watch installer, the classifier. Four call sites rather than
one chokepoint because there is no single funnel: push, the reconciliation
beat, and the onboarding sync all reach Gmail by different routes.

It fails closed. A missing user, a missing subscription or an unreadable row
all mean "do not work in this mailbox", because the cost of wrongly proceeding
(labels written into an unpaid inbox, LLM spend on a locked account) is worse
than the cost of wrongly pausing, which a later tick corrects by itself.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from core.database import run_worker_session
from core.logging import get_logger
from models.users import User
from services.billing.access import may_process_mail
from services.billing.store import get_subscription

log = get_logger(__name__)

# Give the redirect that triggered this a moment to land before a worker starts
# calling Gmail on the same grant.
START_DELAY_SECONDS = 5


def mail_gate_open(user_id: str) -> bool:
    """Whether the mail pipeline may run for `user_id` right now."""

    async def _read(db) -> bool:
        try:
            uid = uuid.UUID(user_id)
        except (TypeError, ValueError):
            log.warning("billing.gate_bad_user_id", user_id=user_id)
            return False

        user = await db.get(User, uid)
        if user is None:
            return False

        sub = await get_subscription(db, uid)
        return may_process_mail(user, sub, datetime.now(timezone.utc))

    return run_worker_session(_read)


async def maybe_start_mail_sync(db: AsyncSession, user: User) -> bool:
    """Kick off the first mailbox sync if `user` has just become eligible.

    Called from both ends of the eligibility rule — finishing onboarding and
    the subscription becoming entitled — because they complete in either order
    and only the second one matters. The first call is a no-op, the second
    starts the sync, and neither needs to know which it is.

    Only ever the *first* sync: `initial_sync_at` makes repeat calls free, which
    matters because billing webhooks retry. A user who lapses and resubscribes
    is picked up by the watch renewal and reconciliation poll instead, both of
    which consult the same gate on every tick.

    Returns whether it enqueued, which is what the caller logs.
    """
    if user.initial_sync_at is not None:
        return False

    sub = await get_subscription(db, user.id)
    if not may_process_mail(user, sub, datetime.now(timezone.utc)):
        return False

    # Imported here, not at module scope: the task module imports this one for
    # `mail_gate_open`, so a top-level import would be circular.
    from workers.jobs.sync_last_7_days import sync_last_7_days

    sync_last_7_days.apply_async((str(user.id),), countdown=START_DELAY_SECONDS)
    log.info("gmail.sync_started", user_id=str(user.id))
    return True
