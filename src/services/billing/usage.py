"""Monthly usage counters for the metered dimensions.

Rows are created on first use rather than reset by a scheduled job: a cron that
fails to fire cannot hand out a free month of quota, and idle users cost nothing.

v1 counts and caps; it does not report to Razorpay. These counters are the
foundation the later overage-billing work reads from.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.billing import UsageCounter


def period_start_for(now: datetime) -> date:
    """First day of the billing month `now` falls in, UTC."""
    return date(now.year, now.month, 1)


async def get_or_create_counter(
    db: AsyncSession, user_id: uuid.UUID, now: datetime
) -> UsageCounter:
    period = period_start_for(now)
    counter = await db.scalar(
        select(UsageCounter).where(
            UsageCounter.user_id == user_id, UsageCounter.period_start == period
        )
    )
    if counter is None:
        counter = UsageCounter(user_id=user_id, period_start=period)
        db.add(counter)
        await db.flush()
    return counter


async def add_bot_seconds(
    db: AsyncSession, user_id: uuid.UUID, seconds: int, now: datetime
) -> UsageCounter:
    counter = await get_or_create_counter(db, user_id, now)
    counter.bot_seconds_used += seconds
    await db.flush()
    return counter


async def add_drafts(
    db: AsyncSession, user_id: uuid.UUID, count: int, now: datetime
) -> UsageCounter:
    counter = await get_or_create_counter(db, user_id, now)
    counter.drafts_generated += count
    await db.flush()
    return counter
