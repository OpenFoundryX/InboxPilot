from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from core.plans import CURRENCY, INTERVAL_MONTHLY, PLAN_PRO
from models.billing import STATUS_AUTHENTICATED, Subscription, UsageCounter


async def test_subscription_round_trips(db, user):
    sub = Subscription(
        user_id=user.id,
        plan_id=PLAN_PRO,
        interval=INTERVAL_MONTHLY,
        status=STATUS_AUTHENTICATED,
        trial_ends_at=datetime.now(timezone.utc) + timedelta(days=7),
    )
    db.add(sub)
    await db.flush()
    assert sub.id is not None
    assert sub.comped is False
    assert sub.cancel_at_period_end is False
    assert sub.razorpay_customer_id is None
    assert sub.currency == CURRENCY


async def test_one_subscription_per_user(db, user):
    for _ in range(2):
        db.add(
            Subscription(
                user_id=user.id,
                plan_id=PLAN_PRO,
                interval=INTERVAL_MONTHLY,
                status=STATUS_AUTHENTICATED,
            )
        )
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_usage_counter_is_unique_per_user_and_period(db, user):
    period = date(2026, 8, 1)
    for _ in range(2):
        db.add(UsageCounter(user_id=user.id, period_start=period))
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_usage_counter_defaults_to_zero(db, user):
    counter = UsageCounter(user_id=user.id, period_start=date(2026, 8, 1))
    db.add(counter)
    await db.flush()
    assert counter.bot_seconds_used == 0
    assert counter.drafts_generated == 0


async def test_meeting_duration_defaults_to_null(db, user):
    from models.meetings import Meeting

    meeting = Meeting(user_id=user.id, meeting_url="https://meet.example/abc")
    db.add(meeting)
    await db.flush()
    assert meeting.duration_seconds is None
