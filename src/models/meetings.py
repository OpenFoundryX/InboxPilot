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
SOURCE_UPLOAD = "upload"  # a media file the user handed us
SOURCE_LIVE = "live"  # recorded in the browser, no call to join

# Sources whose media we hold ourselves rather than the bot vendor. They differ
# only in how the bytes were produced: one is a file the user already had, the
# other is one their browser just made. Everything downstream — storage,
# transcription, metering, retention — treats them identically.
SELF_HOSTED_SOURCES = frozenset({SOURCE_UPLOAD, SOURCE_LIVE})

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

# The provider has finished assembling media, so asking it for a recording can
# actually return one. `ended` is deliberately excluded: the bot has left but the
# recording isn't cut yet, and asking early just buys a wasted call per page view.
MEDIA_READY_STATUSES = frozenset({STATUS_RECORDED, STATUS_PROCESSED, STATUS_DELIVERED})

PLATFORM_MEET = "google_meet"
PLATFORM_ZOOM = "zoom"
PLATFORM_TEAMS = "teams"


class Meeting(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "meetings"
    __table_args__ = (
        UniqueConstraint("user_id", "calendar_event_id", name="uq_meeting_user_event"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    source: Mapped[str] = mapped_column(String(16), default=SOURCE_CALENDAR, nullable=False)
    calendar_event_id: Mapped[str | None] = mapped_column(String(256), nullable=True)

    title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # Null for uploads and browser recordings: there is no call to join, only
    # media to transcribe. Non-null for everything a bot attends.
    meeting_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    platform: Mapped[str | None] = mapped_column(String(16), nullable=True)
    starts_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attendees: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    status: Mapped[str] = mapped_column(
        String(16), default=STATUS_PENDING, nullable=False, index=True
    )
    # Provider's reason for the current status (a denied recording, a fatal
    # error's sub_code). The only thing that explains a `failed` meeting.
    status_detail: Mapped[str | None] = mapped_column(String(200), nullable=True)
    bot_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    recording_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # How long the bot was actually in the call, from the provider's payload.
    # `joined_at` alone cannot answer this, and bot-hours cannot be metered
    # without it.
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # A link to the video, never the video. Providers sign these links for a few
    # hours, so this pair is a cache that knows its own deadline — the durable
    # handle is `recording_id`, which a fresh link is re-resolved from. Storing
    # only the URL would leave us serving links that died overnight.
    recording_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    recording_url_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Set once, by retention pruning, the moment `recording_id`/`recording_url`
    # are cleared for being past the plan's video window. Never cleared again.
    # This is what tells `resolve_recording_url` "deliberately removed" apart
    # from "never had a recording" — both look like `recording_id is None`
    # otherwise, and without this a re-resolve would just fetch the video back
    # from the provider and undo the prune on the next page view.
    recording_pruned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Where our own copy of the media lives, for uploads and browser recordings.
    # This is the discriminator the whole self-hosted path turns on: `media_key`
    # set means the bytes are in our bucket, `bot_id` set means they are the
    # vendor's. `recording_id` stays the vendor's handle and is never used for
    # ours — the two must not be conflated, or a prune written for one silently
    # skips the other.
    media_key: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # When the object was confirmed present in the bucket, by asking the bucket.
    # A key is reserved before the browser starts uploading, so `media_key`
    # alone only means "we expect bytes" — this means "the bytes are there".
    # Everything that would hand out a link, badge the row as playable, or queue
    # transcription keys on this rather than on `media_key`, because presigning
    # a key that was never written yields a URL that 404s in the user's player.
    # Its absence on an old row is also what identifies an abandoned upload.
    media_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # The retention windows in force when this meeting was captured, in days.
    # Written once at processing time from the plan the user was on then, and
    # never touched by a later plan change — a later downgrade must not shorten
    # the deadline for a meeting recorded under a more generous plan. Null on
    # legacy rows (recorded before this column existed) and on rows that never
    # reached processing; the prune falls back to the *current* plan for those,
    # since there is nothing else to grandfather them against.
    #
    # Storing the resolved day counts rather than a plan id is deliberate:
    # `core.plans.PLANS` is a plain dict in code, so a later edit to what
    # "starter" or "pro" means would otherwise retroactively move the deadline
    # for every meeting recorded under that plan id, including ones whose
    # actual guarantee was already fixed. These two columns are the guarantee
    # itself, frozen at capture time.
    retention_video_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retention_transcript_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    decisions: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    action_items: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    recap_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def has_recording(self) -> bool:
        """Whether a video exists, answerable without calling the provider.

        That is the whole point: the list badges rows for free, and only the
        detail view pays for a live link.

        Our own media counts once confirmed, not once reserved: a row that
        claimed a key and never received bytes has no recording, and badging it
        as playable offers a Watch button that resolves to nothing.
        """
        return bool(self.recording_id or self.recording_url or self.media_confirmed_at)


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
    bot_name: Mapped[str] = mapped_column(
        String(64), default="InboxPilot Notetaker", nullable=False
    )

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
