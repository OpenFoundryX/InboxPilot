"""Booking, cancelling, and rescheduling — the parts with side effects.

The ordering in `create` is the whole point of this module and is not
negotiable: **commit the reservation, then talk to Google.**

The obvious sequence — insert, call the calendar, set the status, let the
request's session commit at the end — cannot record a failure. The FastAPI
session dependency rolls back on any exception, so raising a 503 after setting
`status = "failed"` discards the status *and* the booking row it was on. The
handler looks like it keeps an audit trail and keeps nothing. It also holds a
write transaction open across a multi-second third-party call.

So: commit `pending` first (which takes the slot, under the exclusion
constraint, so nobody else can), then create the event, then commit the outcome
either way. A booking that exists as `failed` is recoverable and visible. A
booking that never existed is neither.
"""

import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi.concurrency import run_in_threadpool
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.logging import get_logger
from integrations.google import calendar
from models.scheduling import (
    STATUS_CANCELLED,
    STATUS_CONFIRMED,
    STATUS_FAILED,
    STATUS_PENDING,
    SchedulingBooking,
    SchedulingEventType,
    SchedulingSettings,
)
from models.users import User
from services.scheduling import availability, notifications, questions
from services.scheduling.slots import timezone_for

log = get_logger(__name__)


class SlotTaken(RuntimeError):
    """The requested time is no longer on offer."""


class CalendarFailed(RuntimeError):
    """The reservation is held, but no calendar event could be created."""


def new_management_token() -> str:
    """A guest's only credential, so it is random rather than derived.

    32 URL-safe bytes: not guessable by anyone who knows a booking id, an email
    address, or a slug.
    """
    return secrets.token_urlsafe(32)


def _host_name(user: User) -> str:
    return user.full_name or user.email.split("@", 1)[0]


def _attendees(booking: SchedulingBooking) -> list[str]:
    return list(dict.fromkeys([booking.booker_email, *booking.attendee_emails]))


async def _notify(user_id: str, to: str, subject: str, body: str) -> None:
    """Queue a notification, falling back to sending it inline.

    Queued because a Gmail send is a second Google round trip on a request that
    has already done everything the caller is waiting for. The fallback matters
    because these emails carry the guest's manage link: if the broker is down,
    a slow confirmation still beats a guest with no way back to their booking.
    """
    from workers.jobs.scheduling_notify import notify

    try:
        notify.delay(user_id, to, subject, body)
    except Exception:
        log.warning("scheduling.notify_enqueue_failed", to=to)
        await run_in_threadpool(notifications.send, user_id, to, subject, body)


async def create(
    db: AsyncSession,
    settings: SchedulingSettings,
    event_type: SchedulingEventType,
    user: User,
    *,
    starts_at: datetime,
    name: str,
    email: str,
    attendee_emails: list[str],
    notes: str | None,
    answers: dict,
) -> SchedulingBooking:
    """Reserve a slot and put it on the host's calendar.

    Raises `SlotTaken` if the time went while the guest was filling the form,
    and `CalendarFailed` if the reservation stuck but Google refused — in which
    case the booking is persisted as `failed`, not silently dropped.
    """
    if not await availability.is_bookable(db, settings, event_type, starts_at):
        raise SlotTaken("That time is no longer available")

    cleaned = questions.validate(event_type.questions, answers)
    starts_utc = starts_at.astimezone(timezone.utc)
    booking = SchedulingBooking(
        id=uuid.uuid4(),
        user_id=settings.user_id,
        event_type_id=event_type.id,
        starts_at=starts_utc,
        ends_at=starts_utc + timedelta(minutes=event_type.duration_minutes),
        booker_name=name,
        booker_email=email,
        attendee_emails=attendee_emails,
        title=event_type.name,
        notes=notes,
        answers=questions.labelled(event_type.questions, cleaned),
        status=STATUS_PENDING,
        management_token=new_management_token(),
    )
    db.add(booking)

    # The exclusion constraint is the authority on whether this slot is free —
    # `is_bookable` above is a read, and two guests can pass it at once. A
    # savepoint keeps the session usable when Postgres rejects the loser.
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError as exc:
        raise SlotTaken("That time is no longer available") from exc

    await db.commit()
    await availability.invalidate_busy(settings.user_id)

    tz = timezone_for(settings.timezone)
    try:
        event = await run_in_threadpool(
            calendar.create_event,
            str(settings.user_id),
            title=booking.title,
            starts_at=booking.starts_at,
            ends_at=booking.ends_at,
            attendee_emails=_attendees(booking),
            description=notifications.event_description(booking, tz),
        )
    except Exception as exc:
        booking.status = STATUS_FAILED
        booking.failure_reason = str(exc)[:500]
        await db.commit()
        log.exception("scheduling.booking_calendar_failed", booking_id=str(booking.id))
        raise CalendarFailed("The meeting could not be added to the calendar") from exc

    booking.status = STATUS_CONFIRMED
    booking.calendar_event_id = str(event.get("id") or event.get("event_id") or "") or None
    booking.meeting_url = event.get("hangoutLink") or event.get("meeting_url")
    await db.commit()

    if settings.confirmation_email:
        await _notify(
            str(settings.user_id),
            booking.booker_email,
            f"Confirmed: {booking.title} with {_host_name(user)}",
            notifications.confirmation_body(booking, _host_name(user), tz),
        )
    return booking


async def cancel(
    db: AsyncSession,
    booking: SchedulingBooking,
    settings: SchedulingSettings,
    user: User,
    *,
    by: str,
    reason: str | None = None,
) -> SchedulingBooking:
    """Release a slot and withdraw the calendar event.

    Idempotent: cancelling an already-cancelled booking returns it unchanged
    rather than erroring, because the guest's link is in an email they may open
    twice and a second click should not look like a failure.
    """
    if booking.status == STATUS_CANCELLED:
        return booking

    if not booking.calendar_event_id:
        # Nothing to withdraw. Worth saying out loud: a confirmed booking with
        # no event id means the create path failed to record one, and the only
        # symptom is a meeting that stays in the host's calendar after the
        # guest cancels — which is precisely how a payload-parsing bug went
        # unnoticed here until someone checked their calendar by hand.
        log.warning(
            "scheduling.cancel_without_calendar_event",
            booking_id=str(booking.id),
            status=booking.status,
        )
    else:
        try:
            await run_in_threadpool(
                calendar.delete_event, str(booking.user_id), booking.calendar_event_id
            )
        except Exception:
            # The database is the system of record for whether the slot is
            # free. Refusing to cancel here would leave the guest with a
            # meeting they have said they cannot attend and no way out.
            log.exception("scheduling.cancel_calendar_failed", booking_id=str(booking.id))

    booking.status = STATUS_CANCELLED
    booking.cancelled_at = datetime.now(timezone.utc)
    booking.cancelled_by = by
    booking.cancel_reason = (reason or None) and reason[:500]
    await db.commit()
    await availability.invalidate_busy(booking.user_id)

    tz = timezone_for(settings.timezone)
    await _notify(
        str(settings.user_id),
        booking.booker_email,
        f"Cancelled: {booking.title}",
        notifications.cancellation_body(booking, _host_name(user), tz, by),
    )
    return booking


async def reschedule(
    db: AsyncSession,
    booking: SchedulingBooking,
    settings: SchedulingSettings,
    event_type: SchedulingEventType,
    user: User,
    *,
    starts_at: datetime,
) -> SchedulingBooking:
    """Move a booking, keeping its calendar event and management link.

    The row moves rather than a new one being created, so the link already in
    the guest's inbox keeps working and the event in everyone's calendar is
    updated rather than replaced by a cancellation plus a fresh invite.
    """
    if booking.status == STATUS_CANCELLED:
        raise SlotTaken("That booking was cancelled and cannot be moved")

    # Its own reservation must not block it: excluding the booking is what lets
    # a guest shift 10:00 to 10:15 when the meeting is longer than the shift.
    if not await availability.is_bookable(
        db, settings, event_type, starts_at, exclude_booking_id=booking.id
    ):
        raise SlotTaken("That time is no longer available")

    previous = booking.starts_at
    starts_utc = starts_at.astimezone(timezone.utc)
    booking.starts_at = starts_utc
    booking.ends_at = starts_utc + timedelta(minutes=event_type.duration_minutes)
    booking.rescheduled_at = datetime.now(timezone.utc)

    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError as exc:
        booking.starts_at = previous
        raise SlotTaken("That time is no longer available") from exc

    await db.commit()
    await availability.invalidate_busy(booking.user_id)

    tz = timezone_for(settings.timezone)
    if not booking.calendar_event_id:
        # See `cancel` — the guest is told the meeting moved while everyone's
        # calendar still shows the old time, so this must not pass in silence.
        log.warning(
            "scheduling.reschedule_without_calendar_event", booking_id=str(booking.id)
        )
    else:
        try:
            # Every field, not just the changed ones — `update_event` replaces
            # the event, so anything omitted here is deleted from it. Sending
            # only the new times previously wiped the attendee list and dropped
            # the guest from their own meeting.
            await run_in_threadpool(
                calendar.update_event,
                str(booking.user_id),
                booking.calendar_event_id,
                starts_at=booking.starts_at,
                ends_at=booking.ends_at,
                title=booking.title,
                description=notifications.event_description(booking, tz),
                attendee_emails=_attendees(booking),
            )
        except Exception:
            log.exception("scheduling.reschedule_calendar_failed", booking_id=str(booking.id))

    await _notify(
        str(settings.user_id),
        booking.booker_email,
        f"Moved: {booking.title}",
        notifications.reschedule_body(booking, _host_name(user), tz, previous),
    )
    return booking
