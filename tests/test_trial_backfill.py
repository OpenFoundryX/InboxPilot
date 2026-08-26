"""Nobody gets locked out by the paywall coming back on.

Accounts created while billing was off have no `subscriptions` row at all —
`get_or_create_subscription` only runs from `start_checkout`, and a plain GET
never creates one. The moment `resolve_access` stops returning "entitled" every
one of them is locked *and* bounced to the plan picker, which is what the
backfill migration exists to prevent.

These tests exercise the same SQL the migration runs, against the test session,
rather than driving alembic — the assertion worth making is about the rules
(one row per user, never restart a running trial), not about alembic working.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

from models.billing import STATUS_AUTHENTICATED, Subscription
from tests.factories import make_user

BACKFILL_SQL = text(
    """
    INSERT INTO subscriptions
        (id, user_id, plan_id, interval, currency, status, trial_ends_at,
         trial_consumed, cancel_at_period_end, comped, created_at, updated_at)
    SELECT gen_random_uuid(), u.id, 'pro', 'monthly', 'USD', 'authenticated',
           now() + interval '14 days', true, false, false, now(), now()
    FROM users u
    ON CONFLICT (user_id) DO NOTHING
    """
)


async def test_a_user_with_no_subscription_gets_a_trial(db):
    user = await make_user(db)
    await db.execute(BACKFILL_SQL)

    sub = await db.scalar(select(Subscription).where(Subscription.user_id == user.id))
    assert sub is not None
    assert sub.status == STATUS_AUTHENTICATED
    assert sub.plan_id == "pro"
    assert sub.comped is False
    # trial_consumed, or the next checkout hands them a second free fortnight.
    assert sub.trial_consumed is True
    remaining = sub.trial_ends_at - datetime.now(timezone.utc)
    assert timedelta(days=13) < remaining <= timedelta(days=14)


async def test_an_existing_subscription_is_left_alone(db):
    """ON CONFLICT DO NOTHING: never restart a trial already counting down."""
    user = await make_user(db)
    original = datetime.now(timezone.utc) + timedelta(days=2)
    db.add(
        Subscription(
            user_id=user.id,
            status=STATUS_AUTHENTICATED,
            trial_ends_at=original,
            trial_consumed=True,
        )
    )
    await db.flush()

    await db.execute(BACKFILL_SQL)

    subs = list(await db.scalars(select(Subscription).where(Subscription.user_id == user.id)))
    assert len(subs) == 1
    assert subs[0].trial_ends_at == original


async def test_running_the_backfill_twice_changes_nothing(db):
    user = await make_user(db)
    await db.execute(BACKFILL_SQL)
    first = await db.scalar(select(Subscription).where(Subscription.user_id == user.id))
    ends_at = first.trial_ends_at

    await db.execute(BACKFILL_SQL)

    subs = list(await db.scalars(select(Subscription).where(Subscription.user_id == user.id)))
    assert len(subs) == 1
    assert subs[0].trial_ends_at == ends_at
