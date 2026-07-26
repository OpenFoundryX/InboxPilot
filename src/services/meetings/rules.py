"""Should the notetaker attend this event?

Sending a bot into a call is visible to everyone in it, so the answer needs to be
predictable and explainable. Each check returns a reason string rather than a
bare False — the sweep logs it, which is what makes "why didn't it join?"
answerable after the fact.
"""

from datetime import datetime, timedelta, timezone

from models.meetings import MeetingSettings
from services.meetings.links import link_from_event


def event_bounds(event: dict) -> tuple[datetime, datetime] | None:
    """Start/end as tz-aware datetimes, or None for all-day events."""
    start = (event.get("start") or {}).get("dateTime")
    end = (event.get("end") or {}).get("dateTime")
    if not start or not end:
        return None
    try:
        return (
            datetime.fromisoformat(start.replace("Z", "+00:00")),
            datetime.fromisoformat(end.replace("Z", "+00:00")),
        )
    except ValueError:
        return None


def attendee_emails(event: dict) -> list[str]:
    """Attendees who haven't declined, excluding other meeting bots."""
    out = []
    for a in event.get("attendees") or []:
        if a.get("responseStatus") == "declined" or a.get("resource"):
            continue
        email = a.get("email")
        if email:
            out.append(email)
    return out


def skip_reason(
    event: dict, settings: MeetingSettings, *, now: datetime | None = None
) -> str | None:
    """Why the bot should stay away, or None if it should attend."""
    now = now or datetime.now(timezone.utc)

    if not settings.enabled:
        return "notetaker disabled"
    if not settings.auto_join:
        return "auto-join off"
    if event.get("status") == "cancelled":
        return "event cancelled"

    bounds = event_bounds(event)
    if not bounds:
        return "all-day or untimed event"
    start, _end = bounds

    if start < now - timedelta(minutes=5):
        return "already started"
    if start > now + timedelta(minutes=settings.lookahead_minutes):
        return "outside lookahead window"

    if not link_from_event(event):
        return "no meeting link"

    # A solo hold with a link is a personal block, not a meeting to record.
    if len(attendee_emails(event)) < settings.min_attendees:
        return f"fewer than {settings.min_attendees} attendees"

    title = (event.get("summary") or "").casefold()
    for needle in settings.skip_titles or []:
        if needle and str(needle).casefold() in title:
            return f"title matches skip rule {needle!r}"

    return None
