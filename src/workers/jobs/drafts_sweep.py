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
from core.locks import single_run
from core.logging import get_logger
from core.plans import get_plan
from models.drafts import DraftSettings
from services.billing.access import effective_plan_id
from services.billing.entitlements import FEATURE_DRAFT, check
from services.billing.store import get_subscription
from services.billing.usage import get_or_create_counter
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

# Shared between `drafts_sweep` and `drafts_follow_up` rather than one key per
# task. Both tasks compute a per-user `budget` from the same monthly counter
# in `_due_users` and then spend from that snapshot — `sweep.sweep_user` and
# `follow_up.sweep_user` respectively. A lock scoped to just one task would
# stop that task overlapping itself but would do nothing about the two tasks
# overlapping *each other*: each would read the same remaining quota and each
# spend it in full, for up to `follow_up.MAX_PER_SWEEP` (5) drafts over quota
# in the worst case (see the module's exactness discussion). A shared key
# closes both. The cost is that whichever task's beat tick fires while the
# other holds the lock skips that tick entirely — acceptable here because
# both are catch-up jobs whose next run recovers the skip, whereas an
# overshoot past the quota is not something a later run can undo.
DRAFTS_LOCK = "drafts.quota"
# Rough end-to-end cost of one generation: the LLM call, the Gmail draft create,
# and the label write that marks the source message. Measured loosely, and only
# ever used to size the lock below — it does not need to be exact, it needs to
# not be optimistic.
SECONDS_PER_DRAFT = 30

# `single_run`'s own default (300s) equals `drafts.sweep`'s beat interval exactly
# (`beat_schedule.py`), so a pass running its full TTL would let the lock expire
# mid-run, the next tick acquire, and both passes spend the same stale `budget`
# snapshot — exactly the overshoot this lock exists to prevent.
#
# Derived from the ceiling rather than written as a number, because the two must
# move together: `sweep` now clears its whole window in one pass instead of ten
# drafts per tick, so the worst-case run got an order of magnitude longer, and a
# hand-written TTL would silently stop covering it the next time the ceiling is
# raised. A test asserts this stays ahead of the worst case.
#
# The cost of a TTL this long is that a worker killed outright (no `finally`)
# wedges the key for its remainder, and `drafts.follow_up` shares it. Follow-ups
# are daily, so a delayed tick recovers on the next hour; an overshot monthly
# quota does not recover at all. See `core.locks.single_run`'s docstring for the
# separate, unfixed fencing-token gap this doesn't address.
DRAFTS_LOCK_TTL = int(sweep.SWEEP_SAFETY_CEILING * SECONDS_PER_DRAFT * 1.2)


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
        decision = await check(db, row.user_id, FEATURE_DRAFT, now=now)
        if not decision.allowed:
            log.info(
                "drafts.skipped_no_entitlement",
                user_id=str(row.user_id),
                reason=decision.reason,
            )
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
    with single_run(DRAFTS_LOCK, ttl=DRAFTS_LOCK_TTL) as acquired:
        if not acquired:
            return {"skipped": "locked"}

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
            run_async(
                with_worker_session(lambda db, u=user_id: _stamp(db, uuid.UUID(u), "last_sweep_at"))
            )
            try:
                # `budget` is this user's remaining monthly quota, computed by
                # `_due_users` at the top of this run. Handing it to `sweep_user`
                # is what keeps the quota exact: a pass now clears its whole
                # window rather than a fixed batch, so without `budget` a user
                # sitting at 19 of 20 drafts would draft the entire backlog and
                # finish far over quota. It is the *only* bound that applies on a
                # metered plan — `sweep.SWEEP_SAFETY_CEILING` sits behind it for
                # unlimited plans. The lock above is what makes that snapshot
                # trustworthy for the length of this run.
                #
                # Metering happens inside `services.drafts.create` now, once per
                # draft actually made, not here in bulk — that is the one funnel
                # every draft-producing caller shares, so counting it a second
                # time here would double it.
                made = sweep.sweep_user(user_id, config, budget=budget)
                created += made
            except Exception:
                log.exception("drafts.sweep_user_failed", user_id=user_id)

        return {"users": len(due), "drafts_created": created}


@celery_app.task(name="drafts.follow_up")
def drafts_follow_up() -> dict:
    """Daily follow-up nudges for every user who is due."""
    with single_run(DRAFTS_LOCK, ttl=DRAFTS_LOCK_TTL) as acquired:
        if not acquired:
            return {"skipped": "locked"}

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
                # Follow-up nudges are drafts too and draw on the same monthly
                # quota, so `budget` is capped here exactly as it is for the
                # catch-up sweep — otherwise a user near their limit could take a
                # full `follow_up.MAX_PER_SWEEP` batch of nudges and land over
                # quota within this single pass. The shared lock (see
                # `DRAFTS_LOCK`) is what stops this task and `drafts_sweep`
                # from both spending the same stale budget snapshot at once.
                #
                # Metering happens inside `services.drafts.create`, once per
                # draft actually made — not here in bulk, or it would double count.
                made = follow_up.sweep_user(user_id, config, budget=budget)
                nudges += made
            except Exception:
                log.exception("drafts.follow_up_user_failed", user_id=user_id)

        return {"users": len(due), "follow_ups_created": nudges}
