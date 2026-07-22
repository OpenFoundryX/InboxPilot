"""Celery beat job: release held mail for any user whose delivery slot is due.

Runs every minute. For each active user it computes the current time in their
timezone; if that lands on a delivery slot (and not inside DND), it fetches all
mail the Gmail filter has parked under `inboxos-later` and moves it back to the
inbox as one batch.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from core.database import SessionLocal, run_async
from core.logging import get_logger
from services.mailman import gmail_ops
from services.mailman.scheduler import is_delivery_due
from services.mailman.store import list_active_settings
from workers.celery_app import celery_app

log = get_logger(__name__)


@celery_app.task(name="mailman.tick")
def tick() -> dict:
    """Evaluate every active user's schedule and release due batches."""
    return run_async(_tick())


async def _tick() -> dict:
    now_utc = datetime.now(timezone.utc)
    released_users = 0
    released_msgs = 0

    async with SessionLocal() as db:
        settings_list = await list_active_settings(db)

        for settings in settings_list:
            try:
                tz = ZoneInfo(settings.timezone)
            except Exception:
                tz = timezone.utc
            now_local = now_utc.astimezone(tz)

            # Don't fire twice for the same minute-granular slot.
            if settings.last_delivery_at and now_utc - settings.last_delivery_at < timedelta(
                seconds=90
            ):
                continue

            if not is_delivery_due(settings, now_local, now_utc):
                continue

            uid = str(settings.user_id)
            try:
                message_ids = gmail_ops.held_message_ids(uid)
            except Exception:
                log.exception("mailman.held_lookup_failed", user_id=uid)
                continue

            # Stamp the slot even when there's nothing to release, so interval
            # mode advances and we don't recheck every second.
            settings.last_delivery_at = now_utc

            if not message_ids:
                continue

            try:
                gmail_ops.release(uid, message_ids)
            except Exception:
                log.exception("mailman.release_failed", user_id=uid)
                continue

            released_users += 1
            released_msgs += len(message_ids)
            log.info("mailman.batch_released", user_id=uid, count=len(message_ids))

        await db.commit()

    return {"users": released_users, "messages": released_msgs}
