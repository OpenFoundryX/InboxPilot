"""Async DB helpers for meetings and notetaker settings."""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.meetings import (
    ACTIVE_STATUSES,
    SOURCE_ADHOC,
    SOURCE_CALENDAR,
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_PENDING,
    Meeting,
    MeetingSettings,
)
from services.meetings.rules import attendee_emails, event_bounds
from services.meetings.links import link_from_event


async def get_or_create_settings(db: AsyncSession, user_id: uuid.UUID) -> MeetingSettings:
    row = await db.scalar(select(MeetingSettings).where(MeetingSettings.user_id == user_id))
    if row is None:
        row = MeetingSettings(user_id=user_id, skip_titles=[])
        db.add(row)
        await db.flush()
    return row


async def list_enabled_settings(db: AsyncSession) -> list[MeetingSettings]:
    """Users the sweep should consider — enabled with auto-join on."""
    result = await db.scalars(
        select(MeetingSettings).where(
            MeetingSettings.enabled.is_(True), MeetingSettings.auto_join.is_(True)
        )
    )
    return list(result)


async def get_by_bot_id(db: AsyncSession, bot_id: str) -> Meeting | None:
    return await db.scalar(select(Meeting).where(Meeting.bot_id == bot_id))


async def upsert_from_event(
    db: AsyncSession, user_id: uuid.UUID, event: dict
) -> tuple[Meeting, bool]:
    """Get-or-create the Meeting row for a calendar event.

    Returns `(meeting, created)`. Existing rows have their times and title
    refreshed — a meeting moved by 15 minutes is the same meeting, and the bot's
    `join_at` needs to follow it.
    """
    event_id = str(event.get("id"))
    link = link_from_event(event)
    bounds = event_bounds(event)
    url = link[0] if link else None
    platform = link[1] if link else None

    row = await db.scalar(
        select(Meeting).where(Meeting.user_id == user_id, Meeting.calendar_event_id == event_id)
    )
    created = False
    if row is None:
        row = Meeting(
            user_id=user_id,
            source=SOURCE_CALENDAR,
            calendar_event_id=event_id,
            meeting_url=url or "",
            platform=platform,
            status=STATUS_PENDING,
        )
        db.add(row)
        created = True

    row.title = event.get("summary")
    row.attendees = attendee_emails(event)
    if url:
        row.meeting_url = url
        row.platform = platform
    if bounds:
        row.starts_at, row.ends_at = bounds

    # A meeting we cancelled a bot for (because the event vanished from the
    # calendar) can come back — a reschedule looks like a delete then an
    # insert. Put it back in line for a bot instead of silently skipping it.
    if (
        not created
        and row.status == STATUS_CANCELLED
        and bounds
        and bounds[0] > datetime.now(timezone.utc)
    ):
        row.status = STATUS_PENDING
        row.bot_id = None
        row.status_detail = None

    await db.flush()
    return row, created


#: A sweep landing slightly late should still book the bot; an hour late means
#: the meeting is over and a bot would join an empty room.
BOOK_GRACE_MINUTES = 5


async def list_awaiting_bots(db: AsyncSession, user_id: uuid.UUID) -> list[Meeting]:
    """Rows the sweep still owes a bot: pending, with a link, not yet over.

    Rows left pending by an earlier provider outage are picked up here, which is
    the retry — but only while the meeting could still be running.

    Ad-hoc rows stay eligible regardless of their start time. They used to have
    none at all, which the `is_(None)` arm covered; they now carry a
    provisional stamp of when the link was pasted, purely so the list can group
    them under today rather than "Date unknown". That stamp says nothing about
    when the call ends, so treating it as a deadline would stop retrying a
    meeting that is still going.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=BOOK_GRACE_MINUTES)
    result = await db.scalars(
        select(Meeting).where(
            Meeting.user_id == user_id,
            Meeting.status == STATUS_PENDING,
            Meeting.bot_id.is_(None),
            or_(
                Meeting.starts_at.is_(None),
                Meeting.starts_at > cutoff,
                Meeting.source == SOURCE_ADHOC,
            ),
        )
    )
    return [m for m in result if m.meeting_url]


async def list_scheduled_upcoming(db: AsyncSession, user_id: uuid.UUID) -> list[Meeting]:
    """Meetings with an outstanding bot that haven't started yet.

    Used to spot events deleted from the calendar after we scheduled a bot, so
    the bot doesn't turn up to a meeting nobody is holding.
    """
    now = datetime.now(timezone.utc)
    result = await db.scalars(
        select(Meeting).where(
            Meeting.user_id == user_id,
            Meeting.status.in_(ACTIVE_STATUSES),
            Meeting.calendar_event_id.is_not(None),
            Meeting.starts_at.is_not(None),
            Meeting.starts_at > now,
        )
    )
    return list(result)


#: How long a reserved key may sit without bytes before we give up on it. Long
#: enough that a slow 1 GB upload on a bad connection is never mistaken for an
#: abandoned one, short enough that a user who closed the tab sees an honest
#: failure rather than a row stuck on "Processing" indefinitely.
ABANDONED_MEDIA_HOURS = 24


async def list_abandoned_media(db: AsyncSession, *, now: datetime) -> list[Meeting]:
    """Rows that reserved a key and never received the bytes.

    A meeting whose upload was announced but never completed — the tab was
    closed, the browser recording died mid-call, the network dropped. The row
    and any partial object both need clearing.

    Queried across all users rather than per-user: uploads have nothing to do
    with auto-join, so scoping this to the users the bot sweep visits would
    leave everyone else's abandoned rows to accumulate forever.
    """
    cutoff = now - timedelta(hours=ABANDONED_MEDIA_HOURS)
    result = await db.scalars(
        select(Meeting).where(
            Meeting.media_key.is_not(None),
            Meeting.media_confirmed_at.is_(None),
            Meeting.created_at < cutoff,
            Meeting.status.notin_((STATUS_FAILED, STATUS_CANCELLED)),
        )
    )
    return list(result)


async def recent_delivered(
    db: AsyncSession, user_id: uuid.UUID, *, since_hours: int = 24, limit: int = 10
) -> list[Meeting]:
    """Meetings summarized in the last `since_hours`, newest first."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    result = await db.scalars(
        select(Meeting)
        .where(
            Meeting.user_id == user_id,
            Meeting.summary.is_not(None),
            Meeting.created_at >= cutoff,
        )
        .order_by(Meeting.starts_at.desc().nullslast())
        .limit(limit)
    )
    return list(result)
