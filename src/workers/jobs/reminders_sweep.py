"""Celery beat job: deliver reminders whose time has come.

Every minute it finds undelivered reminders due now, and delivers each: threaded
into the source conversation when there is one, otherwise as a fresh email.
"""

from datetime import datetime, timezone

from sqlalchemy import select

from core.database import run_async, with_worker_session
from core.locks import single_run
from core.logging import get_logger
from integrations.composio import gmail
from models.reminders import Reminder
from models.users import User
from workers.celery_app import celery_app

log = get_logger(__name__)


@celery_app.task(name="reminders.sweep")
def sweep() -> dict:
    with single_run("reminders.sweep") as acquired:
        if not acquired:
            return {"skipped": "locked"}
        return run_async(with_worker_session(_sweep))


async def _sweep(db) -> dict:
    now = datetime.now(timezone.utc)
    due = list(
        await db.scalars(
            select(Reminder).where(
                Reminder.delivered_at.is_(None), Reminder.remind_at <= now
            )
        )
    )
    delivered = 0
    for r in due:
        user = await db.get(User, r.user_id)
        if not user or not user.email:
            continue
        try:
            _deliver(str(r.user_id), user.email, r)
        except Exception:
            log.exception("reminders.deliver_failed", reminder_id=str(r.id))
            continue
        r.delivered_at = now
        delivered += 1

    await db.commit()
    return {"delivered": delivered}


def _deliver(user_id: str, email: str, r: Reminder) -> None:
    body = f"⏰ Reminder: {r.title}"
    if r.note:
        body += f"\n\n{r.note}"
    body += "\n\n— InboxOS"
    if r.thread_id:
        gmail.reply_in_thread(user_id, r.thread_id, email, body)
    else:
        gmail.send_email(user_id, email, f"⏰ Reminder: {r.title}"[:120], body)
    log.info("reminders.delivered", user_id=user_id, title=r.title)
