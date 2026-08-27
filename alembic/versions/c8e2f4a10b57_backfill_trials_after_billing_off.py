"""backfill trials for accounts created while billing was off

Revision ID: c8e2f4a10b57
Revises: a7c3e1d90f42
Create Date: 2026-08-24

Billing was switched off on 2026-08-18 and is being switched back on. Accounts
created in between have no `subscriptions` row at all —
`get_or_create_subscription` only runs from `start_checkout`, and a plain GET
never creates one — so restoring the paywall would lock every one of them out
and bounce them to the plan picker.

Mirrors f1a2b3c4d5e6's original backfill. `status = 'authenticated'` is
Razorpay's "mandate signed, first charge not yet due" — the state the access
rules read as trialing — even though no Razorpay subscription exists.

`trial_consumed = true` because this row's creation IS the trial grant. Without
it the next checkout reads the column's default and hands these users a second
full-length trial, which is the exact bug d4e5f6a7b8c9 added the column to
close.

`ON CONFLICT DO NOTHING` makes a re-run harmless and, more importantly, leaves
anyone who did reach checkout with the trial already counting down rather than
restarting it.

The 14 is a hard-coded literal, not settings.TRIAL_DAYS. A migration is a
record of what happened; reading live config is how f1a2b3c4d5e6 ended up
changing its own history when the setting moved.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "c8e2f4a10b57"
down_revision: str | None = "a7c3e1d90f42"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
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
    )

    # Everyone already here got in before the door was shut, so they hold a
    # slot whether or not anyone typed their address into invite.py. Recording
    # them as claimed keeps `invite.py list`'s "x claimed / y invited" honest
    # and makes them count against the first 100.
    op.execute(
        sa.text(
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
    )


def downgrade() -> None:
    # Deliberately not reversible. Deleting the backfilled rows would have to
    # guess which subscriptions this migration created versus which a user
    # authorised themselves, and getting that wrong destroys live billing
    # state. Downgrading past this point is a restore-from-backup operation.
    pass
