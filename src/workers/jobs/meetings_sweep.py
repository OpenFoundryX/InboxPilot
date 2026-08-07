"""Celery beat job: send the notetaker to meetings that are about to start.

Runs every minute for every user with auto-join on. Reads the calendar once,
records the meetings worth attending, books a bot for each with a `join_at`, and
recalls bots whose meeting has disappeared from the calendar.

The calendar read spans a full day even though bots are only booked inside the
user's lookahead window: `rules.skip_reason` enforces the window itself, and the
wider read is what makes a deleted event distinguishable from one that is simply
further out. One API call either way.

It also carries the janitor for uploads that were announced and never arrived —
unrelated to bots, but it wants the same every-minute tick and the same session.
"""

import uuid
from datetime import datetime, timedelta, timezone

from core.database import run_async, with_worker_session
from core.locks import single_run
from core.logging import get_logger
from integrations.composio import calendar
from integrations.meetingbot import get_provider
from integrations.meetingbot.base import MeetingBotError
from models.meetings import (
    STATUS_CANCELLED,
    STATUS_FAILED,
    STATUS_SCHEDULED,
    Meeting,
    MeetingSettings,
)
from models.users import User
from services.billing.entitlements import FEATURE_MEETING_BOT, check
from services.meetings.media import discard
from services.meetings.rules import skip_reason
from services.meetings.store import (
    get_or_create_settings,
    list_abandoned_media,
    list_awaiting_bots,
    list_enabled_settings,
    list_scheduled_upcoming,
    upsert_from_event,
)
from workers.celery_app import celery_app

log = get_logger(__name__)

# How far ahead we read the calendar, independent of the join window.
READ_HORIZON_HOURS = 24
# Join slightly early so the bot is admitted before people start talking.
JOIN_LEAD_SECONDS = 60


@celery_app.task(name="meetings.sweep")
def sweep() -> dict:
    with single_run("meetings.sweep") as acquired:
        if not acquired:
            return {"skipped": "locked"}
        return run_async(with_worker_session(_sweep))


async def _sweep(db) -> dict:
    scheduled = 0
    cancelled = 0
    users = 0

    for settings_row in await list_enabled_settings(db):
        user = await db.get(User, settings_row.user_id)
        if not user:
            continue
        users += 1
        try:
            result = await _sweep_user(db, settings_row)
        except Exception:
            log.exception("meetings.sweep_user_failed", user_id=str(settings_row.user_id))
            continue
        scheduled += result[0]
        cancelled += result[1]

    abandoned = await _expire_abandoned_media(db)

    await db.commit()
    return {
        "users": users,
        "scheduled": scheduled,
        "cancelled": cancelled,
        "abandoned": abandoned,
    }


async def _expire_abandoned_media(db) -> int:
    """Fail rows whose upload never arrived, and remove any partial object.

    Runs across every user, unlike the bot passes above: an upload has nothing
    to do with auto-join, so scoping this to the users those passes visit would
    leave everyone else's abandoned rows and orphaned bytes to accumulate.

    A partial object is deleted before the row is failed. If that delete fails
    the row is still failed — it is genuinely dead either way — and the object
    is caught by retention pruning later, which selects on `media_key` and does
    not care whether the upload ever completed.
    """
    now = datetime.now(timezone.utc)
    expired = 0
    for meeting in await list_abandoned_media(db, now=now):
        await discard(meeting)
        meeting.status = STATUS_FAILED
        meeting.status_detail = "upload never completed"
        expired += 1
        log.info(
            "meetings.media_abandoned",
            meeting_id=str(meeting.id),
            user_id=str(meeting.user_id),
            source=meeting.source,
        )
    return expired


async def _sweep_user(db, settings_row: MeetingSettings) -> tuple[int, int]:
    user_id = settings_row.user_id
    now = datetime.now(timezone.utc)

    events = calendar.list_events(str(user_id), now, now + timedelta(hours=READ_HORIZON_HOURS))
    seen: set[str] = set()

    for event in events:
        event_id = event.get("id")
        if not event_id:
            continue
        seen.add(str(event_id))
        reason = skip_reason(event, settings_row, now=now)
        if reason:
            log.debug(
                "meetings.event_skipped",
                user_id=str(user_id),
                event_id=event_id,
                reason=reason,
            )
            continue
        meeting, created = await upsert_from_event(db, user_id, event)
        if created:
            log.info(
                "meetings.tracked",
                user_id=str(user_id),
                meeting_id=str(meeting.id),
                title=meeting.title,
                platform=meeting.platform,
            )

    scheduled = 0
    for meeting in await list_awaiting_bots(db, user_id):
        decision = await check(db, user_id, FEATURE_MEETING_BOT, now=now)
        if not decision.allowed:
            # Out of quota or unsubscribed. The meeting stays tracked and still
            # shows in the dashboard — it just goes unattended, rather than the
            # user's calendar sync appearing broken.
            log.info(
                "meetings.bot_withheld",
                user_id=str(user_id),
                meeting_id=str(meeting.id),
                reason=decision.reason,
            )
            break
        if _book_bot(meeting, settings_row.bot_name, now):
            scheduled += 1

    cancelled = 0
    for meeting in await list_scheduled_upcoming(db, user_id):
        if meeting.calendar_event_id in seen:
            continue
        # The event is gone from a 24h read but the meeting hasn't started, so
        # it was deleted or moved out of range. Recall the bot rather than let it
        # turn up alone.
        _recall_bot(meeting)
        cancelled += 1

    return scheduled, cancelled


def _book_bot(meeting: Meeting, bot_name: str, now: datetime) -> bool:
    """Book a bot for `meeting`. Leaves the row pending on failure, to retry."""
    join_at = None
    if meeting.starts_at:
        join_at = max(meeting.starts_at - timedelta(seconds=JOIN_LEAD_SECONDS), now)

    try:
        handle = get_provider().schedule_bot(
            meeting.meeting_url,
            bot_name,
            join_at=join_at,
            meeting_id=str(meeting.id),
        )
    except MeetingBotError:
        # Provider trouble costs a delay, not the meeting: still pending, so the
        # next sweep tries again.
        log.warning(
            "meetings.schedule_failed",
            meeting_id=str(meeting.id),
            title=meeting.title,
            exc_info=True,
        )
        return False

    meeting.bot_id = handle.bot_id
    meeting.status = STATUS_SCHEDULED
    # Clears any earlier "bot withheld: ..." note from a quota/lock denial
    # (see `_join_now`) now that a bot is actually booked.
    meeting.status_detail = None
    log.info(
        "meetings.bot_scheduled",
        meeting_id=str(meeting.id),
        bot_id=handle.bot_id,
        join_at=join_at.isoformat() if join_at else None,
    )
    return True


def _recall_bot(meeting: Meeting) -> None:
    if meeting.bot_id:
        try:
            get_provider().cancel_bot(meeting.bot_id)
        except MeetingBotError:
            # Mark it cancelled regardless: the calendar is the source of truth,
            # and a stuck row would be retried forever.
            log.warning("meetings.cancel_failed", meeting_id=str(meeting.id), exc_info=True)
    meeting.status = STATUS_CANCELLED
    meeting.status_detail = "calendar event removed"
    log.info("meetings.bot_cancelled", meeting_id=str(meeting.id), title=meeting.title)


@celery_app.task(name="meetings.join_now")
def join_now(meeting_id: str) -> dict:
    """Send a bot into a meeting immediately — the pasted-link path."""
    return run_async(with_worker_session(lambda db: _join_now(db, meeting_id)))


async def _join_now(db, meeting_id: str) -> dict:
    meeting = await db.get(Meeting, uuid.UUID(meeting_id))
    if not meeting or meeting.bot_id:
        return {"skipped": "missing or already booked"}

    now = datetime.now(timezone.utc)
    decision = await check(db, meeting.user_id, FEATURE_MEETING_BOT, now=now)
    if not decision.allowed:
        # Unlike the sweep, this path is user-initiated: doing nothing with no
        # trace would look like the join silently failed. The row stays
        # STATUS_PENDING with no bot_id, which `list_awaiting_bots` already
        # treats as eligible (ad-hoc rows have no start time and are always
        # picked up), so the next minute's sweep retries it the moment quota
        # or plan allows — the same withheld-not-failed treatment the sweep
        # gives calendar meetings. `status_detail` is set so the caller (who
        # only ever sees this via GET /meetings/{id}, since the API enqueues
        # this task with `.delay()` and never waits on its result) has
        # somewhere to read the reason.
        meeting.status_detail = f"bot withheld: {decision.reason}"
        log.info(
            "meetings.bot_withheld",
            user_id=str(meeting.user_id),
            meeting_id=meeting_id,
            reason=decision.reason,
        )
        await db.commit()
        return {"booked": False, "meeting_id": meeting_id, "reason": decision.reason}

    settings_row = await get_or_create_settings(db, meeting.user_id)
    booked = _book_bot(meeting, settings_row.bot_name, now)
    await db.commit()
    return {"booked": booked, "meeting_id": meeting_id}
