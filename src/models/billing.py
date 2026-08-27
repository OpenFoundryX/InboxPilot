"""Subscription state and monthly usage counters.

`subscriptions` mirrors Razorpay rather than owning the truth — with one
exception. `trial_ends_at` has three writers: the subscription's `start_at` for
users who completed checkout, migration f1a2b3c4d5e6 for accounts that predate
billing and have no Razorpay customer at all, and migration c8e2f4a10b57 for
accounts created while billing was switched off. All three write the same
column so every reader asks one question instead of branching on account age.

Razorpay's `authenticated` state means the mandate is signed but the first
charge is not yet due — which is exactly a trial, so no separate trial flag
exists to drift out of sync.

`usage_counters` rows are created lazily on the first write of a period rather
than reset by a scheduled job. A cron that fails to fire cannot hand out free
quota, and a user who does nothing costs no rows.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from core.plans import CURRENCY, INTERVAL_MONTHLY, PLAN_PRO
from models.base import Base, TimestampMixin, UUIDMixin

# Razorpay's subscription status vocabulary, spelled exactly as Razorpay spells
# it. Note `cancelled` with two l's — Stripe's is `canceled`, and mixing them up
# silently breaks every comparison.
STATUS_CREATED = "created"
STATUS_AUTHENTICATED = "authenticated"
STATUS_ACTIVE = "active"
STATUS_PENDING = "pending"
STATUS_HALTED = "halted"
STATUS_PAUSED = "paused"
STATUS_CANCELLED = "cancelled"
STATUS_EXPIRED = "expired"
STATUS_COMPLETED = "completed"

# Statuses that still grant the plan's entitlements.
#
# `authenticated` is the trial: the mandate is signed and the first charge is
# scheduled but not yet taken. `pending` is included on purpose — Razorpay is
# still retrying the card, and cutting someone off mid-retry turns a recoverable
# payment blip into a support ticket. `created` is NOT included: it means the
# customer abandoned the modal without authorising, so no card exists.
ENTITLED_STATUSES = frozenset({STATUS_AUTHENTICATED, STATUS_ACTIVE, STATUS_PENDING})

# Statuses reachable only once the user has actually authorised a Razorpay
# mandate — i.e. they got past the checkout modal rather than just opening it.
# This is a *superset* of `ENTITLED_STATUSES` on purpose: authorisation can
# outlive entitlement (a subscription that later lapses to `halted`/`paused`,
# or is deliberately `cancelled`), and someone who authorised once but has
# since churned still proves they aren't trying to sneak past checkout — they
# just aren't currently paying. Keeping the two sets side by side means a
# status added to one is a visible, deliberate decision about the other
# rather than something that quietly falls through the cracks.
#
# Of Razorpay's nine statuses, exactly two are reachable *without* ever
# authorising: `created` (the subscription object exists server-side, but the
# modal was closed before the mandate was signed — the plan-picker bypass
# this set exists to close) and `expired` (the mandate was never authenticated
# before `start_at` passed). A user who has neither of those nor any row at
# all has, definitionally, never started a subscription.
SUBSCRIPTION_STARTED_STATUSES = ENTITLED_STATUSES | frozenset(
    {STATUS_HALTED, STATUS_PAUSED, STATUS_CANCELLED, STATUS_COMPLETED}
)


class Subscription(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "subscriptions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True, nullable=False
    )

    # Null until the user completes checkout. Backfilled accounts trial without
    # ever touching Razorpay, so neither id can be required.
    razorpay_customer_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    razorpay_subscription_id: Mapped[str | None] = mapped_column(
        String(64), index=True, nullable=True
    )

    plan_id: Mapped[str] = mapped_column(String(16), default=PLAN_PRO, nullable=False)
    interval: Mapped[str] = mapped_column(String(16), default=INTERVAL_MONTHLY, nullable=False)
    # USD for every v1 plan. Stored per-row so an INR plan set can coexist later
    # without a migration or a second table.
    currency: Mapped[str] = mapped_column(String(3), default=CURRENCY, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), default=STATUS_CREATED, index=True, nullable=False
    )

    # Mirrors the Razorpay subscription's `start_at`: the moment the first real
    # charge is attempted, i.e. when the trial stops.
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    current_period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Set once, the first time a trial is ever granted to this user (by
    # `get_or_create_subscription` on row creation, or by the billing backfill
    # migration for pre-existing accounts) and never cleared afterwards. Trials
    # are once per customer: without this marker, `start_checkout` cannot tell
    # "first-ever checkout, grant a trial" from "already had one" and will
    # otherwise recompute `trial_ends_at = now + TRIAL_DAYS` on every call —
    # which both stacks a second trial onto a still-running one (a backfilled
    # user mid-trial) and, worse, lets `subscribe -> cancel before the first
    # charge -> checkout again` mint an unlimited run of free weeks.
    trial_consumed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Escape hatch for design partners and support goodwill. A comped row needs
    # no Razorpay record and never locks.
    comped: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    def __repr__(self) -> str:
        return f"<Subscription {self.plan_id} {self.status}>"


class UsageCounter(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "usage_counters"
    __table_args__ = (
        UniqueConstraint("user_id", "period_start", name="uq_usage_counters_user_period"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # First day of the billing month, UTC.
    period_start: Mapped[date] = mapped_column(Date, nullable=False)

    bot_seconds_used: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    drafts_generated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    def __repr__(self) -> str:
        return f"<UsageCounter {self.period_start} bot={self.bot_seconds_used}>"
