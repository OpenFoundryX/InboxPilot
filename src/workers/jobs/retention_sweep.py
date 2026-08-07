"""Enforce the per-plan retention windows.

The pricing matrix and the privacy policy both quote these windows, so they are
a commitment rather than a feature. Media and transcripts are cleared; the
summary, decisions, and action items survive — those are what the recap email
already delivered and what users actually return to.

Media lives in one of two places and clearing it means two different things.
The bot vendor holds its own recordings and enforces its own retention, so
dropping our pointer is the whole job. Uploads and browser recordings are in
our bucket, where the object has to be deleted outright.

Two policy calls, both irreversible if gotten wrong, so both are made
explicitly rather than falling out of a shortcut:

- A locked account (no subscription, or one in a terminal status) is not
  pruned at all. `effective_plan_id`'s Starter fallback exists for
  entitlements, not for a one-way deletion path — pruning resumes once the
  account resubscribes.
- Each meeting is pruned against the window it was captured under
  (`Meeting.retention_video_days`/`retention_transcript_days`), not the
  account's current plan, so a downgrade can't retroactively shorten a
  deadline that was already fixed. Legacy rows with no stored window fall
  back to the current plan.
"""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import run_async, with_worker_session
from core.locks import single_run
from core.logging import get_logger
from core.plans import get_plan
from models.meetings import Meeting
from models.users import User
from services.billing.access import ACCESS_LOCKED, effective_plan_id, resolve_access
from services.billing.store import get_subscription
from services.meetings.media import discard
from workers.celery_app import celery_app

log = get_logger(__name__)


def _captured_at(meeting: Meeting) -> datetime:
    """When a meeting happened, for aging purposes.

    Ad-hoc (paste-a-link) meetings never get a calendar `starts_at` — it stays
    NULL for the life of the row. `created_at` is the next best anchor: it's
    non-null on every row and, for an ad-hoc meeting, is set at the moment the
    call was requested — close enough to "when this happened" for a window
    measured in days.
    """
    return meeting.starts_at if meeting.starts_at is not None else meeting.created_at


def _past_window(meeting: Meeting, window_days: int, now: datetime) -> bool:
    return now - _captured_at(meeting) > timedelta(days=window_days)


async def prune_user(db: AsyncSession, user_id: uuid.UUID, now: datetime) -> dict:
    sub = await get_subscription(db, user_id)

    # Locked means "cannot use the product right now", not "begin deleting
    # their data sooner." A subscription-less or terminal-status account gets
    # `effective_plan_id`'s Starter fallback for *entitlements*, but pruning is
    # a one-way door, so it uses the same access check the API and every other
    # sweep use rather than inferring lock state from a missing row itself —
    # a `cancelled`/`halted`/`expired` row must be treated identically to no
    # row at all. Pruning resumes once the account resubscribes.
    if resolve_access(sub, now) == ACCESS_LOCKED:
        return {"videos": 0, "transcripts": 0}

    entitlements = get_plan(effective_plan_id(sub)).entitlements

    videos = 0
    # Both places media can live. Selecting on `recording_id` alone would match
    # only meetings a bot attended, silently exempting every upload and browser
    # recording from a window the pricing page states as a commitment — and
    # leaving their objects in our bucket forever.
    stale_media = await db.scalars(
        select(Meeting).where(
            Meeting.user_id == user_id,
            or_(Meeting.recording_id.isnot(None), Meeting.media_key.isnot(None)),
        )
    )
    for meeting in stale_media:
        # A meeting's own stored window (set once, at processing time, from
        # the plan in force then) grandfathers it against a later plan change
        # — a downgrade must not retroactively shorten a deadline that was
        # already fixed. Null only for legacy rows recorded before this column
        # existed, which fall back to the current plan for lack of anything
        # else to grandfather them against.
        window_days = meeting.retention_video_days
        if window_days is None:
            window_days = entitlements.video_retention_days
        if not _past_window(meeting, window_days, now):
            continue

        # Media in our own bucket has to actually be deleted. For a bot
        # recording, dropping the pointer is the whole job — the vendor holds
        # the bytes and enforces its own retention — but for ours, forgetting
        # the key without removing the object is how a retention promise
        # becomes fiction and storage grows forever.
        if meeting.media_key:
            if not await discard(meeting):
                # The object is still there. Leaving the key set is what makes
                # the next sweep able to try again; clearing it now would
                # orphan the object beyond anything's reach.
                continue
            meeting.media_key = None
            meeting.media_confirmed_at = None

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
            Meeting.transcript.isnot(None),
        )
    )
    for meeting in stale_transcripts:
        window_days = meeting.retention_transcript_days
        if window_days is None:
            window_days = entitlements.transcript_retention_days
        if not _past_window(meeting, window_days, now):
            continue
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
