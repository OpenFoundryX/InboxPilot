"""Turn media we host ourselves into a recap.

The counterpart to `process_meeting`: same destination, different source. That
job asks Recall for a transcript of a call its bot attended; this one transcribes
an object in our own bucket — an uploaded file, or a recording made in the
browser. Both hand off to `pipeline.finalize`, so a recap reads the same either
way.

Enqueued when an upload is confirmed present, never when it is merely announced.
"""

import uuid
from datetime import datetime, timezone

from core.database import run_async, with_worker_session
from core.logging import get_logger
from core.plans import get_plan
from integrations.storage.base import StorageError
from models.meetings import STATUS_FAILED, STATUS_PROCESSED, Meeting
from services.billing.access import effective_plan_id
from services.billing.store import get_subscription
from services.billing.usage import add_bot_seconds
from services.meetings.pipeline import finalize
from services.meetings.transcribe import TranscriptionError, transcribe_key
from workers.celery_app import celery_app

log = get_logger(__name__)

# Longer than the bot path's retry: a failure here usually means a large
# download or transcode died part-way, and retrying that immediately tends to
# fail the same way.
RETRY_DELAY_SECONDS = 600


@celery_app.task(name="meetings.transcribe", bind=True, max_retries=2)
def transcribe_meeting_media(self, meeting_id: str) -> dict:
    try:
        return run_async(with_worker_session(lambda db: _run(db, meeting_id)))
    except (TranscriptionError, StorageError) as exc:
        # Bound to a plain local before the retry: Python clears the `as`
        # binding when the handler exits, and the failure path below needs the
        # reason after `self.retry` has raised through it.
        reason = str(exc)
        try:
            raise self.retry(exc=exc, countdown=RETRY_DELAY_SECONDS) from exc
        except self.MaxRetriesExceededError:
            # Out of retries, so nothing else will revisit this row. Recording
            # why is the difference between a meeting the user can see failed
            # and one that sits on "Processing" forever.
            log.error("meetings.transcribe_gave_up", meeting_id=meeting_id, error=reason)
            run_async(with_worker_session(lambda db: _mark_failed(db, meeting_id, reason)))
            return {"failed": reason}


async def _mark_failed(db, meeting_id: str, reason: str) -> dict:
    meeting = await db.get(Meeting, uuid.UUID(meeting_id))
    if meeting:
        meeting.status = STATUS_FAILED
        meeting.status_detail = f"transcription failed: {reason}"[:200]
    return {"failed": reason}


async def _run(db, meeting_id: str) -> dict:
    meeting = await db.get(Meeting, uuid.UUID(meeting_id))
    if not meeting:
        return {"skipped": "unknown meeting"}
    if meeting.recap_sent_at:
        return {"skipped": "already delivered"}
    if not meeting.media_key or not meeting.media_confirmed_at:
        # Enqueued before the bytes landed, or for a bot meeting that has no
        # business here. Either way there is nothing to transcribe.
        return {"skipped": "no confirmed media"}

    # Stamp the retention windows this meeting is grandfathered against, once,
    # for the same reason the bot path does: a later downgrade must not
    # retroactively shorten a deadline that was already fixed. Guarded on
    # `is None` so a retry cannot overwrite an already-stamped value.
    if meeting.retention_video_days is None:
        sub = await get_subscription(db, meeting.user_id)
        entitlements = get_plan(effective_plan_id(sub)).entitlements
        meeting.retention_video_days = entitlements.video_retention_days
        meeting.retention_transcript_days = entitlements.transcript_retention_days
        await db.flush()

    if not meeting.transcript:
        # By key, not by presigned URL: a signed link points at wherever a
        # browser reaches the bucket, which from in here resolves to this
        # container's own localhost.
        transcript, duration = transcribe_key(meeting.media_key)

        # Metered from the media's own duration, before the empty-transcript
        # return below and guarded on `is None` — same contract as the bot
        # path. A silent recording still cost us a download, a transcode, and a
        # transcription request, and a retry must not charge for them twice.
        if meeting.duration_seconds is None:
            meeting.duration_seconds = int(duration)
            await add_bot_seconds(
                db, meeting.user_id, meeting.duration_seconds, datetime.now(timezone.utc)
            )

        if transcript.is_empty:
            meeting.status = STATUS_PROCESSED
            meeting.status_detail = "empty transcript"
            log.info("meetings.empty_transcript", meeting_id=meeting_id)
            return {"skipped": "empty transcript"}

        meeting.transcript = transcript.render()
        await db.flush()

    # No diarization on this path — the transcription model returns text, not
    # speakers — so the summarizer is told not to guess who committed to what.
    return await finalize(db, meeting, speakers_labelled=False)
