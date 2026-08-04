"""Turn a finished recording into a recap: transcript, summary, reminders, email.

Enqueued by the provider webhook when a bot reports `done`. Every step persists
before the next begins, so a failure part-way through is recoverable by simply
running the task again — the transcript download is the expensive part and it
only happens once.
"""

import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from core.database import run_async, with_worker_session
from core.logging import get_logger
from core.plans import get_plan
from integrations.meetingbot import get_provider
from integrations.meetingbot.base import MeetingBotError
from models.meetings import (
    STATUS_DELIVERED,
    STATUS_PROCESSED,
    Meeting,
)
from models.reminders import ORIGIN_MEETING, Reminder
from models.users import User
from services.billing.access import effective_plan_id
from services.billing.store import get_subscription
from services.billing.usage import add_bot_seconds
from services.meetings.recap import compose_recap
from services.meetings.recording import resolve_recording_url
from services.meetings.store import get_or_create_settings
from services.meetings.summarize import summarize
from services.notify import send_to_inbox
from workers.celery_app import celery_app

log = get_logger(__name__)

RETRY_DELAY_SECONDS = 300
# Same lead as extracted email deadlines, so reminders behave consistently
# however they were discovered.
REMINDER_LEAD_HOURS = 24


@celery_app.task(name="meetings.process", bind=True, max_retries=3)
def process_meeting(self, meeting_id: str) -> dict:
    try:
        return run_async(with_worker_session(lambda db: _process(db, meeting_id)))
    except MeetingBotError as exc:
        # Media can lag the `done` webhook, and the provider can blip. Retrying
        # is nearly free because nothing has been persisted yet.
        log.warning("meetings.process_retry", meeting_id=meeting_id, error=str(exc))
        raise self.retry(exc=exc, countdown=RETRY_DELAY_SECONDS) from exc


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

    extracted = summarize(
        meeting.transcript,
        title=meeting.title,
        started_at=meeting.starts_at,
        attendees=meeting.attendees,
    )
    if not extracted:
        # The transcript is saved, so re-running costs one LLM call.
        meeting.status_detail = "summarization failed"
        log.warning("meetings.summarize_empty", meeting_id=meeting_id)
        return {"skipped": "summarization failed"}

    meeting.summary = extracted["summary"]
    meeting.decisions = extracted["decisions"]
    meeting.action_items = extracted["action_items"]
    meeting.status = STATUS_PROCESSED
    meeting.status_detail = None
    await db.flush()

    settings_row = await get_or_create_settings(db, meeting.user_id)
    reminders = 0
    if settings_row.create_reminders:
        reminders = _create_reminders(db, meeting)

    user = await db.get(User, meeting.user_id)
    sent = False
    if settings_row.email_recap and user and user.email:
        subject, body = compose_recap(meeting)
        try:
            send_to_inbox(str(meeting.user_id), user.email, subject, body)
            meeting.recap_sent_at = datetime.now(timezone.utc)
            meeting.status = STATUS_DELIVERED
            sent = True
        except Exception:
            # Summary is already persisted and readable via the API; a failed
            # send shouldn't lose the work or block reminders.
            log.exception("meetings.recap_send_failed", meeting_id=meeting_id)

    log.info(
        "meetings.processed",
        meeting_id=meeting_id,
        action_items=len(meeting.action_items),
        reminders=reminders,
        recap_sent=sent,
    )
    return {"processed": True, "reminders": reminders, "recap_sent": sent}


def _create_reminders(db, meeting: Meeting) -> int:
    """One reminder per action item that came with a date.

    Undated commitments are deliberately skipped — a reminder needs a time, and
    inventing one turns a useful nudge into noise.
    """
    now = datetime.now(timezone.utc)
    created = 0
    for item in meeting.action_items or []:
        due = _parse_due(item.get("due_at"))
        if not due:
            continue
        remind_at = max(due - timedelta(hours=REMINDER_LEAD_HOURS), now)
        owner = f" ({item['owner']})" if item.get("owner") else ""
        db.add(
            Reminder(
                user_id=meeting.user_id,
                remind_at=remind_at,
                title=str(item["what"])[:300],
                note=(
                    f"From meeting: {meeting.title or 'untitled'}{owner}\n"
                    f"Due {due.isoformat()}"
                ),
                origin=ORIGIN_MEETING,
            )
        )
        created += 1
    return created


def _parse_due(value) -> datetime | None:
    if not value:
        return None
    try:
        due = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return due.replace(tzinfo=timezone.utc) if due.tzinfo is None else due
