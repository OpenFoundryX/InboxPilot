"""The single gate for "may this user do this billable thing right now".

This is a plain async function rather than a FastAPI dependency on purpose.
Meeting bots are booked by `workers.jobs.meetings_sweep` and drafts are written
by `workers.jobs.drafts_sweep` — neither runs inside an HTTP request. A guard
that only wrapped API routes would leave the two most expensive operations in
the product completely ungated, which is the opposite of the point.

`dependencies.py` wraps this for the routes that do have a request.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from core.plans import get_plan
from services.billing.access import ACCESS_ENTITLED, effective_plan_id, resolve_access
from services.billing.store import get_subscription
from services.billing.usage import get_or_create_counter

FEATURE_MEETING_BOT = "meetings.bot"
FEATURE_DRAFT = "drafts.generate"
FEATURE_ROUTINE = "routines.run"
FEATURE_CUSTOM_CATEGORIES = "categorization.custom"

REASON_LOCKED = "locked"
REASON_QUOTA = "quota_exhausted"
REASON_PLAN = "not_in_plan"


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str | None = None


ALLOWED = Decision(True)


async def check(
    db: AsyncSession,
    user_id: uuid.UUID,
    feature: str,
    *,
    routine_type: str | None = None,
    now: datetime | None = None,
) -> Decision:
    """Whether `user_id` may use `feature`."""
    now = now or datetime.now(timezone.utc)

    sub = await get_subscription(db, user_id)
    if resolve_access(sub, now) != ACCESS_ENTITLED:
        return Decision(False, REASON_LOCKED)

    entitlements = get_plan(effective_plan_id(sub)).entitlements

    if feature == FEATURE_MEETING_BOT:
        counter = await get_or_create_counter(db, user_id, now)
        if counter.bot_seconds_used >= entitlements.bot_seconds_per_month:
            return Decision(False, REASON_QUOTA)
        return ALLOWED

    if feature == FEATURE_DRAFT:
        if entitlements.drafts_per_month is None:
            return ALLOWED
        counter = await get_or_create_counter(db, user_id, now)
        if counter.drafts_generated >= entitlements.drafts_per_month:
            return Decision(False, REASON_QUOTA)
        return ALLOWED

    if feature == FEATURE_ROUTINE:
        if routine_type not in entitlements.allowed_routines:
            return Decision(False, REASON_PLAN)
        return ALLOWED

    if feature == FEATURE_CUSTOM_CATEGORIES:
        if not entitlements.custom_categories:
            return Decision(False, REASON_PLAN)
        return ALLOWED

    # An unknown feature string is a programming error, not a customer state.
    raise ValueError(f"unknown feature: {feature}")
