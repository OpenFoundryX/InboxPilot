"""Booking links, event types, and the reservations guests make against them.

Three tables and one non-obvious constraint:

  * `scheduling_settings` — one row per user. The *profile*: the handle in the
    URL, the time zone every stored window is interpreted in, and the recurring
    weekly hours. What is true of the person regardless of what is being booked.
  * `scheduling_event_types` — the bookable things. Duration, notice, buffers,
    and the questions a guest answers live here, not on the user, because
    "15 min intro" and "60 min deep dive" differ in every one of them.
  * `scheduling_date_overrides` — exceptions to the weekly pattern, keyed by
    date. An empty `windows` list means "not available at all that day", which
    is why the column is a list rather than nullable: absent and empty mean
    different things and the difference is load-bearing.

The constraint worth reading twice is `no_overlap` on bookings. A uniqueness
check on (user, start) is not enough — two guests taking 09:00 and 09:15 of a
30-minute event type collide without sharing a start time. Postgres range
exclusion is the only place that check can live and still be correct under
concurrency; anything computed in the application is a read followed by a write
with a gap in the middle. It is declared here so `Base.metadata` matches the
migration, and applies only to live bookings so a cancelled slot frees up.
"""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ExcludeConstraint, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import literal_column, text

from models.base import Base, TimestampMixin, UUIDMixin

#: A booking that holds its slot. Anything outside this set frees the time.
LIVE_STATUSES = ("pending", "confirmed")

STATUS_PENDING = "pending"
STATUS_CONFIRMED = "confirmed"
STATUS_CANCELLED = "cancelled"
STATUS_FAILED = "failed"


class SchedulingSettings(UUIDMixin, TimestampMixin, Base):
    """Per-user scheduling profile and recurring availability."""

    __tablename__ = "scheduling_settings"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True
    )
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC", nullable=False)
    weekly_hours: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    include_link_in_drafts: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    confirmation_email: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    reschedule_reminders: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class SchedulingEventType(UUIDMixin, TimestampMixin, Base):
    """One bookable meeting shape: how long, how much notice, what to ask."""

    __tablename__ = "scheduling_event_types"
    __table_args__ = (UniqueConstraint("user_id", "slug", name="uq_event_type_slug_per_user"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    slug: Mapped[str] = mapped_column(String(80))
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    duration_minutes: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    slot_interval_minutes: Mapped[int] = mapped_column(Integer, default=15, nullable=False)
    minimum_notice_minutes: Mapped[int] = mapped_column(Integer, default=120, nullable=False)
    booking_horizon_days: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    buffer_before_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    buffer_after_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: NULL means no cap. Zero would mean "never bookable", so it can't be the
    #: sentinel for "unlimited".
    max_bookings_per_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: `[{key, label, type, required, options}]` — see `schemas.scheduling`.
    questions: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)


class SchedulingDateOverride(UUIDMixin, TimestampMixin, Base):
    """A specific date's hours, replacing the weekly pattern. Empty = day off."""

    __tablename__ = "scheduling_date_overrides"
    __table_args__ = (UniqueConstraint("user_id", "day", name="uq_date_override_per_user"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    day: Mapped[date] = mapped_column(Date)
    #: `[{start, end}]` in the user's time zone. `[]` blocks the whole day.
    windows: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)


class SchedulingBooking(UUIDMixin, TimestampMixin, Base):
    """A reserved slot. See the module docstring for why `no_overlap` exists."""

    __tablename__ = "scheduling_bookings"
    __table_args__ = (
        ExcludeConstraint(
            (literal_column("user_id"), "="),
            (literal_column("tstzrange(starts_at, ends_at)"), "&&"),
            name="scheduling_bookings_no_overlap",
            using="gist",
            where=text("status IN ('pending', 'confirmed')"),
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    #: SET NULL rather than CASCADE: deleting an event type must not silently
    #: delete meetings people already have in their calendars.
    event_type_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("scheduling_event_types.id", ondelete="SET NULL"), nullable=True, index=True
    )

    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    booker_name: Mapped[str] = mapped_column(String(200))
    booker_email: Mapped[str] = mapped_column(String(320))
    attendee_emails: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    title: Mapped[str] = mapped_column(String(300), default="Meeting", nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Answers to the event type's `questions`, keyed by question key.
    answers: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    status: Mapped[str] = mapped_column(String(20), default=STATUS_PENDING, index=True)
    calendar_event_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    meeting_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)

    #: Bearer of this token may cancel or reschedule without logging in. It is
    #: the only credential a guest ever has, so it is random, unique, and
    #: indexed for lookup rather than derived from the booking id.
    management_token: Mapped[str] = mapped_column(String(64), unique=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancelled_by: Mapped[str | None] = mapped_column(String(10), nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    #: A reschedule moves this row rather than creating a second one, so the
    #: guest's management link and the calendar event both survive the move.
    #: This is what the host's list shows to explain a changed time.
    rescheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reminder_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
