"""Turn a bot's finished recording into a recap.

Enqueued by the provider webhook when a bot reports `done`. This job owns the
bot-specific half — fetching the vendor's transcript and metering the call —
and hands off to `services.meetings.pipeline.finalize` for everything from the
transcript onward, which it shares with transcribed uploads.

Every step persists before the next begins, so a failure part-way through is
recoverable by simply running the task again — the transcript download is the
expensive part and it only happens once.
"""

import uuid
from dataclasses import asdict
from datetime import datetime, timezone

from core.database import run_async, with_worker_session
from core.logging import get_logger
from core.plans import get_plan
from integrations.meetingbot import get_provider
from integrations.meetingbot.base import MeetingBotError, MeetingNotRecorded
from models.meetings import STATUS_FAILED, STATUS_PROCESSED, Meeting
from services.billing.access import effective_plan_id
from services.billing.store import get_subscription
from services.billing.usage import add_bot_seconds
from services.meetings.pipeline import finalize
from services.meetings.recording import resolve_recording_url
from workers.celery_app import celery_app

log = get_logger(__name__)

RETRY_DELAY_SECONDS = 300


@celery_app.task(name="meetings.process", bind=True, max_retries=3)
def process_meeting(self, meeting_id: str) -> dict:
    try:
        return run_async(with_worker_session(lambda db: _process(db, meeting_id)))
    except MeetingNotRecorded as exc:
        # Nothing was captured and nothing ever will be. Retrying would spend
        # fifteen minutes confirming that while the meeting sits in the list
        # claiming to be processing.
        log.info("meetings.not_recorded", meeting_id=meeting_id, error=str(exc))
        return run_async(
            with_worker_session(
                lambda db: _fail(db, meeting_id, "the notetaker was never admitted to the call")
            )
        )
    except MeetingBotError as exc:
        # Media can lag the `done` webhook, and the provider can blip. Retrying
        # is nearly free because nothing has been persisted yet.
        log.warning("meetings.process_retry", meeting_id=meeting_id, error=str(exc))
        reason = str(exc)
        try:
            raise self.retry(exc=exc, countdown=RETRY_DELAY_SECONDS) from exc
        except self.MaxRetriesExceededError:
            # Out of retries, so nothing will revisit this row. Without this it
            # keeps the status it had when the webhook fired and shows as
            # "Processing" indefinitely — a meeting that looks busy forever is
            # worse than one that says it failed.
            log.error("meetings.process_gave_up", meeting_id=meeting_id, error=reason)
            return run_async(with_worker_session(lambda db: _fail(db, meeting_id, reason)))


async def _fail(db, meeting_id: str, reason: str) -> dict:
    """Record why a meeting produced nothing, so the UI can stop waiting."""
    meeting = await db.get(Meeting, uuid.UUID(meeting_id))
    if meeting:
        meeting.status = STATUS_FAILED
        meeting.status_detail = reason[:200]
    return {"failed": reason}


def compute_duration_seconds(meeting: Meeting, payload: dict, now: datetime) -> int:
    """How long the bot was in the call, in seconds.

    The provider's own figure is authoritative when present. Falling back to
    wall-clock since `joined_at` keeps a provider that omits it from silently
    metering as zero, which would make bot-hours free.
    """
    reported = payload.get("duration_seconds")
    if isinstance(reported, (int, float)) and reported >= 0:
        return int(reported)
    if meeting.joined_at is None:
        return 0
    return max(0, int((now - meeting.joined_at).total_seconds()))


def _backfill_from_provider(meeting: Meeting) -> None:
    """Fill in what only the bot could know: the title, who was there, when.

    A meeting booked from a pasted link starts with none of this — there is no
    calendar event to copy it from, so the row is created with a null title, no
    start time and nobody in it, which is why the list showed "Untitled
    meeting" and "Time unknown". The bot has since sat in the call and can
    answer all three.

    Only ever fills gaps. A calendar meeting already carries the organiser's
    title and invitee list, and those are better than what the platform reports
    from inside the room — Meet, in particular, reports no title at all.

    Failure is swallowed: this is decoration on a recap whose real content is
    already in hand, and no provider hiccup should cost the summary.
    """
    if meeting.title and meeting.starts_at and meeting.attendees:
        return
    try:
        details = get_provider().fetch_details(meeting.bot_id)
    except MeetingBotError as exc:
        log.info("meetings.details_unavailable", meeting_id=str(meeting.id), error=str(exc))
        return

    if details.title and not meeting.title:
        meeting.title = details.title[:300]
    if details.participants and not meeting.attendees:
        meeting.attendees = details.participants

    # A calendar meeting's scheduled time is the one people recognise, so it
    # stands even though the bot joined a minute either side of it. A pasted
    # link has only the provisional stamp written when it was requested, and
    # the moment recording actually began is strictly better than that — so
    # here the provider wins.
    if details.started_at and (meeting.starts_at is None or meeting.calendar_event_id is None):
        meeting.starts_at = details.started_at


async def _process(db, meeting_id: str) -> dict:
    meeting = await db.get(Meeting, uuid.UUID(meeting_id))
    if not meeting:
        return {"skipped": "unknown meeting"}
    if meeting.recap_sent_at:
        return {"skipped": "already delivered"}
    if not meeting.bot_id:
        return {"skipped": "no bot"}

    # Stamp the windows this meeting is grandfathered against, once. Retention
    # pruning must not let a later plan change (a downgrade in particular)
    # retroactively shorten a deadline that was already fixed, so it needs to
    # know what the account's plan actually was right now, at capture time —
    # not what it happens to be whenever the sweep gets around to this row.
    # Guarded on `is None` so a retry can't overwrite an already-stamped value
    # with whatever the plan happens to be by the time it retries.
    if meeting.retention_video_days is None:
        sub = await get_subscription(db, meeting.user_id)
        entitlements = get_plan(effective_plan_id(sub)).entitlements
        meeting.retention_video_days = entitlements.video_retention_days
        meeting.retention_transcript_days = entitlements.transcript_retention_days

    # Claim the video first, before the transcript can end this run early: a
    # call where nobody spoke still produced a recording worth watching. It
    # swallows its own provider errors, so this line can never cost the recap.
    await resolve_recording_url(db, meeting)

    _backfill_from_provider(meeting)

    # Populated only when this run is the one that freshly fetched the
    # transcript; a retry that finds `meeting.transcript` already stored (or a
    # meeting whose transcript was already populated before duration metering
    # existed) has no provider object left to read a duration off, and falls
    # back to wall-clock below.
    transcript_payload: dict = {}
    transcript_is_empty = False
    if not meeting.transcript:
        transcript = get_provider().fetch_transcript(meeting.bot_id)
        transcript_payload = asdict(transcript)
        transcript_is_empty = transcript.is_empty
        if not transcript_is_empty:
            meeting.transcript = transcript.render()
        await db.flush()

    # Metered before the empty-transcript early return below, and gated on
    # `duration_seconds is None` rather than on which branch ran above: a bot
    # that sat through a silent call still occupied real wall-clock time we
    # pay the provider for, so skipping it here would make those bot-hours
    # free. `process_meeting` declares retries, so a re-run must not meter the
    # same call twice — a Starter user's whole month is one meeting's worth of
    # bot-hours.
    if meeting.duration_seconds is None:
        meeting.duration_seconds = compute_duration_seconds(
            meeting, transcript_payload, now=datetime.now(timezone.utc)
        )
        await add_bot_seconds(
            db, meeting.user_id, meeting.duration_seconds, datetime.now(timezone.utc)
        )

    if transcript_is_empty:
        # A bot that sat in a waiting room, or a call where nobody spoke.
        # Silence is not worth an email.
        meeting.status = STATUS_PROCESSED
        meeting.status_detail = "empty transcript"
        log.info("meetings.empty_transcript", meeting_id=meeting_id)
        return {"skipped": "empty transcript"}

    # The vendor diarizes, so the summarizer can trust speaker attribution here
    # in a way it cannot for our own transcripts.
    return await finalize(db, meeting, speakers_labelled=True)
