"""Monthly usage counters for the metered dimensions.

Rows are created on first use rather than reset by a scheduled job: a cron that
fails to fire cannot hand out a free month of quota, and idle users cost nothing.

v1 counts and caps; it does not report to Razorpay. These counters are the
foundation the later overage-billing work reads from.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from models.billing import UsageCounter


def period_start_for(now: datetime) -> date:
    """First day of the billing month `now` falls in, UTC."""
    return date(now.year, now.month, 1)


async def get_or_create_counter(
    db: AsyncSession, user_id: uuid.UUID, now: datetime
) -> UsageCounter:
    """The period's counter row, inserting on first use.

    Concurrent first-time callers — e.g. TrialPill, SubscribeBanner, and the
    dashboard layout all hitting GET /billing/subscription at once — all try
    to create the same `(user_id, period_start)` row. A plain SELECT-then-
    INSERT races and 500s the losers on `uq_usage_counters_user_period`.
    `ON CONFLICT DO NOTHING` makes the insert a no-op for everyone but the
    winner, then the SELECT below returns whichever row exists. Same shape as
    `services.activity.record._insert` — and deliberately *not* the
    begin_nested/IntegrityError pattern in `services.drafts.store`, which
    autoflushes the pending INSERT before the SAVEPOINT is opened and so
    still poisons the outer transaction on conflict.
    """
    period = period_start_for(now)
    await db.execute(
        insert(UsageCounter)
        .values(
            id=uuid.uuid4(),
            user_id=user_id,
            period_start=period,
            bot_seconds_used=0,
            drafts_generated=0,
        )
        .on_conflict_do_nothing(constraint="uq_usage_counters_user_period")
    )
    counter = await db.scalar(
        select(UsageCounter).where(
            UsageCounter.user_id == user_id, UsageCounter.period_start == period
        )
    )
    assert counter is not None
    return counter


async def add_bot_seconds(
    db: AsyncSession, user_id: uuid.UUID, seconds: int, now: datetime
) -> UsageCounter:
    """Credit `seconds` of bot time, as one atomic SQL increment.

    Two `process_meeting` runs for different meetings of the same user can be
    in flight at once (see `workers.jobs.process_meeting`'s module docstring:
    each meeting's "done" webhook enqueues its own task). A Python
    read-modify-write (`counter.bot_seconds_used += seconds`) would let both
    read the same starting value and have whichever commits last silently
    clobber the other's increment — a lost update, not an error, so nothing
    would ever surface it. `UPDATE ... SET x = x + :n` does the addition in
    SQL instead, so it is correct no matter how many sessions hit this row at
    once.

    `synchronize_session=False` matters here, not just as a style choice:
    SQLAlchemy's default ORM auto-sync ("evaluate") would otherwise recompute
    the new value in Python from `counter`'s *current* in-memory attribute —
    which is exactly the stale value a concurrent writer's commit wouldn't be
    reflected in — and silently overwrite the correct, SQL-computed result
    with that wrong one. Selecting just the column back via `RETURNING` and
    assigning it explicitly sidesteps that: the only value the caller ever
    sees is the one Postgres computed against the row's current, real value.
    """
    counter = await get_or_create_counter(db, user_id, now)
    stmt = (
        update(UsageCounter)
        .where(UsageCounter.id == counter.id)
        .values(bot_seconds_used=UsageCounter.bot_seconds_used + seconds)
        .returning(UsageCounter.bot_seconds_used)
        .execution_options(synchronize_session=False)
    )
    # Sharp edge (latent): this assignment marks `counter` dirty in the ORM
    # session; an unrelated flush/commit on this same session before the
    # caller's own final commit would re-issue a plain SET of this stale
    # Python value instead of another atomic add, reopening the clobber
    # window above. Not reachable from any call site today.
    counter.bot_seconds_used = (await db.execute(stmt)).scalar_one()
    return counter


async def add_drafts(
    db: AsyncSession, user_id: uuid.UUID, count: int, now: datetime
) -> UsageCounter:
    """Credit `count` drafts, as one atomic SQL increment.

    Same shape and the same reason as `add_bot_seconds`: a Python
    read-modify-write on a shared counter row is a silent lost-update race
    under concurrent callers, so the addition happens in SQL, and the result
    is read back explicitly rather than trusted to ORM auto-sync.
    """
    counter = await get_or_create_counter(db, user_id, now)
    stmt = (
        update(UsageCounter)
        .where(UsageCounter.id == counter.id)
        .values(drafts_generated=UsageCounter.drafts_generated + count)
        .returning(UsageCounter.drafts_generated)
        .execution_options(synchronize_session=False)
    )
    # Same latent sharp edge as `add_bot_seconds` above: this assignment marks
    # `counter` dirty, so an unrelated flush/commit on this session before the
    # caller's own commit would re-issue a plain (non-atomic) SET instead of
    # another SQL add. `services.drafts.create._meter_draft` is now a caller
    # of this function (each draft meters itself, once, at creation) — it
    # uses its own single-purpose `with_worker_session` today, so this isn't
    # reachable yet, but it's the kind of caller that could make it so if a
    # future change shares one session across more than this one write.
    counter.drafts_generated = (await db.execute(stmt)).scalar_one()
    return counter
