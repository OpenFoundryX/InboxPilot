"""Resolving a playable link to a meeting's video.

Nobody gives out permanent video URLs, ourselves included. Recall signs an S3
link that dies within hours, and our own bucket signs one the same way, which
makes "the recording URL" a thing you resolve, not a thing you store — the
stored copy is only a cache that spares a round trip on every page view.

The media lives in one of two places, and one function owns picking between
them, so the API and the worker can never disagree about where a meeting's
video is or when its link has gone stale.
"""

from datetime import datetime, timedelta, timezone

from fastapi.concurrency import run_in_threadpool
from sqlalchemy.ext.asyncio import AsyncSession

from core.logging import get_logger
from integrations.meetingbot import get_provider
from integrations.meetingbot.base import MeetingBotError
from integrations.storage import get_storage
from integrations.storage.base import StorageError
from models.meetings import MEDIA_READY_STATUSES, Meeting

log = get_logger(__name__)

# Hand back a link only if it survives long enough to be clicked and opened.
# A URL with nine seconds left is the same as no URL, but worse: it fails in
# the user's player instead of here, where we can just fetch another.
FRESHNESS_MARGIN = timedelta(minutes=5)


async def resolve_recording_url(db: AsyncSession, meeting: Meeting) -> str | None:
    """The meeting's video URL, fresh enough to hand to a browser.

    Wherever the media lives — our bucket for uploads and browser recordings,
    the provider's for calls a bot attended — callers ask this one question and
    get one answer.

    Returns None when there is no video or its host is unreachable. That is
    deliberate: a missing recording is never a reason to fail the page the user
    asked for, or the recap job that has already done the valuable work.

    Safe to call repeatedly — a re-run refreshes an expired link and otherwise
    changes nothing.

    Returns None without calling the provider once retention pruning has run
    on this meeting (`recording_pruned_at` set). Without this check, a pruned
    meeting still has `bot_id` set and a status in `MEDIA_READY_STATUSES` —
    the only thing pruning clears is `recording_id`/`recording_url` — so the
    logic below would treat it exactly like a video that simply hasn't been
    fetched yet, fetch it fresh from the provider, and write it straight back
    onto the row. That would undo the prune on the very next page view.
    """
    if meeting.recording_pruned_at is not None:
        return None
    if meeting.recording_url and _is_fresh(meeting.recording_url_expires_at):
        return meeting.recording_url

    # Our own media, for uploads and browser recordings. Keyed on the
    # confirmation rather than on `media_key`, because a key is reserved before
    # the browser starts sending: presigning one whose bytes never arrived
    # hands the player a URL that 404s. An in-progress live recording has a key
    # and no confirmation, and correctly resolves to nothing here.
    #
    # Deliberately not gated on MEDIA_READY_STATUSES, unlike the provider
    # branch below. There is no assembly step to wait for — the object is
    # complete the moment it is confirmed — so someone who has just stopped
    # recording can play it back while transcription is still queued.
    if meeting.media_confirmed_at is not None and meeting.media_key:
        return await _resolve_own_media(db, meeting)

    if not meeting.bot_id:
        return None
    if meeting.status not in MEDIA_READY_STATUSES:
        # Nothing to fetch yet, and nothing to cache if we did — so opening a
        # meeting that is still running would otherwise cost a provider call
        # every single time the page loads.
        return None

    try:
        media = await run_in_threadpool(get_provider().fetch_recording, meeting.bot_id)
    except MeetingBotError as exc:
        log.info(
            "meetings.recording_unavailable",
            meeting_id=str(meeting.id),
            bot_id=meeting.bot_id,
            error=str(exc),
        )
        return None

    # The recording id is durable, so it is written once and never overwritten;
    # the URL is a cache, so a newer one always wins.
    if media.recording_id and not meeting.recording_id:
        meeting.recording_id = media.recording_id
    if media.video_url:
        meeting.recording_url = media.video_url
        meeting.recording_url_expires_at = media.expires_at
    await db.flush()

    return media.video_url


async def _resolve_own_media(db: AsyncSession, meeting: Meeting) -> str | None:
    """Sign a fresh link to the object in our own bucket, and cache it.

    Mirrors the provider branch's failure contract: an unreachable bucket
    returns None rather than raising, so a storage blip costs the video on this
    page view and never the page itself or the recap job.
    """
    try:
        url, expires_at = await run_in_threadpool(get_storage().presign_get, meeting.media_key)
    except StorageError as exc:
        log.info(
            "meetings.recording_unavailable",
            meeting_id=str(meeting.id),
            media_key=meeting.media_key,
            error=str(exc),
        )
        return None

    meeting.recording_url = url
    meeting.recording_url_expires_at = expires_at
    await db.flush()
    return url


def _is_fresh(expires_at: datetime | None) -> bool:
    # No deadline means we don't know when the link dies, which is not the same
    # as knowing it is alive — re-resolve rather than gamble on it.
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at - FRESHNESS_MARGIN > datetime.now(timezone.utc)
