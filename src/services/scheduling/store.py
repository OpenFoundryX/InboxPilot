"""Persistence for scheduling: profiles, event types, overrides, reservations.

The API layer asks questions in domain terms ("what is this user's profile",
"which windows apply on this date") and this module answers them in SQL. Two
things here are subtler than they look and are the reason it isn't inline in
the router: creating a profile has to survive two browser tabs racing, and slug
allocation has to survive losing that race to a different user.
"""

import re
import secrets
import uuid
from datetime import date, datetime

from sqlalchemy import Select, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.scheduling import (
    LIVE_STATUSES,
    SchedulingBooking,
    SchedulingDateOverride,
    SchedulingEventType,
    SchedulingSettings,
)
from models.users import User
from services.scheduling.slots import DEFAULT_WEEKLY_HOURS

#: How many slug candidates to try before giving up. Collisions are rare; six
#: attempts ending in random suffixes is far more than enough to never be hit.
_SLUG_ATTEMPTS = 6

DEFAULT_EVENT_TYPES = [
    {"slug": "15min", "name": "15 Minute Meeting", "duration_minutes": 15, "position": 0},
    {"slug": "30min", "name": "30 Minute Meeting", "duration_minutes": 30, "position": 1},
]


def slugify(source: str, fallback: str = "meet") -> str:
    return re.sub(r"[^a-z0-9]+", "-", source.lower()).strip("-")[:60] or fallback


def _candidate(base: str, attempt: int) -> str:
    if attempt == 0:
        return base
    if attempt < 4:
        return f"{base}-{attempt + 1}"
    return f"{base}-{secrets.token_hex(2)}"


def _base_slug(user: User) -> str:
    return slugify(user.full_name or user.email.split("@", 1)[0])


async def get_or_create_settings(db: AsyncSession, user: User) -> SchedulingSettings:
    """The user's profile, creating it (with default event types) on first ask.

    This is reached from a GET, and the dashboard calls it from two components
    at once — which React's StrictMode helpfully doubles again in development.
    So the insert is `ON CONFLICT DO NOTHING` over a re-select rather than
    check-then-insert: whoever loses the race reads the winner's row instead of
    raising a unique violation at the user.

    The conflict clause covers `user_id` only. A slug colliding with a
    *different* user is a different constraint and still raises, which is why
    each attempt runs in a savepoint and the loop moves to the next candidate.
    """
    row = await db.scalar(select(SchedulingSettings).where(SchedulingSettings.user_id == user.id))
    if row is not None:
        return row

    base = _base_slug(user)
    for attempt in range(_SLUG_ATTEMPTS):
        try:
            async with db.begin_nested():
                await db.execute(
                    pg_insert(SchedulingSettings)
                    .values(
                        id=uuid.uuid4(),
                        user_id=user.id,
                        slug=_candidate(base, attempt),
                        weekly_hours=DEFAULT_WEEKLY_HOURS,
                    )
                    .on_conflict_do_nothing(index_elements=["user_id"])
                )
        except IntegrityError:
            continue  # slug taken by someone else — try the next candidate

        row = await db.scalar(
            select(SchedulingSettings).where(SchedulingSettings.user_id == user.id)
        )
        if row is not None:
            await ensure_default_event_types(db, user.id)
            return row

    raise RuntimeError("Could not allocate a scheduling slug")


async def ensure_default_event_types(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Give a brand-new profile something bookable. No-op once any type exists."""
    existing = await db.scalar(
        select(func.count())
        .select_from(SchedulingEventType)
        .where(SchedulingEventType.user_id == user_id)
    )
    if existing:
        return
    for spec in DEFAULT_EVENT_TYPES:
        await db.execute(
            pg_insert(SchedulingEventType)
            .values(id=uuid.uuid4(), user_id=user_id, questions=[], **spec)
            .on_conflict_do_nothing(constraint="uq_event_type_slug_per_user")
        )


async def allocate_event_type_slug(db: AsyncSession, user_id: uuid.UUID, desired: str) -> str:
    """A slug free for this user, suffixing the desired one if it is taken."""
    base = slugify(desired, fallback="meeting")
    taken = set(
        await db.scalars(
            select(SchedulingEventType.slug).where(SchedulingEventType.user_id == user_id)
        )
    )
    for attempt in range(_SLUG_ATTEMPTS):
        candidate = _candidate(base, attempt)
        if candidate not in taken:
            return candidate
    return f"{base}-{secrets.token_hex(3)}"


async def profile_by_slug(db: AsyncSession, slug: str) -> tuple[SchedulingSettings, User] | None:
    """A public profile and its owner, or None when the link is off or unknown."""
    result = await db.execute(
        select(SchedulingSettings, User)
        .join(User, User.id == SchedulingSettings.user_id)
        .where(SchedulingSettings.slug == slug, SchedulingSettings.enabled.is_(True))
    )
    pair = result.one_or_none()
    return (pair[0], pair[1]) if pair is not None else None


async def settings_for_user(db: AsyncSession, user_id: uuid.UUID) -> SchedulingSettings | None:
    """A profile by owner, without creating one.

    The management endpoints need the host's zone to render a guest's booking,
    and a booking cannot exist without a profile — so unlike the dashboard's
    read, absence here is a genuine "not found" rather than "not set up yet".
    """
    return await db.scalar(select(SchedulingSettings).where(SchedulingSettings.user_id == user_id))


async def event_types_for(
    db: AsyncSession, user_id: uuid.UUID, *, enabled_only: bool = False
) -> list[SchedulingEventType]:
    stmt: Select = select(SchedulingEventType).where(SchedulingEventType.user_id == user_id)
    if enabled_only:
        stmt = stmt.where(SchedulingEventType.enabled.is_(True))
    rows = await db.scalars(
        stmt.order_by(SchedulingEventType.position, SchedulingEventType.created_at)
    )
    return list(rows)


async def event_type_by_slug(
    db: AsyncSession, user_id: uuid.UUID, slug: str, *, enabled_only: bool = False
) -> SchedulingEventType | None:
    stmt = select(SchedulingEventType).where(
        SchedulingEventType.user_id == user_id, SchedulingEventType.slug == slug
    )
    if enabled_only:
        stmt = stmt.where(SchedulingEventType.enabled.is_(True))
    return await db.scalar(stmt)


async def overrides_between(
    db: AsyncSession, user_id: uuid.UUID, first: date, last: date
) -> dict[date, list[dict]]:
    """Date overrides in a range, keyed by date.

    Returned as a dict so a caller can tell "no override" (key absent) from
    "override that blocks the day" (key present, empty list) — a distinction
    `slots.windows_for` depends on.
    """
    rows = await db.execute(
        select(SchedulingDateOverride.day, SchedulingDateOverride.windows).where(
            SchedulingDateOverride.user_id == user_id,
            SchedulingDateOverride.day >= first,
            SchedulingDateOverride.day <= last,
        )
    )
    return {day: windows for day, windows in rows.all()}


async def live_bookings_between(
    db: AsyncSession,
    user_id: uuid.UUID,
    starts_after: datetime,
    ends_before: datetime,
    *,
    exclude_id: uuid.UUID | None = None,
) -> list[tuple[datetime, datetime]]:
    """Reserved intervals that overlap the range and still hold their slot.

    `exclude_id` lets a reschedule ignore the booking being moved, so a guest
    shifting 10:00 to 10:30 isn't blocked by their own reservation.
    """
    stmt = select(SchedulingBooking.starts_at, SchedulingBooking.ends_at).where(
        SchedulingBooking.user_id == user_id,
        SchedulingBooking.status.in_(LIVE_STATUSES),
        SchedulingBooking.starts_at < ends_before,
        SchedulingBooking.ends_at > starts_after,
    )
    if exclude_id is not None:
        stmt = stmt.where(SchedulingBooking.id != exclude_id)
    return [(start, end) for start, end in (await db.execute(stmt)).all()]


async def count_bookings_on(
    db: AsyncSession,
    user_id: uuid.UUID,
    event_type_id: uuid.UUID,
    day_start: datetime,
    day_end: datetime,
    *,
    exclude_id: uuid.UUID | None = None,
) -> int:
    """Live bookings of one event type within a day, for the per-day cap."""
    stmt = (
        select(func.count())
        .select_from(SchedulingBooking)
        .where(
            SchedulingBooking.user_id == user_id,
            SchedulingBooking.event_type_id == event_type_id,
            SchedulingBooking.status.in_(LIVE_STATUSES),
            SchedulingBooking.starts_at >= day_start,
            SchedulingBooking.starts_at < day_end,
        )
    )
    if exclude_id is not None:
        stmt = stmt.where(SchedulingBooking.id != exclude_id)
    return int(await db.scalar(stmt) or 0)


async def booking_by_token(db: AsyncSession, token: str) -> SchedulingBooking | None:
    return await db.scalar(
        select(SchedulingBooking).where(SchedulingBooking.management_token == token)
    )
