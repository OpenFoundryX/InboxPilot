"""Celery beat job: run due user routines (briefings, digests, nudges).

Every minute it finds enabled routines whose local run-time matches now (and
weekday, for weekly ones) and dispatches each to its per-type handler. A Redis
lock prevents overlapping runs from double-sending.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from core.database import run_async, with_worker_session
from core.locks import single_run
from core.logging import get_logger
from integrations.composio import gmail
from models.routines import ROUTINE_BRIEFING, ROUTINE_CHASE_THREADS, Routine
from models.users import User
from services.digest.briefing import compose_briefing
from services.digest.nudges import chase_open_threads
from services.mailman.store import get_or_create_settings
from workers.celery_app import celery_app

log = get_logger(__name__)


@celery_app.task(name="routines.sweep")
def sweep() -> dict:
    with single_run("routines.sweep") as acquired:
        if not acquired:
            return {"skipped": "locked"}
        return run_async(with_worker_session(_sweep))


def _hm(value: str) -> int:
    h, m = value.split(":")
    return int(h) * 60 + int(m)


async def _sweep(db) -> dict:
    now_utc = datetime.now(timezone.utc)
    ran = 0
    routines = list(await db.scalars(select(Routine).where(Routine.enabled.is_(True))))

    for routine in routines:
        user = await db.get(User, routine.user_id)
        if not user or not user.email:
            continue
        settings = await get_or_create_settings(db, routine.user_id)
        try:
            tz = ZoneInfo(settings.timezone)
        except Exception:
            tz = timezone.utc
        now_local = now_utc.astimezone(tz)

        # Don't run twice for the same minute-granular slot.
        if routine.last_run_at and now_utc - routine.last_run_at < timedelta(seconds=90):
            continue
        if routine.weekday is not None and now_local.weekday() != routine.weekday:
            continue
        if now_local.hour * 60 + now_local.minute != _hm(routine.run_time):
            continue

        try:
            _run_routine(routine, str(routine.user_id), user.email, settings.timezone)
        except Exception:
            log.exception("routines.run_failed", type=routine.type, user_id=str(routine.user_id))
            continue
        routine.last_run_at = now_utc
        ran += 1

    await db.commit()
    return {"ran": ran}


def _run_routine(routine: Routine, user_id: str, email: str, tz: str) -> None:
    if routine.type == ROUTINE_BRIEFING:
        subject, body = compose_briefing(user_id, tz)
        gmail.send_email(user_id, email, subject, body)
        log.info("routines.briefing_sent", user_id=user_id)
        return
    if routine.type == ROUTINE_CHASE_THREADS:
        chase_open_threads(user_id, email, email)
        return
    log.warning("routines.unknown_type", type=routine.type, user_id=user_id)
