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
from datetime import UTC, datetime, timedelta, timezone

from sqlalchemy import select

from core.database import run_async, with_worker_session
from core.logging import get_logger
from core.plans import get_plan
from models.drafts import DraftSettings
from services.billing.access import effective_plan_id
from services.billing.entitlements import FEATURE_DRAFT, check
from services.billing.store import get_subscription
from services.billing.usage import add_drafts, get_or_create_counter
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


async def may_draft(db, user_id: uuid.UUID, now: datetime) -> bool:
    """Whether this user's plan still allows a generated draft."""
    return (await check(db, user_id, FEATURE_DRAFT, now=now)).allowed


async def remaining_drafts(db, user_id: uuid.UUID, now: datetime) -> int | None:
    """Drafts left in this user's monthly quota. None means unlimited."""
    sub = await get_subscription(db, user_id)
    allowance = get_plan(effective_plan_id(sub)).entitlements.drafts_per_month
    if allowance is None:
        return None
    counter = await get_or_create_counter(db, user_id, now)
    return max(0, allowance - counter.drafts_generated)


async def _due_users(db, field: str, delta: timedelta) -> list[tuple[str, DraftConfig, int | None]]:
    """Load config and remaining quota for every enabled user who is due.

    A user who is locked or already at quota is filtered out here rather than
    loaded and then skipped later, so the entitlement check happens once per
    user per pass instead of being scattered across both callers.
    """
    rows = await users_with_drafting_enabled(db)
    now = datetime.now(timezone.utc)
    out: list[tuple[str, DraftConfig, int | None]] = []
    for row in rows:
        if not _is_due(getattr(row, field), delta):
            continue
        if not await may_draft(db, row.user_id, now):
            log.info("drafts.skipped_no_entitlement", user_id=str(row.user_id))
            continue
        budget = await remaining_drafts(db, row.user_id, now)
        out.append((str(row.user_id), await load_config(db, row.user_id), budget))
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
    due: list[tuple[str, DraftConfig, int | None]] = run_async(
        with_worker_session(lambda db: _due_users(db, "last_sweep_at", delta))
    )

    created = 0
    for user_id, config, budget in due:
        # Stamp before working, not after. A sweep that dies partway through
        # would otherwise be retried by the next beat tick immediately, and its
        # Gmail queries plus LLM calls would run again from the top.
        run_async(with_worker_session(lambda db, u=user_id: _stamp(db, uuid.UUID(u), "last_sweep_at")))
        try:
            # `budget` is this user's remaining monthly quota, computed by
            # `_due_users` at the top of this run. Handing it to `sweep_user`
            # is what keeps the quota exact rather than per-sweep: without it,
            # a user sitting at 19 of 20 drafts would get a whole
            # `MAX_PER_SWEEP` batch and finish over quota.
            made = sweep.sweep_user(user_id, config, budget=budget)
            created += made
            if made:
                run_async(
                    with_worker_session(
                        lambda db, u=user_id, n=made: add_drafts(
                            db, uuid.UUID(u), n, datetime.now(timezone.utc)
                        )
                    )
                )
        except Exception:
            log.exception("drafts.sweep_user_failed", user_id=user_id)

    return {"users": len(due), "drafts_created": created}


@celery_app.task(name="drafts.follow_up")
def drafts_follow_up() -> dict:
    """Daily follow-up nudges for every user who is due."""
    delta = timedelta(hours=FOLLOW_UP_INTERVAL_HOURS)
    due: list[tuple[str, DraftConfig, int | None]] = run_async(
        with_worker_session(lambda db: _due_users(db, "last_follow_up_at", delta))
    )

    nudges = 0
    for user_id, config, budget in due:
        if not config.follow_up_enabled:
            continue
        run_async(
            with_worker_session(
                lambda db, u=user_id: _stamp(db, uuid.UUID(u), "last_follow_up_at")
            )
        )
        try:
            # Follow-up nudges are drafts too, and `_due_users` already excluded
            # anyone locked or at 0 remaining. `budget` is not threaded into
            # `follow_up.sweep_user` (unlike the catch-up sweep) — that
            # function's own `MAX_PER_SWEEP` is 5, small enough that this pass
            # is not the exactness-critical path the brief calls out, and its
            # signature is not part of this task's interface. Usage is still
            # recorded so the counter — and therefore the next sweep's
            # `remaining_drafts` — stays accurate.
            made = follow_up.sweep_user(user_id, config)
            nudges += made
            if made:
                run_async(
                    with_worker_session(
                        lambda db, u=user_id, n=made: add_drafts(
                            db, uuid.UUID(u), n, datetime.now(timezone.utc)
                        )
                    )
                )
        except Exception:
            log.exception("drafts.follow_up_user_failed", user_id=user_id)

    return {"users": len(due), "follow_ups_created": nudges}
