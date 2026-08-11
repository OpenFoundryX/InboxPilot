"""Never miss a deadline — extract dates from actionable mail into reminders."""

import json
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from openai import OpenAI
from sqlalchemy import select

from core.config import settings
from core.logging import get_logger
from integrations.google import gmail
from models.reminders import ORIGIN_DEADLINE, Reminder

log = get_logger(__name__)

# Actionable mail is where deadlines usually live.
SCAN_QUERY = 'label:"to do" OR label:"to follow up"'
_SYS = """Extract a single concrete deadline/date the recipient must act on from this
email, if any. Return ONLY JSON: {"has_deadline": bool, "due_at": "YYYY-MM-DDTHH:MM:SS",
"what": "<short action>"}. Use the current time given to resolve relative dates. If the
email has no actionable deadline, return {"has_deadline": false}."""


def _client() -> OpenAI:
    return OpenAI(api_key=settings.OPENAI_API_KEY)


async def scan_deadlines(db, user_id: str, tz: str, lead_hours: int = 24) -> int:
    """Find deadlines in recent actionable mail and create reminders. Returns count."""
    try:
        now = datetime.now(ZoneInfo(tz))
    except Exception:
        now = datetime.now()
    tzinfo = now.tzinfo

    emails = gmail.fetch_by_query(user_id, f"{SCAN_QUERY} newer_than:7d -from:me", 15)
    created = 0
    for e in emails:
        if not e.id:
            continue
        # Skip if we already made a reminder from this message.
        exists = await db.scalar(
            select(Reminder).where(Reminder.source_message_id == e.id)
        )
        if exists:
            continue

        content = (
            f"Current time: {now.isoformat()} ({tz}).\n"
            f"From: {e.sender}\nSubject: {e.subject}\n{(e.body or e.snippet or '')[:1500]}"
        )
        try:
            resp = _client().chat.completions.create(
                model=settings.OPENAI_MODEL,
                temperature=0,
                response_format={"type": "json_object"},
                messages=[{"role": "system", "content": _SYS}, {"role": "user", "content": content}],
            )
            data = json.loads(resp.choices[0].message.content or "{}")
        except Exception:
            log.exception("deadlines.extract_failed", message_id=e.id)
            continue
        if not data.get("has_deadline") or not data.get("due_at"):
            continue

        try:
            due = datetime.fromisoformat(str(data["due_at"]))
        except ValueError:
            continue
        if due.tzinfo is None:
            due = due.replace(tzinfo=tzinfo)
        remind_at = due - timedelta(hours=lead_hours)
        if remind_at < now:
            remind_at = now  # deadline is imminent/past — remind now

        db.add(
            Reminder(
                user_id=uuid.UUID(user_id),
                remind_at=remind_at,
                title=data.get("what") or (e.subject or "Deadline"),
                note=f"Due {due.isoformat()} — from {e.sender}\nRe: {e.subject}",
                thread_id=e.thread_id,
                source_message_id=e.id,
                origin=ORIGIN_DEADLINE,
            )
        )
        created += 1
        log.info("deadlines.reminder_created", message_id=e.id, due=due.isoformat())
    return created
