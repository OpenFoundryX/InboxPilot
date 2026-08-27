"""The guest-facing booking surface. No session, no cookie, no account.

Everything reachable from here is reachable by anyone on the internet who has
(or guesses) a slug, and each route spends something real: an availability read
costs a Google Calendar call, and a booking makes a customer's own Google
account email up to eleven people. So every route is rate limited — but on what
the limit is *keyed* matters more than its number.

Behind a proxy that does not forward `X-Forwarded-For`, every guest arrives
wearing the same address, so a per-IP ceiling is a global one: the first
person to exhaust it locks out everybody. The tight limits are therefore keyed
on things that genuinely identify what is being protected — the host whose
Google account sends the invites, and the booking token whose meeting is being
changed. The per-IP guard survives only as a loose flood stop.

The management routes authenticate with the token in the guest's confirmation
email. That token *is* the credential: it is 32 random bytes, it is compared by
unique-column lookup, and possessing it means you are the person who made the
booking or someone they forwarded the mail to. That is the same trust model as
every "manage your booking" link in the industry, and the blast radius is one
meeting.
"""

from datetime import date, datetime, timezone
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status

from api.deps import DbSession
from core import ratelimit
from core.logging import get_logger
from models.scheduling import STATUS_CANCELLED, SchedulingEventType, SchedulingSettings
from models.users import User
from schemas.scheduling import (
    AvailabilityDay,
    AvailabilityRange,
    BookingRead,
    CancelBooking,
    CreateBooking,
    ManagedBooking,
    PublicEventDetail,
    PublicEventType,
    PublicProfile,
    RescheduleBooking,
)
from services.scheduling import availability, booking as booking_service, questions, store
from services.scheduling.slots import timezone_for

log = get_logger(__name__)
router = APIRouter(prefix="/scheduling/public", tags=["scheduling-public"])

#: Per-IP ceilings are deliberately loose — see `client_key` for why an IP here
#: is often not one person. They exist to stop a flood, not to meter a guest.
#: The limits that actually protect anything are the two keyed on something
#: real: the host being booked, and the booking being managed.
CALLER_FLOOD_LIMIT = 60

#: Bookings are the expensive, side-effectful ones: real invites sent from a
#: customer's own Google account. This is the ceiling that matters, because it
#: is keyed on the account at risk rather than on whoever is calling.
BOOKING_LIMIT_PER_HOST = 30
BOOKING_WINDOW = 3600

#: Managing one booking. Keyed on the token, so it bounds what can be done to a
#: single meeting without letting one guest's retries affect anybody else.
#: Reschedules legitimately retry — a guest races someone else for a slot, gets
#: a 409, and picks again — so this has to leave room for that.
MANAGE_LIMIT_PER_TOKEN = 20
MANAGE_WINDOW = 3600

#: A month view is the widest thing the UI asks for; more is a scrape.
MAX_RANGE_DAYS = 62


def client_key(request: Request) -> str:
    """Best available identity for an anonymous caller — which may be nobody.

    `X-Forwarded-For`'s first entry is the client when a proxy sets it. When it
    is absent, `request.client.host` is the *proxy's* address, not the guest's:
    in this deployment that resolves to a single Cloudflare edge IP shared by
    every visitor on earth.

    That is why nothing tight is keyed on this. A per-IP limit over an
    unidentifiable caller is not a rate limit, it is a switch that takes the
    booking page down for everyone as soon as one person is busy — which is
    exactly what a 5-per-hour reschedule ceiling did here.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def _limit(key: str, *, limit: int, window: int, message: str | None = None) -> None:
    if not await ratelimit.allow(key, limit=limit, window_seconds=window):
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            message or "Too many requests. Please wait a moment and try again.",
            headers={"Retry-After": str(window)},
        )


async def _flood_guard(request: Request, name: str) -> None:
    """Loose per-IP backstop. Deliberately not the primary control."""
    await _limit(
        f"sched:{name}:{client_key(request)}",
        limit=CALLER_FLOOD_LIMIT,
        window=MANAGE_WINDOW,
    )


async def _manage_limit(token: str, name: str) -> None:
    """Bound what can be done to one booking, keyed on its own token."""
    await _limit(
        f"sched:manage:{name}:{token}",
        limit=MANAGE_LIMIT_PER_TOKEN,
        window=MANAGE_WINDOW,
        message=(
            "This booking has been changed several times in the last hour. "
            "Please wait a little before trying again."
        ),
    )


async def _profile(db: DbSession, profile_slug: str) -> tuple[SchedulingSettings, User]:
    pair = await store.profile_by_slug(db, profile_slug)
    if pair is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Scheduling link not found")
    return pair


async def _event(
    db: DbSession, profile: SchedulingSettings, event_slug: str
) -> SchedulingEventType:
    row = await store.event_type_by_slug(db, profile.user_id, event_slug, enabled_only=True)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Meeting type not found")
    return row


def _host_name(user: User) -> str:
    return user.full_name or user.email.split("@", 1)[0]


def _public_event(row: SchedulingEventType) -> PublicEventType:
    return PublicEventType(
        slug=row.slug,
        name=row.name,
        description=row.description,
        duration_minutes=row.duration_minutes,
    )


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


@router.get("/{profile_slug}", response_model=PublicProfile)
async def public_profile(profile_slug: str, request: Request, db: DbSession) -> PublicProfile:
    """The host's bookable meeting types — the index page of their link."""
    await _flood_guard(request, "profile")
    profile, user = await _profile(db, profile_slug)
    rows = await store.event_types_for(db, profile.user_id, enabled_only=True)
    return PublicProfile(
        slug=profile.slug,
        host_name=_host_name(user),
        timezone=profile.timezone,
        event_types=[_public_event(row) for row in rows],
    )


@router.get("/{profile_slug}/{event_slug}", response_model=PublicEventDetail)
async def public_event(
    profile_slug: str, event_slug: str, request: Request, db: DbSession
) -> PublicEventDetail:
    """Everything the booking page needs before it asks for availability.

    Includes the bookable date bounds so the calendar can grey out dates it
    cannot serve, instead of letting a guest click one and learn from a 422.
    """
    await _flood_guard(request, "event")
    profile, user = await _profile(db, profile_slug)
    event = await _event(db, profile, event_slug)
    first, last = availability.bookable_range(event, timezone_for(profile.timezone))
    return PublicEventDetail(
        slug=profile.slug,
        host_name=_host_name(user),
        host_timezone=profile.timezone,
        event=_public_event(event),
        questions=event.questions,
        first_bookable_day=first,
        last_bookable_day=last,
    )


# --------------------------------------------------------------------------
# Availability
# --------------------------------------------------------------------------


@router.get("/{profile_slug}/{event_slug}/availability", response_model=AvailabilityRange)
async def public_availability(
    profile_slug: str,
    event_slug: str,
    request: Request,
    db: DbSession,
    start: Annotated[date, Query(alias="from")],
    end: Annotated[date | None, Query(alias="to")] = None,
) -> AvailabilityRange:
    """Slots across a date range, in the host's zone.

    A range rather than a single date deliberately: the guest's calendar shows
    a month, and answering it a day at a time was one Google round trip per
    click. One call now covers the whole view, and dates outside the booking
    window come back empty rather than as an error, so the UI renders a month
    uniformly.
    """
    await _flood_guard(request, "avail")
    end = end or start
    if end < start:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "'to' precedes 'from'")
    if (end - start).days > MAX_RANGE_DAYS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"Ask for at most {MAX_RANGE_DAYS} days at a time",
        )

    profile, _ = await _profile(db, profile_slug)
    event = await _event(db, profile, event_slug)
    try:
        days = await availability.slots_between(db, profile, event, start, end)
    except availability.CalendarUnavailable as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The host's calendar is temporarily unavailable",
        ) from exc

    return AvailabilityRange(
        timezone=profile.timezone,
        duration_minutes=event.duration_minutes,
        days=[AvailabilityDay(date=day, slots=days.get(day, [])) for day in sorted(days)],
    )


# --------------------------------------------------------------------------
# Booking
# --------------------------------------------------------------------------


@router.post(
    "/{profile_slug}/{event_slug}/book",
    response_model=BookingRead,
    status_code=status.HTTP_201_CREATED,
)
async def book(
    profile_slug: str,
    event_slug: str,
    payload: CreateBooking,
    request: Request,
    db: DbSession,
) -> BookingRead:
    profile, user = await _profile(db, profile_slug)
    event = await _event(db, profile, event_slug)

    # Two ceilings. The caller limit stops one script; the host limit stops a
    # distributed one from turning a customer's Google account into a mailer.
    await _flood_guard(request, "book")
    await _limit(
        f"sched:book:host:{profile.user_id}",
        limit=BOOKING_LIMIT_PER_HOST,
        window=BOOKING_WINDOW,
        message=(
            "This host has taken a lot of bookings in the last hour. Please try again shortly."
        ),
    )

    try:
        row = await booking_service.create(
            db,
            profile,
            event,
            user,
            starts_at=payload.starts_at,
            name=payload.name,
            email=str(payload.email),
            attendee_emails=[str(e) for e in payload.attendee_emails],
            notes=payload.notes,
            answers=payload.answers,
        )
    except questions.InvalidAnswers as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except booking_service.SlotTaken as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except booking_service.CalendarFailed as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The meeting could not be booked. Please try again.",
        ) from exc
    return BookingRead.model_validate(row)


# --------------------------------------------------------------------------
# Managing a booking with the token from the confirmation email
# --------------------------------------------------------------------------

manage_router = APIRouter(prefix="/scheduling/manage", tags=["scheduling-public"])


@manage_router.get("/{token}", response_model=ManagedBooking)
async def view_booking(token: str, request: Request, db: DbSession) -> ManagedBooking:
    await _flood_guard(request, "view")
    await _manage_limit(token, "view")
    row = await store.booking_by_token(db, token)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Booking not found")

    profile = await store.settings_for_user(db, row.user_id)
    user = await db.get(User, row.user_id)
    if profile is None or user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Booking not found")

    event = await db.get(SchedulingEventType, row.event_type_id) if row.event_type_id else None
    return ManagedBooking(
        booking=BookingRead.model_validate(row),
        host_name=_host_name(user),
        host_timezone=profile.timezone,
        profile_slug=profile.slug,
        event_slug=event.slug if event else None,
        can_reschedule=(
            event is not None
            and row.status != STATUS_CANCELLED
            and row.starts_at > datetime.now(timezone.utc)
        ),
    )


@manage_router.post("/{token}/cancel", response_model=BookingRead)
async def cancel_own_booking(
    token: str, payload: CancelBooking, request: Request, db: DbSession
) -> BookingRead:
    await _flood_guard(request, "cancel")
    await _manage_limit(token, "cancel")
    row = await store.booking_by_token(db, token)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Booking not found")

    profile = await store.settings_for_user(db, row.user_id)
    user = await db.get(User, row.user_id)
    if profile is None or user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Booking not found")

    result = await booking_service.cancel(db, row, profile, user, by="guest", reason=payload.reason)
    return BookingRead.model_validate(result)


@manage_router.post("/{token}/reschedule", response_model=BookingRead)
async def reschedule_own_booking(
    token: str, payload: RescheduleBooking, request: Request, db: DbSession
) -> BookingRead:
    await _flood_guard(request, "reschedule")
    await _manage_limit(token, "reschedule")
    row = await store.booking_by_token(db, token)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Booking not found")

    profile = await store.settings_for_user(db, row.user_id)
    user = await db.get(User, row.user_id)
    event = await db.get(SchedulingEventType, row.event_type_id) if row.event_type_id else None
    if profile is None or user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Booking not found")
    if event is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This meeting type is no longer offered, so it cannot be moved. "
            "Cancel it and book a new time instead.",
        )

    try:
        result = await booking_service.reschedule(
            db, row, profile, event, user, starts_at=payload.starts_at
        )
    except booking_service.SlotTaken as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return BookingRead.model_validate(result)


@manage_router.get("/{token}/availability", response_model=AvailabilityRange)
async def reschedule_availability(
    token: str,
    request: Request,
    db: DbSession,
    start: Annotated[date, Query(alias="from")],
    end: Annotated[date | None, Query(alias="to")] = None,
) -> AvailabilityRange:
    """Slots offered when moving this booking, ignoring its own reservation."""
    await _flood_guard(request, "manage-avail")
    await _manage_limit(token, "manage-avail")
    row = await store.booking_by_token(db, token)
    if row is None or row.event_type_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Booking not found")

    profile = await store.settings_for_user(db, row.user_id)
    event = await db.get(SchedulingEventType, row.event_type_id)
    if profile is None or event is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Booking not found")

    end = end or start
    if end < start or (end - start).days > MAX_RANGE_DAYS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Invalid date range")

    try:
        days = await availability.slots_between(
            db, profile, event, start, end, exclude_booking_id=row.id
        )
    except availability.CalendarUnavailable as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The host's calendar is temporarily unavailable",
        ) from exc

    return AvailabilityRange(
        timezone=profile.timezone,
        duration_minutes=event.duration_minutes,
        days=[AvailabilityDay(date=day, slots=days.get(day, [])) for day in sorted(days)],
    )
