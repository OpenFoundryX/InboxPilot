"""Enforce the per-plan retention windows.

The pricing matrix and the privacy policy both quote these windows, so they are
a commitment rather than a feature. Media and transcripts are cleared; the
summary, decisions, and action items survive — those are what the recap email
already delivered and what users actually return to.
"""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import run_async, with_worker_session
from core.locks import single_run
from core.logging import get_logger
from core.plans import get_plan
from models.meetings import Meeting
from models.users import User
from services.billing.access import effective_plan_id
from services.billing.store import get_subscription
from workers.celery_app import celery_app

log = get_logger(__name__)


async def prune_user(db: AsyncSession, user_id: uuid.UUID, now: datetime) -> dict:
    sub = await get_subscription(db, user_id)
    entitlements = get_plan(effective_plan_id(sub)).entitlements

    video_cutoff = now - timedelta(days=entitlements.video_retention_days)
    transcript_cutoff = now - timedelta(days=entitlements.transcript_retention_days)

    # Ad-hoc (paste-a-link) meetings never get a calendar `starts_at` — it stays
    # NULL for the life of the row. `NULL < cutoff` is NULL, not true, so those
    # meetings would otherwise never age out on any plan. `created_at` is the
    # next best anchor: it's non-null on every row and, for an ad-hoc meeting,
    # is set at the moment the call was requested — close enough to "when this
    # happened" for a retention window measured in days.
    meeting_age = func.coalesce(Meeting.starts_at, Meeting.created_at)

    videos = 0
    stale_media = await db.scalars(
        select(Meeting).where(
            Meeting.user_id == user_id,
            meeting_age < video_cutoff,
            Meeting.recording_id.isnot(None),
        )
    )
    for meeting in stale_media:
        meeting.recording_id = None
        meeting.recording_url = None
        meeting.recording_url_expires_at = None
        # Marks this as deliberately pruned, not merely "never had a video" —
        # `resolve_recording_url` checks this before re-fetching from the
        # provider, so the prune can't be undone by the next page view.
        meeting.recording_pruned_at = now
        videos += 1

    transcripts = 0
    stale_transcripts = await db.scalars(
        select(Meeting).where(
            Meeting.user_id == user_id,
            meeting_age < transcript_cutoff,
            Meeting.transcript.isnot(None),
        )
    )
    for meeting in stale_transcripts:
        meeting.transcript = None
        transcripts += 1

    await db.flush()
    return {"videos": videos, "transcripts": transcripts}


@celery_app.task(name="retention.sweep")
def sweep() -> dict:
    with single_run("retention.sweep") as acquired:
        if not acquired:
            return {"skipped": "locked"}
        return run_async(with_worker_session(_sweep))


async def _sweep(db) -> dict:
    now = datetime.now(timezone.utc)
    videos = 0
    transcripts = 0
    users = 0

    for user_id in await db.scalars(select(User.id)):
        users += 1
        try:
            result = await prune_user(db, user_id, now)
        except Exception:
            log.exception("retention.prune_failed", user_id=str(user_id))
            continue
        videos += result["videos"]
        transcripts += result["transcripts"]

    await db.commit()
    return {"users": users, "videos": videos, "transcripts": transcripts}
