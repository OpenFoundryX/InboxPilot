"""Assembles the dashboard home payload.

All the logic lives here so the router stays thin, matching every other v1
router in this codebase.
"""

import uuid
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.logging import get_logger
from integrations.composio import calendar
from models.activity import KIND_DRAFT_CREATED, KIND_EMAIL_CATEGORIZED, ActivityEvent
from models.meetings import (
    STATUS_CANCELLED,
    STATUS_DELIVERED,
    STATUS_ENDED,
    STATUS_FAILED,
    STATUS_PROCESSED,
    STATUS_RECORDED,
    STATUS_RECORDING,
    Meeting,
)
from models.users import User
from schemas.dashboard import (
    AgendaItem,
    DashboardMeetings,
    DashboardSetup,
    DashboardStats,
    DashboardSummary,
    DashboardUser,
)
from services.mailman.store import get_or_create_settings
from services.meetings.links import link_from_event
from services.meetings.rules import event_bounds

log = get_logger(__name__)

SETUP_SYNCING = "syncing"
SETUP_READY = "ready"

# No bot is attending: either none was ever booked, or the booking is gone.
BOT_OFF_STATUSES = frozenset({STATUS_CANCELLED, STATUS_FAILED})

# The call has started or finished — nothing left to toggle. Mirrors the 409
# condition in POST /v1/meetings/bot; the two must be kept in step.
BOT_LOCKED_STATUSES = frozenset(
    {STATUS_RECORDING, STATUS_ENDED, STATUS_RECORDED, STATUS_PROCESSED, STATUS_DELIVERED}
)


def first_name(user: User) -> str:
    """The name to greet by: first token of full_name, else the email local part."""
    if user.full_name:
        parts = user.full_name.split()
        if parts:
            return parts[0]
    return user.email.split("@")[0]


def setup_state(user: User) -> str:
    """Derived from the DB alone — no Composio round trip on the dashboard path.

    DashboardLayout already gates on Gmail and Calendar being connected and
    redirects to /onboarding/connect otherwise, so a request that reaches this
    endpoint has established connectivity. Re-verifying would add a blocking
    third-party call per page load to restate a known fact.
    """
    return SETUP_READY if user.initial_sync_at else SETUP_SYNCING


async def load_stats(db: AsyncSession, user_id: uuid.UUID) -> DashboardStats:
    """Lifetime totals. One grouped query; absent kinds report zero."""
    rows = await db.execute(
        select(ActivityEvent.kind, func.count())
        .where(ActivityEvent.user_id == user_id)
        .group_by(ActivityEvent.kind)
    )
    counts = {kind: total for kind, total in rows.all()}
    return DashboardStats(
        emails_categorized=counts.get(KIND_EMAIL_CATEGORIZED, 0),
        drafts_created=counts.get(KIND_DRAFT_CREATED, 0),
    )


def day_bounds(tz_name: str, now: datetime | None = None) -> tuple[datetime, datetime, datetime]:
    """Local midnight today, tomorrow, and the day after — returned as UTC instants.

    Built from calendar dates rather than by adding 24-hour deltas: on a DST
    transition day the local day is 23 or 25 hours long, and `midnight + 1 day`
    would land an hour either side of midnight rather than on it.
    """
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        log.warning("dashboard.unknown_timezone", timezone=tz_name)
        tz = ZoneInfo("UTC")

    local_today = (now or datetime.now(timezone.utc)).astimezone(tz).date()
    midnights = [
        datetime.combine(local_today + timedelta(days=offset), time.min, tzinfo=tz)
        for offset in (0, 1, 2)
    ]
    return tuple(m.astimezone(timezone.utc) for m in midnights)  # type: ignore[return-value]


def bot_flags(
    meeting: Meeting | None, starts_at: datetime, has_link: bool, now: datetime
) -> tuple[bool, bool]:
    """(bot_on, bot_editable) for one agenda row."""
    bot_on = meeting is not None and meeting.status not in BOT_OFF_STATUSES

    if not has_link:
        # Part of the user's day, but there is nothing for a bot to join.
        return False, False
    if starts_at <= now:
        return bot_on, False
    if meeting is not None and meeting.status in BOT_LOCKED_STATUSES:
        return bot_on, False
    return bot_on, True


async def load_agenda(db: AsyncSession, user_id: uuid.UUID, tz_name: str) -> DashboardMeetings:
    """The user's next two days, from the calendar, annotated with bot state.

    Read from the calendar rather than the meetings table on purpose: the table
    holds only calls the sweep chose to book, so an event the notetaker skips —
    "deep work (no calls please)" — has no row, and an agenda built from the
    table alone could never show it as Off.

    A calendar outage empties the agenda but must not empty the page: the stats
    card has nothing to do with Google being reachable.
    """
    now = datetime.now(timezone.utc)
    today_start, tomorrow_start, day_after_start = day_bounds(tz_name, now)

    try:
        events = await run_in_threadpool(
            calendar.list_events, str(user_id), today_start, day_after_start
        )
    except Exception:
        log.exception("dashboard.calendar_unavailable", user_id=str(user_id))
        return DashboardMeetings(timezone=tz_name, today=[], tomorrow=[])

    rows = await db.scalars(select(Meeting).where(Meeting.user_id == user_id))
    by_event = {m.calendar_event_id: m for m in rows if m.calendar_event_id}

    today: list[AgendaItem] = []
    tomorrow: list[AgendaItem] = []

    for event in events:
        event_id = event.get("id")
        bounds = event_bounds(event)
        # All-day entries have no dateTime and no place on a timed agenda.
        if not event_id or not bounds:
            continue
        starts_at, ends_at = bounds

        meeting = by_event.get(str(event_id))
        link = link_from_event(event)
        bot_on, bot_editable = bot_flags(meeting, starts_at, link is not None, now)

        item = AgendaItem(
            calendar_event_id=str(event_id),
            meeting_id=meeting.id if meeting else None,
            title=event.get("summary"),
            starts_at=starts_at,
            ends_at=ends_at,
            meeting_url=link[0] if link else None,
            bot_on=bot_on,
            bot_editable=bot_editable,
        )

        if starts_at < tomorrow_start:
            today.append(item)
        elif starts_at < day_after_start:
            tomorrow.append(item)

    today.sort(key=lambda i: i.starts_at)
    tomorrow.sort(key=lambda i: i.starts_at)
    return DashboardMeetings(timezone=tz_name, today=today, tomorrow=tomorrow)


async def build_summary(db: AsyncSession, user: User) -> DashboardSummary:
    stats = await load_stats(db, user.id)
    mailman_settings = await get_or_create_settings(db, user.id)
    meetings = await load_agenda(db, user.id, mailman_settings.timezone or "UTC")
    return DashboardSummary(
        user=DashboardUser(first_name=first_name(user)),
        setup=DashboardSetup(state=setup_state(user), initial_sync_at=user.initial_sync_at),
        stats=stats,
        meetings=meetings,
    )
