"""From a stored transcript to a delivered recap.

The half of meeting processing that does not care where the transcript came
from. A bot's diarized transcript and a transcribed upload reach this point
identically, and from here they are the same meeting: summarize, persist,
create reminders for dated commitments, send the recap.

Extracted so the two jobs that produce transcripts — `process_meeting` for bot
calls, `transcribe_media` for our own media — cannot drift. Without one owner
for this, a recap from an upload quietly stops matching one from a bot the
first time either job is touched.

Async, session-bound; called from Celery tasks inside `with_worker_session`.
"""

from datetime import datetime, timedelta, timezone

from core.logging import get_logger
from models.meetings import STATUS_DELIVERED, STATUS_PROCESSED, Meeting
from models.reminders import ORIGIN_MEETING, Reminder
from models.users import User
from services.meetings.recap import compose_recap
from services.meetings.store import get_or_create_settings
from services.meetings.summarize import summarize
from services.notify import send_to_inbox

log = get_logger(__name__)

# Same lead as extracted email deadlines, so reminders behave consistently
# however they were discovered.
REMINDER_LEAD_HOURS = 24


async def finalize(db, meeting: Meeting, *, speakers_labelled: bool = True) -> dict:
    """Summarize a meeting's stored transcript and deliver the recap.

    `speakers_labelled` says whether the transcript names who said what. Bot
    transcripts do; ours do not, and the summarizer has to be told, or it
    attributes commitments to whoever it finds in the attendee list.

    Expects `meeting.transcript` to be set and non-empty. Safe to re-run: the
    transcript is already persisted, so a retry costs one LLM call, and a
    meeting whose recap already went out is caught by the caller's guard.
    """
    extracted = summarize(
        meeting.transcript,
        title=meeting.title,
        started_at=meeting.starts_at,
        attendees=meeting.attendees,
        speakers_labelled=speakers_labelled,
    )
    if not extracted:
        # The transcript is saved, so re-running costs one LLM call.
        meeting.status_detail = "summarization failed"
        log.warning("meetings.summarize_empty", meeting_id=str(meeting.id))
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
            log.exception("meetings.recap_send_failed", meeting_id=str(meeting.id))

    log.info(
        "meetings.processed",
        meeting_id=str(meeting.id),
        source=meeting.source,
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
