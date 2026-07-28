"""Meeting notetaker API — history, ad-hoc joins, and per-user rules."""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select

from api.deps import DbSession
from core.logging import get_logger
from integrations.composio import calendar
from integrations.meetingbot import get_provider
from integrations.meetingbot.base import MeetingBotError
from models.meetings import (
    ACTIVE_STATUSES,
    BOT_LOCKED_STATUSES,
    SOURCE_ADHOC,
    STATUS_CANCELLED,
    STATUS_PENDING,
    Meeting,
    MeetingSettings,
)
from models.users import User
from schemas.meetings import (
    EnableBotRequest,
    JoinRequest,
    MeetingDetail,
    MeetingRead,
    SettingsRead,
    SettingsUpdate,
)
from services.auth.dependencies import get_current_user
from services.meetings.links import find_meeting_link, link_from_event
from services.meetings.store import get_or_create_settings, upsert_from_event
from workers.jobs.meetings_sweep import join_now

log = get_logger(__name__)

router = APIRouter(prefix="/meetings", tags=["meetings"])

CurrentUser = Annotated[User, Depends(get_current_user)]

LIST_LIMIT = 50
# How far ahead to look for the event, matching meetings_sweep's horizon.
EVENT_LOOKUP_HOURS = 48


@router.get("/settings", response_model=SettingsRead)
async def get_settings(user: CurrentUser, db: DbSession) -> MeetingSettings:
    return await get_or_create_settings(db, user.id)


@router.put("/settings", response_model=SettingsRead)
async def update_settings(
    payload: SettingsUpdate, user: CurrentUser, db: DbSession
) -> MeetingSettings:
    settings_row = await get_or_create_settings(db, user.id)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(settings_row, key, value)
    return settings_row


@router.get("", response_model=list[MeetingRead])
async def list_meetings(user: CurrentUser, db: DbSession) -> list[Meeting]:
    """Meetings newest first — scheduled ones included, so the UI can show what's coming."""
    result = await db.scalars(
        select(Meeting)
        .where(Meeting.user_id == user.id)
        .order_by(Meeting.starts_at.desc().nullslast(), Meeting.created_at.desc())
        .limit(LIST_LIMIT)
    )
    return list(result)


@router.post("/join", response_model=MeetingRead, status_code=status.HTTP_202_ACCEPTED)
async def join_meeting(payload: JoinRequest, user: CurrentUser, db: DbSession) -> Meeting:
    """Send the notetaker into a call now, from a pasted link or invitation."""
    found = find_meeting_link(payload.meeting_url)
    if not found:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "No Zoom, Google Meet, or Teams link found in that text",
        )
    url, platform = found

    settings_row = await get_or_create_settings(db, user.id)
    if not settings_row.enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "The notetaker is disabled")

    meeting = Meeting(
        user_id=user.id,
        source=SOURCE_ADHOC,
        title=payload.title,
        meeting_url=url,
        platform=platform,
        status=STATUS_PENDING,
    )
    db.add(meeting)
    await db.flush()

    # Booking talks to the provider, so it belongs in a worker; the row is the
    # receipt the caller gets back immediately.
    join_now.delay(str(meeting.id))
    log.info("meetings.adhoc_requested", user_id=str(user.id), meeting_id=str(meeting.id))
    return meeting


@router.get("/{meeting_id}", response_model=MeetingDetail)
async def get_meeting(meeting_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Meeting:
    return await _owned(db, meeting_id, user.id)


@router.post("/bot", response_model=MeetingRead, status_code=status.HTTP_202_ACCEPTED)
async def enable_bot(payload: EnableBotRequest, user: CurrentUser, db: DbSession) -> Meeting:
    """Turn the notetaker on for a calendar event.

    One path covers both "never booked" and "previously cancelled": the row is
    upserted from the calendar event either way. Its counterpart is
    DELETE /meetings/{id}/bot, which already handles turning it off.
    """
    settings_row = await get_or_create_settings(db, user.id)
    if not settings_row.enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "The notetaker is disabled")

    now = datetime.now(timezone.utc)
    events = await run_in_threadpool(
        calendar.list_events, str(user.id), now, now + timedelta(hours=EVENT_LOOKUP_HOURS)
    )
    event = next((e for e in events if str(e.get("id")) == payload.calendar_event_id), None)
    if event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "That event is not on your calendar")

    if not link_from_event(event):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "That event has no Zoom, Google Meet, or Teams link to join",
        )

    meeting, _ = await upsert_from_event(db, user.id, event)
    if meeting.status in BOT_LOCKED_STATUSES:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Too late: meeting is {meeting.status}"
        )
    if meeting.starts_at and meeting.starts_at <= now:
        raise HTTPException(status.HTTP_409_CONFLICT, "That meeting has already started")

    meeting.status = STATUS_PENDING
    meeting.status_detail = None
    await db.flush()

    join_now.delay(str(meeting.id))
    log.info("meetings.bot_enabled", user_id=str(user.id), meeting_id=str(meeting.id))
    return meeting


@router.delete("/{meeting_id}/bot", response_model=MeetingRead)
async def cancel_bot(meeting_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Meeting:
    """Recall the bot for a meeting it hasn't finished yet."""
    meeting = await _owned(db, meeting_id, user.id)
    if meeting.status not in (*ACTIVE_STATUSES, STATUS_PENDING):
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Nothing to cancel: meeting is {meeting.status}"
        )

    if meeting.bot_id:
        try:
            await run_in_threadpool(get_provider().cancel_bot, meeting.bot_id)
        except MeetingBotError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Provider refused: {exc}") from exc

    meeting.status = STATUS_CANCELLED
    meeting.status_detail = "cancelled by user"
    log.info("meetings.cancelled_by_user", user_id=str(user.id), meeting_id=str(meeting_id))
    return meeting


async def _owned(db, meeting_id: uuid.UUID, user_id: uuid.UUID) -> Meeting:
    meeting = await db.scalar(
        select(Meeting).where(Meeting.id == meeting_id, Meeting.user_id == user_id)
    )
    if not meeting:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Meeting not found")
    return meeting
