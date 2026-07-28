"""Meetings the notetaker bot attends, and the per-user rules for attending.

One `Meeting` row is the whole life of a call as far as InboxPilot is concerned:
the calendar event it came from, the bot we sent, and the transcript/summary that
came back. Rows are created ahead of the call (by the calendar sweep, or by a
pasted link) and enriched as the provider reports progress.

Keyed on `(user_id, calendar_event_id)` so re-running the sweep over the same
calendar window can never send a second bot to the same meeting. Ad-hoc rows
carry a NULL event id, which Postgres treats as distinct — many are allowed.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin, UUIDMixin

# Where the meeting came from.
SOURCE_CALENDAR = "calendar"
SOURCE_ADHOC = "adhoc"

# Lifecycle. pending means "we want a bot but don't have one yet" — the sweep
# retries those, so a provider outage costs a delay rather than the meeting.
STATUS_PENDING = "pending"
STATUS_SCHEDULED = "scheduled"  # bot created with a join_at
STATUS_JOINING = "joining"  # connecting, waiting room, or not yet recording
STATUS_RECORDING = "recording"
STATUS_ENDED = "ended"  # bot left the call; media not available yet
STATUS_RECORDED = "recorded"  # provider done; transcript downloadable
STATUS_PROCESSED = "processed"  # transcript + summary persisted
STATUS_DELIVERED = "delivered"  # recap sent
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

# Statuses where a bot is outstanding and the meeting may still produce output.
ACTIVE_STATUSES = (STATUS_SCHEDULED, STATUS_JOINING, STATUS_RECORDING, STATUS_ENDED)

# No bot is attending: either none was ever booked, or the booking is gone.
# Consumed by the dashboard summary (bot_on) and POST /v1/meetings/bot.
BOT_OFF_STATUSES = frozenset({STATUS_CANCELLED, STATUS_FAILED})

# The call has started or finished — nothing left to toggle. Consumed by both
# the dashboard summary's bot_editable flag and POST /v1/meetings/bot's 409
# guard, so the two can never drift out of step: one definition, two readers.
BOT_LOCKED_STATUSES = frozenset(
    {STATUS_RECORDING, STATUS_ENDED, STATUS_RECORDED, STATUS_PROCESSED, STATUS_DELIVERED}
)

PLATFORM_MEET = "google_meet"
PLATFORM_ZOOM = "zoom"
PLATFORM_TEAMS = "teams"


class Meeting(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "meetings"
    __table_args__ = (
        UniqueConstraint("user_id", "calendar_event_id", name="uq_meeting_user_event"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(16), default=SOURCE_CALENDAR, nullable=False)
    calendar_event_id: Mapped[str | None] = mapped_column(String(256), nullable=True)

    title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    meeting_url: Mapped[str] = mapped_column(Text, nullable=False)
    platform: Mapped[str | None] = mapped_column(String(16), nullable=True)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attendees: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    status: Mapped[str] = mapped_column(String(16), default=STATUS_PENDING, nullable=False, index=True)
    # Provider's reason for the current status (a denied recording, a fatal
    # error's sub_code). The only thing that explains a `failed` meeting.
    status_detail: Mapped[str | None] = mapped_column(String(200), nullable=True)
    bot_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    recording_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    decisions: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    action_items: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    recap_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MeetingSettings(UUIDMixin, TimestampMixin, Base):
    """Per-user notetaker rules.

    `auto_join` defaults off. Recording other people is the user's decision to
    make deliberately, not something onboarding switches on for them.
    """

    __tablename__ = "meeting_settings"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    auto_join: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    bot_name: Mapped[str] = mapped_column(String(64), default="InboxPilot Notetaker", nullable=False)

    # Skip 1:1s with yourself and solo holds; 2 means "at least one other person".
    min_attendees: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    # Case-insensitive substrings; a match means don't send a bot.
    skip_titles: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    # How far ahead to schedule bots. The provider wants join_at in advance, so
    # this is also the window the sweep reads from the calendar.
    lookahead_minutes: Mapped[int] = mapped_column(Integer, default=30, nullable=False)

    email_recap: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    create_reminders: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    include_in_digest: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
