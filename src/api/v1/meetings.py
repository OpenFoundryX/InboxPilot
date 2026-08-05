"""Meeting notetaker API — history, ad-hoc joins, and per-user rules."""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.deps import DbSession
from core.logging import get_logger
from integrations.composio import calendar
from integrations.meetingbot import get_provider
from integrations.meetingbot.base import MeetingBotError
from integrations.storage.base import StorageError
from models.meetings import (
    ACTIVE_STATUSES,
    BOT_LOCKED_STATUSES,
    SOURCE_ADHOC,
    SOURCE_LIVE,
    SOURCE_UPLOAD,
    STATUS_CANCELLED,
    STATUS_PENDING,
    STATUS_RECORDING,
    Meeting,
    MeetingSettings,
)
from models.users import User
from schemas.meetings import (
    EnableBotRequest,
    JoinRequest,
    MeetingDetail,
    MeetingRead,
    MeetingUpdate,
    SettingsRead,
    SettingsUpdate,
    StartLiveRequest,
    UploadRequest,
    UploadTarget,
)
from services.auth.dependencies import get_current_user
from services.billing.entitlements import FEATURE_MEETING_BOT, REASON_LOCKED, check
from services.meetings.links import find_meeting_link, link_from_event
from services.meetings.media import (
    MediaRejected,
    confirm,
    discard,
    reserve,
    reserve_for_live,
)
from services.meetings.recording import resolve_recording_url
from services.meetings.store import get_or_create_settings, upsert_from_event
from workers.jobs.meetings_sweep import join_now
from workers.jobs.transcribe_media import transcribe_meeting_media

log = get_logger(__name__)

router = APIRouter(prefix="/meetings", tags=["meetings"])

CurrentUser = Annotated[User, Depends(get_current_user)]

LIST_LIMIT = 50
# How far ahead to look for the event, matching meetings_sweep's horizon.
EVENT_LOOKUP_HOURS = 48


async def _require_bot_quota(db: AsyncSession, user: User, now: datetime) -> None:
    """Reject a join/enable request the same way the worker would, but now.

    Unlike the calendar sweep and `join_now` (fire-and-forget, nobody is
    waiting on the result — silently withholding the bot and logging is the
    right call there), these two routes are synchronous and user-initiated: a
    user pasting a link or flipping a switch is asking a direct question and
    deserves a direct answer rather than a 202 that quietly does nothing.
    `require_entitled`/`EntitledUser` (`services.billing.dependencies`) only
    answers "is this account live" — it would let a Starter user who is over
    their bot-hour cap straight through. `entitlements.check` with
    `FEATURE_MEETING_BOT` is the one that also enforces the cap, which is the
    check that actually matters here, so it is used directly rather than the
    dependency.
    """
    decision = await check(db, user.id, FEATURE_MEETING_BOT, now=now)
    if not decision.allowed:
        detail = (
            "Your subscription is not active."
            if decision.reason == REASON_LOCKED
            else "You've used this month's meeting bot minutes on your plan."
        )
        raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, detail)


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

    result = await db.scalars(select(Meeting).where(
        Meeting.user_id == user.id
    ).order_by(
        Meeting.starts_at.desc().nullslast(), Meeting.created_at.desc()
    ).limit(LIST_LIMIT))

    return list(result)


@router.post("/join", response_model=MeetingRead, status_code=status.HTTP_202_ACCEPTED)
async def join_meeting(payload: JoinRequest, user: CurrentUser, db: DbSession) -> Meeting:
    """Send the notetaker into a call now, from a pasted link or invitation."""

    found = find_meeting_link(payload.meeting_url)
    if not found:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, "No Zoom, Google Meet, or Teams link found in that text",
        )

    url, platform = found
    settings_row = await get_or_create_settings(db, user.id)

    if not settings_row.enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "The notetaker is disabled")

    await _require_bot_quota(db, user, datetime.now(timezone.utc))

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


@router.post("/uploads", response_model=UploadTarget, status_code=status.HTTP_201_CREATED)
async def start_upload(payload: UploadRequest, user: CurrentUser, db: DbSession) -> UploadTarget:
    """Reserve a meeting for a recording, and permission to upload it.

    The row is created before the URL is handed out, so an upload that starts is
    always attributable to a meeting — otherwise a client could fill the bucket
    with objects nothing points at.
    """
    settings_row = await get_or_create_settings(db, user.id)
    if not settings_row.enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "The notetaker is disabled")

    now = datetime.now(timezone.utc)
    await _require_bot_quota(db, user, now)

    meeting = await _meeting_for_upload(db, user, payload, now)
    try:
        presigned = await reserve(
            db,
            meeting,
            content_type=payload.content_type,
            size_bytes=payload.size_bytes,
            filename=payload.filename,
        )
    except MediaRejected as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except StorageError as exc:
        log.exception("meetings.presign_failed", user_id=str(user.id))
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Storage is unavailable right now"
        ) from exc

    log.info(
        "meetings.upload_started",
        user_id=str(user.id),
        meeting_id=str(meeting.id),
        size_bytes=payload.size_bytes,
    )
    return UploadTarget(
        meeting=meeting,
        upload_url=presigned.url,
        headers=presigned.headers,
        expires_at=presigned.expires_at,
    )


async def _meeting_for_upload(
    db: AsyncSession, user: User, payload: UploadRequest, now: datetime
) -> Meeting:
    """The row an uploaded file belongs to — an existing event's, or a new one.

    A file uploaded against a calendar event is a recording *of* that meeting,
    so it attaches to the row that already exists for it and inherits its
    title, attendees, and times. Creating a second row would split one meeting
    into two, one with notes and one with a video.
    """
    if payload.calendar_event_id:
        events = await run_in_threadpool(
            calendar.list_events,
            str(user.id),
            now - timedelta(hours=EVENT_LOOKUP_HOURS),
            now + timedelta(hours=EVENT_LOOKUP_HOURS),
        )
        event = next(
            (e for e in events if str(e.get("id")) == payload.calendar_event_id), None
        )
        if event is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "That event is not on your calendar")

        meeting, _ = await upsert_from_event(db, user.id, event)
        if meeting.media_key:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "That meeting already has a recording"
            )
        # The row keeps `source=calendar` — where it came from hasn't changed —
        # but from here it is media we host, which `media_key` is what decides.
        if payload.title:
            meeting.title = payload.title
        meeting.status = STATUS_PENDING
        meeting.status_detail = None
        await db.flush()
        return meeting

    meeting = Meeting(
        user_id=user.id,
        source=SOURCE_UPLOAD,
        title=payload.title,
        status=STATUS_PENDING,
        # Uploads carry no time of their own, and the file's own metadata is
        # about when it was encoded, not when the meeting happened. The upload
        # time is at least true about something, and it is what the list sorts
        # and groups by.
        starts_at=now,
    )
    db.add(meeting)
    await db.flush()
    return meeting


@router.post("/live", response_model=UploadTarget, status_code=status.HTTP_201_CREATED)
async def start_live_recording(
    payload: StartLiveRequest, user: CurrentUser, db: DbSession
) -> UploadTarget:
    """Start recording in the browser.

    Returns the row and the upload target together so the recorder has
    somewhere to send the audio the moment the user stops, without a second
    round trip at the one point where a failure would lose the recording.
    """
    settings_row = await get_or_create_settings(db, user.id)
    if not settings_row.enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "The notetaker is disabled")

    now = datetime.now(timezone.utc)
    await _require_bot_quota(db, user, now)

    meeting = Meeting(
        user_id=user.id,
        source=SOURCE_LIVE,
        title=payload.title,
        status=STATUS_RECORDING,
        starts_at=now,
        joined_at=now,
    )
    db.add(meeting)
    await db.flush()

    try:
        presigned = await reserve_for_live(db, meeting)
    except StorageError as exc:
        log.exception("meetings.presign_failed", user_id=str(user.id))
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Storage is unavailable right now"
        ) from exc

    log.info("meetings.live_started", user_id=str(user.id), meeting_id=str(meeting.id))
    return UploadTarget(
        meeting=meeting,
        upload_url=presigned.url,
        headers=presigned.headers,
        expires_at=presigned.expires_at,
    )


@router.post("/{meeting_id}/uploads/complete", response_model=MeetingRead)
async def complete_upload(meeting_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Meeting:
    """Confirm the media arrived, and queue it for transcription.

    The bucket is asked, not the client. A client reporting success is not
    evidence of an upload, and taking its word queues a transcription job
    against an object that may never have been written.
    """
    meeting = await _owned(db, meeting_id, user.id)
    if meeting.media_confirmed_at:
        # The browser retried, or two tabs finished at once. The job is already
        # queued; saying so is friendlier than a 409 for something that is fine.
        return meeting

    try:
        await confirm(db, meeting)
    except MediaRejected as exc:
        # The row stays unconfirmed so the janitor can clean it up if the client
        # never comes back.
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except StorageError as exc:
        log.exception("meetings.confirm_failed", meeting_id=str(meeting_id))
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Storage is unavailable right now"
        ) from exc

    transcribe_meeting_media.delay(str(meeting.id))
    log.info("meetings.upload_completed", user_id=str(user.id), meeting_id=str(meeting_id))
    return meeting


@router.get("/{meeting_id}", response_model=MeetingDetail)
async def get_meeting(meeting_id: uuid.UUID, user: CurrentUser, db: DbSession) -> Meeting:
    """One meeting, with a video link that works right now.

    The link is resolved on read rather than served from storage because the
    provider signs it for a few hours — a stored one is a link that works in
    testing and is dead by the time a user clicks it. Resolving costs a provider
    call at most once per meeting per expiry window; the rest are cache hits.
    """
    meeting = await _owned(db, meeting_id, user.id)
    await resolve_recording_url(db, meeting)
    return meeting


@router.patch("/{meeting_id}", response_model=MeetingRead)
async def rename_meeting(
    meeting_id: uuid.UUID, payload: MeetingUpdate, user: CurrentUser, db: DbSession
) -> Meeting:
    """Rename a meeting.

    Allowed at any point in its life, including mid-recording: a title is a
    label the user owns, not part of the capture, so there is no state in which
    correcting it should be refused.
    """
    meeting = await _owned(db, meeting_id, user.id)

    title = (payload.title or "").strip()
    # Empty clears the name rather than storing "". The list already renders an
    # untitled meeting as "Meeting on <date>", so null is the shape that means
    # "no name" everywhere else in this table.
    meeting.title = title or None
    await db.flush()

    log.info("meetings.renamed", user_id=str(user.id), meeting_id=str(meeting_id))
    return meeting


@router.delete("/{meeting_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_meeting(meeting_id: uuid.UUID, user: CurrentUser, db: DbSession) -> None:
    """Delete a meeting, its recording, and any bot still attending it.

    Irreversible, and ordered so that everything which can fail happens while
    the row is still there to retry from. Metered bot-seconds are deliberately
    left alone: returning them would make delete-after-every-call an unlimited
    -usage loophole. Reminders raised from action items also survive — they
    carry no reference to the meeting, and a commitment does not stop existing
    because its recording was deleted.
    """
    meeting = await _owned(db, meeting_id, user.id)

    # A bot still in the call has to go first. If the provider refuses, stop
    # here: a notetaker sitting in a meeting the user believes they deleted is
    # a privacy failure, and it is better to make them retry than to let that
    # happen quietly. A pending or finished bot has nothing to recall.
    if meeting.bot_id and meeting.status in ACTIVE_STATUSES:
        try:
            await run_in_threadpool(get_provider().cancel_bot, meeting.bot_id)
        except MeetingBotError as exc:
            log.warning(
                "meetings.delete_cancel_failed", meeting_id=str(meeting_id), error=str(exc)
            )
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                f"Couldn't recall the notetaker, so the meeting was kept: {exc}",
            ) from exc

    # Then the bytes. Dropping the row while the object survives orphans it for
    # good — the key is the only thing that knew where it was.
    if meeting.media_key and not await discard(meeting):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Couldn't delete the recording, so the meeting was kept. Try again.",
        )

    await db.delete(meeting)
    log.info(
        "meetings.deleted",
        user_id=str(user.id),
        meeting_id=str(meeting_id),
        source=meeting.source,
    )


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
    await _require_bot_quota(db, user, now)

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
        select(Meeting).where(
            Meeting.id == meeting_id,
            Meeting.user_id == user_id,
        )
    )

    if not meeting:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Meeting not found")

    return meeting
