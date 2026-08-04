from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from core.plans import CURRENCY, INTERVAL_MONTHLY, PLAN_PRO
from models.billing import (
    ENTITLED_STATUSES,
    STATUS_ACTIVE,
    STATUS_AUTHENTICATED,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_CREATED,
    STATUS_EXPIRED,
    STATUS_HALTED,
    STATUS_PAUSED,
    STATUS_PENDING,
    SUBSCRIPTION_STARTED_STATUSES,
    Subscription,
    UsageCounter,
)


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


def test_subscription_started_statuses_are_exactly_authorised_ones():
    """Razorpay's nine statuses split into: authorisation happened (this
    set), or it didn't (`created`, `expired`). Pinning the exact membership
    here means a new status added to one set without a matching decision
    about the other fails this test instead of silently mis-gating the
    dashboard."""
    assert SUBSCRIPTION_STARTED_STATUSES == {
        STATUS_AUTHENTICATED,
        STATUS_ACTIVE,
        STATUS_PENDING,
        STATUS_HALTED,
        STATUS_PAUSED,
        STATUS_CANCELLED,
        STATUS_COMPLETED,
    }
    assert STATUS_CREATED not in SUBSCRIPTION_STARTED_STATUSES
    assert STATUS_EXPIRED not in SUBSCRIPTION_STARTED_STATUSES


def test_entitled_statuses_are_a_subset_of_subscription_started_statuses():
    """Every status that still grants entitlements necessarily proves
    authorisation happened — the reverse isn't true (a cancelled
    subscription authorised once but is no longer entitled)."""
    assert ENTITLED_STATUSES <= SUBSCRIPTION_STARTED_STATUSES


async def test_meeting_duration_defaults_to_null(db, user):
    from models.meetings import Meeting

    meeting = Meeting(user_id=user.id, meeting_url="https://meet.example/abc")
    db.add(meeting)
    await db.flush()
    assert meeting.duration_seconds is None
