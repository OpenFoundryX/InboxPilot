"""Mail a booking generates, and the text of it.

Google already emails a calendar invite when the event is created, so these are
not "you have a meeting" notices — that would be the second copy of a message
the guest already has. They exist for the one thing an invite cannot carry: the
management link. A guest who can't find their way back to cancel emails the
host instead, which is the failure this feature is meant to remove.

Bodies are built by pure functions so the wording is testable without a mail
transport, and sending is one function that never raises at its caller: a
booking that is confirmed in Postgres and on the calendar is not un-made by a
bounced courtesy email.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from core.config import settings as app_settings
from core.logging import get_logger
from integrations.google import gmail
from models.scheduling import SchedulingBooking

log = get_logger(__name__)


def manage_url(booking: SchedulingBooking) -> str:
    return f"{app_settings.FRONTEND_BASE_URL}/booking/{booking.management_token}"


def _when(moment: datetime, tz: ZoneInfo) -> str:
    local = moment.astimezone(tz)
    return local.strftime("%A %d %B %Y at %H:%M (%Z)")


def event_description(booking: SchedulingBooking, tz: ZoneInfo) -> str:
    """The calendar event body. Carries the manage link into Google's invite.

    Belt and braces with the confirmation email below: if the host has
    confirmation mail switched off, this is still how the guest keeps a way to
    cancel, so it is not conditional on any setting.
    """
    lines = [f"Booked by {booking.booker_name} ({booking.booker_email})."]
    if booking.notes:
        lines += ["", booking.notes]
    if booking.answers:
        lines += [""] + [f"{key}: {value}" for key, value in booking.answers.items()]
    lines += ["", f"Reschedule or cancel: {manage_url(booking)}"]
    return "\n".join(lines)


def confirmation_body(booking: SchedulingBooking, host_name: str, tz: ZoneInfo) -> str:
    lines = [
        f"Hi {booking.booker_name},",
        "",
        f"Your meeting with {host_name} is confirmed.",
        "",
        f"  {booking.title}",
        f"  {_when(booking.starts_at, tz)}",
    ]
    if booking.meeting_url:
        lines.append(f"  {booking.meeting_url}")
    lines += [
        "",
        f"Need a different time? Reschedule or cancel here: {manage_url(booking)}",
    ]
    return "\n".join(lines)


def cancellation_body(booking: SchedulingBooking, host_name: str, tz: ZoneInfo, by: str) -> str:
    who = "you" if by == "guest" else host_name
    lines = [
        f"Hi {booking.booker_name},",
        "",
        f"The meeting below was cancelled by {who}.",
        "",
        f"  {booking.title}",
        f"  {_when(booking.starts_at, tz)}",
    ]
    if booking.cancel_reason:
        lines += ["", f"Reason: {booking.cancel_reason}"]
    return "\n".join(lines)


def reschedule_body(
    booking: SchedulingBooking, host_name: str, tz: ZoneInfo, previous: datetime
) -> str:
    return "\n".join(
        [
            f"Hi {booking.booker_name},",
            "",
            f"Your meeting with {host_name} has moved.",
            "",
            f"  Was: {_when(previous, tz)}",
            f"  Now: {_when(booking.starts_at, tz)}",
            "",
            f"Manage this booking: {manage_url(booking)}",
        ]
    )


def reminder_body(booking: SchedulingBooking, host_name: str, tz: ZoneInfo) -> str:
    lines = [
        f"Hi {booking.booker_name},",
        "",
        f"A reminder about your meeting with {host_name}:",
        "",
        f"  {booking.title}",
        f"  {_when(booking.starts_at, tz)}",
    ]
    if booking.meeting_url:
        lines.append(f"  {booking.meeting_url}")
    lines += ["", f"Can't make it? Cancel or reschedule: {manage_url(booking)}"]
    return "\n".join(lines)


def send(host_user_id: str, to: str, subject: str, body: str) -> bool:
    """Best-effort send. Logs and returns False rather than raising.

    Every caller is downstream of a state change that has already been
    committed, so there is nothing useful to do with an exception here except
    lose the booking that caused it.
    """
    try:
        gmail.send_email(host_user_id, to=to, subject=subject, body=body)
        return True
    except Exception:
        log.exception("scheduling.email_failed", host_user_id=host_user_id, to=to)
        return False
