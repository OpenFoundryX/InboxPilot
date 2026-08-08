"""Celery beat job: remind guests about meetings that are about to start.

This is what makes `reschedule_reminders` a setting rather than a switch that
does nothing. Google's own invite reminders fire into the guest's calendar; the
value of this one is that it arrives as mail and carries the manage link, so
the guest who realises they can't make it can move the meeting themselves
instead of leaving the host with an empty slot.

`reminder_sent_at` is the idempotency guard. It is stamped whether or not the
send succeeded, deliberately: a Gmail outage should cost one reminder, not
retry a growing backlog into every guest's inbox once the outage clears.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from core.database import run_async, with_worker_session
from core.locks import single_run
from core.logging import get_logger
from models.scheduling import LIVE_STATUSES, SchedulingBooking, SchedulingSettings
from models.users import User
from services.scheduling import notifications
from services.scheduling.slots import timezone_for
from workers.celery_app import celery_app

log = get_logger(__name__)

#: How far ahead of a meeting the reminder goes out.
LEAD_MINUTES = 60

#: Meetings whose reminder window opened longer ago than this are skipped
#: rather than sent late — a reminder for a meeting that started twenty minutes
#: ago is worse than no reminder.
STALE_AFTER_MINUTES = 30


@celery_app.task(name="scheduling.reminders")
def sweep() -> dict:
    with single_run("scheduling.reminders") as acquired:
        if not acquired:
            return {"skipped": "locked"}
        return run_async(with_worker_session(_sweep))


async def _sweep(db) -> dict:
    now = datetime.now(timezone.utc)
    due = list(
        await db.scalars(
            select(SchedulingBooking).where(
                SchedulingBooking.reminder_sent_at.is_(None),
                SchedulingBooking.status.in_(LIVE_STATUSES),
                SchedulingBooking.starts_at <= now + timedelta(minutes=LEAD_MINUTES),
                SchedulingBooking.starts_at
                >= now - timedelta(minutes=STALE_AFTER_MINUTES),
            )
        )
    )

    sent = 0
    for booking in due:
        profile = await db.scalar(
            select(SchedulingSettings).where(
                SchedulingSettings.user_id == booking.user_id
            )
        )
        user = await db.get(User, booking.user_id)
        # Stamp regardless of what happens below: this row has had its one
        # chance, and the alternative is a queue that redelivers forever.
        booking.reminder_sent_at = now
        if profile is None or user is None or not profile.reschedule_reminders:
            continue

        host_name = user.full_name or user.email.split("@", 1)[0]
        tz = timezone_for(profile.timezone)
        if notifications.send(
            str(booking.user_id),
            booking.booker_email,
            f"Reminder: {booking.title} with {host_name}",
            notifications.reminder_body(booking, host_name, tz),
        ):
            sent += 1

    await db.commit()
    return {"due": len(due), "sent": sent}
