"""Nobody gets locked out by the paywall coming back on.

Accounts created while billing was off have no `subscriptions` row at all —
`get_or_create_subscription` only runs from `start_checkout`, and a plain GET
never creates one. The moment `resolve_access` stops returning "entitled" every
one of them is locked *and* bounced to the plan picker, which is what the
backfill migration exists to prevent.

The same migration has a second half: it records every pre-existing user's
address as an invite they have already claimed, so the allowlist's "x claimed /
y invited" counts stay honest once the door is shut behind them.

These tests exercise the same SQL the migration runs, against the test session,
rather than driving alembic — the assertion worth making is about the rules
(one row per user, never restart a running trial, never overwrite a real
invite), not about alembic working.
"""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

from models.billing import STATUS_AUTHENTICATED, Subscription
from models.invites import InvitedEmail
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

# The migration's second statement: everyone already here got in before the
# door was shut, so their address is recorded as an invite they have claimed.
INVITE_BACKFILL_SQL = text(
    """
    INSERT INTO invited_emails
        (id, email, note, invited_at, claimed_at, claimed_by_user_id,
         created_at, updated_at)
    SELECT gen_random_uuid(), lower(trim(u.email)),
           'backfilled: signed up before invites existed',
           now(), now(), u.id, now(), now()
    FROM users u
    ON CONFLICT (email) DO NOTHING
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


# ---------------------------------------------------------------------------
# The migration's other half: the invite allowlist
# ---------------------------------------------------------------------------


async def test_the_invite_backfill_marks_existing_users_claimed(db):
    """Claimed, not merely invited.

    A row with a null `claimed_at` is the definition of an open slot, so
    backfilling these as unclaimed would both misreport "x claimed / y invited"
    and leave the address re-giftable to someone else.
    """
    user = await make_user(db)

    await db.execute(INVITE_BACKFILL_SQL)

    row = await db.scalar(select(InvitedEmail).where(InvitedEmail.claimed_by_user_id == user.id))
    assert row is not None
    assert row.email == user.email
    assert row.claimed_at is not None
    assert row.invited_at is not None


async def test_the_invite_backfill_lowercases_the_address(db):
    """`email` is the gate's lookup key and the callback normalises before
    asking, so a backfilled row spelled the way Google spells it would never
    match — and its owner would read as uninvited."""
    user = await make_user(db, email=f"Mixed-{uuid.uuid4().hex[:12]}@Example.COM")

    await db.execute(INVITE_BACKFILL_SQL)

    row = await db.scalar(select(InvitedEmail).where(InvitedEmail.claimed_by_user_id == user.id))
    assert row is not None
    assert row.email == user.email.lower()


async def test_running_the_invite_backfill_twice_changes_nothing(db):
    """`ON CONFLICT (email) DO NOTHING` infers its arbiter from a unique index
    rather than a table constraint. That the inference resolves at all is worth
    pinning: if it ever stopped, the second run would raise, not duplicate."""
    user = await make_user(db)
    await db.execute(INVITE_BACKFILL_SQL)
    first = await db.scalar(select(InvitedEmail).where(InvitedEmail.claimed_by_user_id == user.id))
    claimed_at = first.claimed_at

    await db.execute(INVITE_BACKFILL_SQL)

    rows = list(
        await db.scalars(select(InvitedEmail).where(InvitedEmail.claimed_by_user_id == user.id))
    )
    assert len(rows) == 1
    assert rows[0].claimed_at == claimed_at


async def test_an_already_invited_address_keeps_its_original_row(db):
    """Someone invited by hand who then signed up: the backfill must not
    overwrite the real invite's note or its own `claimed_at`."""
    user = await make_user(db)
    db.add(InvitedEmail(email=user.email, note="hand-invited"))
    await db.flush()

    await db.execute(INVITE_BACKFILL_SQL)

    rows = list(await db.scalars(select(InvitedEmail).where(InvitedEmail.email == user.email)))
    assert len(rows) == 1
    assert rows[0].note == "hand-invited"
