"""Host-side scheduling: profile, event types, availability overrides, bookings.

Every route here requires a session. The guest-facing half lives in
`scheduling_public` — a separate module rather than a few unauthenticated
routes mixed in among these, because "which of these endpoints can a stranger
call" should be answerable by looking at the file a route is in, not by
checking each signature for a missing dependency.

Reads take `CurrentUser`; writes take `EntitledUser`. A lapsed account can
still see and export what it has, and its existing booking links keep working
for guests — cutting off meetings other people have already scheduled punishes
the wrong party — but it cannot create new ones.
"""

import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from api.deps import DbSession
from core.logging import get_logger
from integrations.google import calendar
from models.scheduling import (
    LIVE_STATUSES,
    SchedulingBooking,
    SchedulingDateOverride,
    SchedulingEventType,
)
from models.users import User
from schemas.scheduling import (
    BookingRead,
    CancelBooking,
    DateOverrideRead,
    DateOverrideUpsert,
    EventTypeCreate,
    EventTypeRead,
    EventTypeUpdate,
    SchedulingSettingsRead,
    SchedulingSettingsUpdate,
)
from services.auth.dependencies import get_current_user
from services.billing.dependencies import EntitledUser
from services.scheduling import availability, booking as booking_service, questions, store
from services.scheduling.slots import timezone_for

log = get_logger(__name__)
router = APIRouter(prefix="/scheduling", tags=["scheduling"])
CurrentUser = Annotated[User, Depends(get_current_user)]


def _settings_read(row) -> SchedulingSettingsRead:
    return SchedulingSettingsRead.model_validate(row)


def _event_read(row: SchedulingEventType, profile_slug: str) -> EventTypeRead:
    return EventTypeRead.model_validate(row).model_copy(update={"profile_slug": profile_slug})


# --------------------------------------------------------------------------
# Profile
# --------------------------------------------------------------------------


@router.get("/settings", response_model=SchedulingSettingsRead)
async def get_settings(user: CurrentUser, db: DbSession) -> SchedulingSettingsRead:
    """The profile, created on first read.

    See `store.get_or_create_settings` for why creating inside a GET is safe
    against the concurrent calls the dashboard actually makes.
    """
    return _settings_read(await store.get_or_create_settings(db, user))


@router.put("/settings", response_model=SchedulingSettingsRead)
async def update_settings(
    payload: SchedulingSettingsUpdate, user: EntitledUser, db: DbSession
) -> SchedulingSettingsRead:
    row = await store.get_or_create_settings(db, user)
    values = payload.model_dump(exclude_unset=True)

    if "timezone" in values:
        try:
            timezone_for(values["timezone"])
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    if "weekly_hours" in values:
        values["weekly_hours"] = [w.model_dump() for w in payload.weekly_hours or []]

    for key, value in values.items():
        setattr(row, key, value)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "That scheduling link is already taken"
        ) from exc

    await availability.invalidate_busy(row.user_id)
    return _settings_read(row)


# --------------------------------------------------------------------------
# Event types
# --------------------------------------------------------------------------


@router.get("/event-types", response_model=list[EventTypeRead])
async def list_event_types(user: CurrentUser, db: DbSession) -> list[EventTypeRead]:
    profile = await store.get_or_create_settings(db, user)
    rows = await store.event_types_for(db, user.id)
    return [_event_read(row, profile.slug) for row in rows]


@router.post("/event-types", response_model=EventTypeRead, status_code=status.HTTP_201_CREATED)
async def create_event_type(
    payload: EventTypeCreate, user: EntitledUser, db: DbSession
) -> EventTypeRead:
    profile = await store.get_or_create_settings(db, user)
    slug = await store.allocate_event_type_slug(db, user.id, payload.slug or payload.name)

    row = SchedulingEventType(
        id=uuid.uuid4(),
        user_id=user.id,
        slug=slug,
        questions=questions.normalise_definitions([q.model_dump() for q in payload.questions]),
        **payload.model_dump(exclude={"slug", "questions"}),
    )
    db.add(row)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "That event link is already taken") from exc
    return _event_read(row, profile.slug)


async def _owned_event_type(
    db: DbSession, user_id: uuid.UUID, event_type_id: uuid.UUID
) -> SchedulingEventType:
    row = await db.scalar(
        select(SchedulingEventType).where(
            SchedulingEventType.id == event_type_id,
            SchedulingEventType.user_id == user_id,
        )
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Event type not found")
    return row


@router.patch("/event-types/{event_type_id}", response_model=EventTypeRead)
async def update_event_type(
    event_type_id: uuid.UUID, payload: EventTypeUpdate, user: EntitledUser, db: DbSession
) -> EventTypeRead:
    profile = await store.get_or_create_settings(db, user)
    row = await _owned_event_type(db, user.id, event_type_id)

    values = payload.model_dump(exclude_unset=True)
    if "questions" in values:
        values["questions"] = questions.normalise_definitions(
            [q.model_dump() for q in payload.questions or []]
        )
    for key, value in values.items():
        setattr(row, key, value)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "That event link is already taken") from exc

    await availability.invalidate_busy(user.id)
    return _event_read(row, profile.slug)


@router.delete("/event-types/{event_type_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_event_type(
    event_type_id: uuid.UUID, user: EntitledUser, db: DbSession
) -> Response:
    """Remove an event type. Meetings already booked through it survive.

    The booking's FK is ON DELETE SET NULL and its title is copied onto the row
    at booking time, so deleting a type leaves the host's history readable and
    the guest's calendar entry intact. It only stops new bookings.
    """
    row = await _owned_event_type(db, user.id, event_type_id)
    await db.delete(row)
    await db.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------
# Date overrides
# --------------------------------------------------------------------------


@router.get("/overrides", response_model=list[DateOverrideRead])
async def list_overrides(
    user: CurrentUser,
    db: DbSession,
    days: Annotated[int, Query(ge=1, le=365)] = 120,
) -> list[SchedulingDateOverride]:
    profile = await store.get_or_create_settings(db, user)
    today = datetime.now(timezone_for(profile.timezone)).date()
    rows = await db.scalars(
        select(SchedulingDateOverride)
        .where(
            SchedulingDateOverride.user_id == user.id,
            SchedulingDateOverride.day >= today,
            SchedulingDateOverride.day <= today + timedelta(days=days),
        )
        .order_by(SchedulingDateOverride.day)
    )
    return list(rows)


@router.put("/overrides", response_model=DateOverrideRead)
async def upsert_override(
    payload: DateOverrideUpsert, user: EntitledUser, db: DbSession
) -> SchedulingDateOverride:
    row = await db.scalar(
        select(SchedulingDateOverride).where(
            SchedulingDateOverride.user_id == user.id,
            SchedulingDateOverride.day == payload.day,
        )
    )
    windows = [w.model_dump() for w in payload.windows]
    if row is None:
        row = SchedulingDateOverride(
            id=uuid.uuid4(),
            user_id=user.id,
            day=payload.day,
            windows=windows,
            note=payload.note,
        )
        db.add(row)
    else:
        row.windows = windows
        row.note = payload.note
    await db.flush()
    await availability.invalidate_busy(user.id)
    return row


@router.delete("/overrides/{day}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_override(day: date, user: EntitledUser, db: DbSession) -> Response:
    """Drop an override so the date falls back to the weekly pattern."""
    await db.execute(
        delete(SchedulingDateOverride).where(
            SchedulingDateOverride.user_id == user.id,
            SchedulingDateOverride.day == day,
        )
    )
    await availability.invalidate_busy(user.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------
# Bookings
# --------------------------------------------------------------------------


@router.get("/bookings", response_model=list[BookingRead])
async def list_bookings(
    user: CurrentUser,
    db: DbSession,
    upcoming: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[SchedulingBooking]:
    stmt = select(SchedulingBooking).where(SchedulingBooking.user_id == user.id)
    if upcoming:
        stmt = stmt.where(
            SchedulingBooking.starts_at >= datetime.now(timezone.utc),
            SchedulingBooking.status.in_(LIVE_STATUSES),
        ).order_by(SchedulingBooking.starts_at)
    else:
        stmt = stmt.order_by(SchedulingBooking.starts_at.desc())
    return list(await db.scalars(stmt.limit(limit)))


@router.post("/bookings/{booking_id}/cancel", response_model=BookingRead)
async def cancel_booking(
    booking_id: uuid.UUID, payload: CancelBooking, user: CurrentUser, db: DbSession
) -> SchedulingBooking:
    """Host-side cancellation. The guest is emailed and the event withdrawn."""
    row = await db.scalar(
        select(SchedulingBooking).where(
            SchedulingBooking.id == booking_id, SchedulingBooking.user_id == user.id
        )
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Booking not found")
    profile = await store.get_or_create_settings(db, user)
    return await booking_service.cancel(db, row, profile, user, by="host", reason=payload.reason)


@router.get("/blockers")
async def calendar_blockers(
    user: CurrentUser, days: Annotated[int, Query(ge=1, le=30)] = 14
) -> list[dict]:
    """Upcoming calendar events that suppress slots on the booking link."""
    now = datetime.now(timezone.utc)
    try:
        windows = await run_in_threadpool(
            calendar.busy_windows, str(user.id), now, now + timedelta(days=days)
        )
    except Exception as exc:
        log.exception("scheduling.blockers_unavailable", user_id=str(user.id))
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Calendar blockers are temporarily unavailable",
        ) from exc
    return [{"starts_at": start.isoformat(), "ends_at": end.isoformat()} for start, end in windows]
