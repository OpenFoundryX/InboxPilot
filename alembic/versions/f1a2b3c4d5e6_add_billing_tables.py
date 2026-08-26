"""add billing tables

Revision ID: f1a2b3c4d5e6
Revises: e7b2140c9a83
"""

import sqlalchemy as sa
from alembic import op

revision: str = "f1a2b3c4d5e6"
down_revision: str | None = "e7b2140c9a83"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subscriptions",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("razorpay_customer_id", sa.String(64), nullable=True),
        sa.Column("razorpay_subscription_id", sa.String(64), nullable=True),
        sa.Column("plan_id", sa.String(16), nullable=False, server_default="pro"),
        sa.Column("interval", sa.String(16), nullable=False, server_default="monthly"),
        sa.Column("currency", sa.String(3), nullable=False, server_default="USD"),
        sa.Column("status", sa.String(16), nullable=False, server_default="created"),
        sa.Column("trial_ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_at_period_end", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("comped", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_subscriptions_user_id", "subscriptions", ["user_id"], unique=True)
    op.create_index("ix_subscriptions_status", "subscriptions", ["status"])
    op.create_index(
        "ix_subscriptions_razorpay_customer_id", "subscriptions", ["razorpay_customer_id"]
    )
    op.create_index(
        "ix_subscriptions_razorpay_subscription_id", "subscriptions", ["razorpay_subscription_id"]
    )

    op.create_table(
        "usage_counters",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("bot_seconds_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("drafts_generated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("user_id", "period_start", name="uq_usage_counters_user_period"),
    )
    op.create_index("ix_usage_counters_user_id", "usage_counters", ["user_id"])

    op.add_column("meetings", sa.Column("duration_seconds", sa.Integer(), nullable=True))

    # Backfill: every pre-billing account starts the same 7-day trial, on Pro,
    # so shipping billing never removes a capability someone had yesterday.
    # They get `authenticated` — Razorpay's "mandate signed, first charge not yet
    # due" state — even though no Razorpay subscription exists, because that is
    # the status our access rules read as "trialing". `ON CONFLICT DO NOTHING`
    # makes a re-run harmless: it must never restart or extend a trial that is
    # already counting down.
    # The 7 is frozen deliberately. This migration originally read
    # settings.TRIAL_DAYS at runtime, which meant changing that setting
    # silently rewrote what this historical migration does on a fresh
    # database. A migration is a record of what happened, not a function of
    # today's config.
    op.execute(
        sa.text(
            """
            INSERT INTO subscriptions
                (id, user_id, plan_id, interval, currency, status, trial_ends_at,
                 cancel_at_period_end, comped, created_at, updated_at)
            SELECT gen_random_uuid(), u.id, 'pro', 'monthly', 'USD', 'authenticated',
                   now() + interval '7 days', false, false, now(), now()
            FROM users u
            ON CONFLICT (user_id) DO NOTHING
            """
        )
    )


def downgrade() -> None:
    op.drop_column("meetings", "duration_seconds")
    op.drop_table("usage_counters")
    op.drop_table("subscriptions")
