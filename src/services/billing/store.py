"""Row access for subscriptions. No business rules — those live in access.py."""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.plans import CURRENCY, INTERVAL_MONTHLY, PLAN_PRO
from models.billing import STATUS_AUTHENTICATED, Subscription


async def get_subscription(db: AsyncSession, user_id: uuid.UUID) -> Subscription | None:
    return await db.scalar(select(Subscription).where(Subscription.user_id == user_id))


async def get_or_create_subscription(
    db: AsyncSession, user_id: uuid.UUID, *, trial_days: int
) -> Subscription:
    """The user's subscription, starting a trial if they have none.

    Never extends an existing trial: re-running this must not hand someone a
    second free week.
    """
    existing = await get_subscription(db, user_id)
    if existing is not None:
        return existing

    sub = Subscription(
        user_id=user_id,
        plan_id=PLAN_PRO,
        interval=INTERVAL_MONTHLY,
        currency=CURRENCY,
        status=STATUS_AUTHENTICATED,
        trial_ends_at=datetime.now(timezone.utc) + timedelta(days=trial_days),
        # This row's creation IS the trial grant — mark it consumed now so a
        # later checkout (see `api.v1.billing.start_checkout`) knows not to
        # hand this user a second one.
        trial_consumed=True,
    )
    db.add(sub)
    await db.flush()
    return sub
