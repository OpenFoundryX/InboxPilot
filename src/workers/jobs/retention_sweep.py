"""Enforce the per-plan retention windows.

The pricing matrix and the privacy policy both quote these windows, so they are
a commitment rather than a feature. Media and transcripts are cleared; the
summary, decisions, and action items survive — those are what the recap email
already delivered and what users actually return to.
"""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
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

    videos = 0
    stale_media = await db.scalars(
        select(Meeting).where(
            Meeting.user_id == user_id,
            Meeting.starts_at < video_cutoff,
            Meeting.recording_id.isnot(None),
        )
    )
    for meeting in stale_media:
        meeting.recording_id = None
        meeting.recording_url = None
        meeting.recording_url_expires_at = None
        videos += 1

    transcripts = 0
    stale_transcripts = await db.scalars(
        select(Meeting).where(
            Meeting.user_id == user_id,
            Meeting.starts_at < transcript_cutoff,
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
