"""Resolving a playable link to a meeting's video.

Providers do not give out permanent video URLs. Recall signs an S3 link that
dies within hours, which makes "the recording URL" a thing you resolve, not a
thing you store — the stored copy is only a cache that spares the provider a
call on every page view.

One function owns that decision so the API and the worker can never disagree
about when a link has gone stale.
"""

from datetime import datetime, timedelta, timezone

from fastapi.concurrency import run_in_threadpool
from sqlalchemy.ext.asyncio import AsyncSession

from core.logging import get_logger
from integrations.meetingbot import get_provider
from integrations.meetingbot.base import MeetingBotError
from models.meetings import MEDIA_READY_STATUSES, Meeting

log = get_logger(__name__)

# Hand back a link only if it survives long enough to be clicked and opened.
# A URL with nine seconds left is the same as no URL, but worse: it fails in
# the user's player instead of here, where we can just fetch another.
FRESHNESS_MARGIN = timedelta(minutes=5)


async def resolve_recording_url(db: AsyncSession, meeting: Meeting) -> str | None:
    """The meeting's video URL, fresh enough to hand to a browser.

    Returns None when there is no video or the provider is unreachable. That is
    deliberate: a missing recording is never a reason to fail the page the user
    asked for, or the recap job that has already done the valuable work.

    Safe to call repeatedly — a re-run refreshes an expired link and otherwise
    changes nothing.
    """
    if not meeting.bot_id:
        return None
    if meeting.recording_url and _is_fresh(meeting.recording_url_expires_at):
        return meeting.recording_url
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


def _is_fresh(expires_at: datetime | None) -> bool:
    # No deadline means we don't know when the link dies, which is not the same
    # as knowing it is alive — re-resolve rather than gamble on it.
    if expires_at is None:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at - FRESHNESS_MARGIN > datetime.now(timezone.utc)
