"""Celery tasks: the periodic draft passes.

Two tasks, both beat-driven, both deciding for themselves who is due — the same
shape as `mailman_tick`. The beat fires often and cheaply; the per-user gate is a
timestamp on `draft_settings`, mirroring `MailmanSettings.last_delivery_at`.

`drafts.sweep` is the catch-up pass over recent mail; `drafts.follow_up` writes
the daily nudges. They are split because their natural cadences differ by an
order of magnitude, and folding them together would mean either sweeping too
rarely or nudging too often.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from core.database import run_async, with_worker_session
from core.logging import get_logger
from models.drafts import DraftSettings
from services.drafts import follow_up, sweep
from services.drafts.context import DraftConfig, load_config
from services.drafts.store import users_with_drafting_enabled
from workers.celery_app import celery_app

log = get_logger(__name__)

# How long after a catch-up pass before a user is due for another.
SWEEP_INTERVAL_MINUTES = 15
# Follow-ups are daily. Anything more often would nudge the same person twice
# about the same silence.
FOLLOW_UP_INTERVAL_HOURS = 24


def _is_due(last: datetime | None, delta: timedelta) -> bool:
    if last is None:
        return True
    # A naive timestamp would raise on the comparison; the column is timezone-aware,
    # but a row written before that was true would not be.
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return datetime.now(UTC) - last >= delta


async def _due_users(db, field: str, delta: timedelta) -> list[tuple[str, DraftConfig]]:
    """Load config for every enabled user who is due, in one session."""
    rows = await users_with_drafting_enabled(db)
    out: list[tuple[str, DraftConfig]] = []
    for row in rows:
        if not _is_due(getattr(row, field), delta):
            continue
        out.append((str(row.user_id), await load_config(db, row.user_id)))
    return out


async def _stamp(db, user_id: uuid.UUID, field: str) -> None:
    row = await db.scalar(select(DraftSettings).where(DraftSettings.user_id == user_id))
    if row is not None:
        setattr(row, field, datetime.now(UTC))


@celery_app.task(name="drafts.sweep")
def drafts_sweep() -> dict:
    """Catch-up pass for every user who is due."""
    delta = timedelta(minutes=SWEEP_INTERVAL_MINUTES)
    # Annotated because `with_worker_session(fn: Any) -> T` gives mypy nothing to
    # infer T from. Same reason at every call site below.
    due: list[tuple[str, DraftConfig]] = run_async(
        with_worker_session(lambda db: _due_users(db, "last_sweep_at", delta))
    )

    created = 0
    for user_id, config in due:
        # Stamp before working, not after. A sweep that dies partway through
        # would otherwise be retried by the next beat tick immediately, and its
        # Gmail queries plus LLM calls would run again from the top.
        run_async(with_worker_session(lambda db, u=user_id: _stamp(db, uuid.UUID(u), "last_sweep_at")))
        try:
            created += sweep.sweep_user(user_id, config)
        except Exception:
            log.exception("drafts.sweep_user_failed", user_id=user_id)

    return {"users": len(due), "drafts_created": created}


@celery_app.task(name="drafts.follow_up")
def drafts_follow_up() -> dict:
    """Daily follow-up nudges for every user who is due."""
    delta = timedelta(hours=FOLLOW_UP_INTERVAL_HOURS)
    due: list[tuple[str, DraftConfig]] = run_async(
        with_worker_session(lambda db: _due_users(db, "last_follow_up_at", delta))
    )

    nudges = 0
    for user_id, config in due:
        if not config.follow_up_enabled:
            continue
        run_async(
            with_worker_session(
                lambda db, u=user_id: _stamp(db, uuid.UUID(u), "last_follow_up_at")
            )
        )
        try:
            nudges += follow_up.sweep_user(user_id, config)
        except Exception:
            log.exception("drafts.follow_up_user_failed", user_id=user_id)

    return {"users": len(due), "follow_ups_created": nudges}
