"""Google Calendar integration via Composio (blocking; call from workers)."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from core.config import settings
from integrations.composio.composio_client import get_composio

EVENTS_LIST = "GOOGLECALENDAR_EVENTS_LIST"
FIND_FREE_SLOTS = "GOOGLECALENDAR_FIND_FREE_SLOTS"
CREATE_EVENT = "GOOGLECALENDAR_CREATE_EVENT"
UPDATE_EVENT = "GOOGLECALENDAR_UPDATE_EVENT"
DELETE_EVENT = "GOOGLECALENDAR_DELETE_EVENT"


def _payload(resp: dict, action: str) -> dict:
    """The Google object out of a Composio envelope, whichever shape it used.

    Composio is not consistent here. Read actions put the result straight on
    `data`::

        EVENTS_LIST  -> {"data": {"items": [...]}}

    while the write actions wrap it once more::

        CREATE_EVENT -> {"data": {"response_data": {"id": ..., "hangoutLink": ...}}}

    Reading `data` directly therefore worked for reads and returned an empty
    dict for writes — so every booking stored a NULL `calendar_event_id`, and
    because cancel and reschedule are guarded on that id being present, they
    skipped the calendar without raising anything. A silent no-op, not an error.

    Unwrapping in one place, tolerant of both shapes, is what stops that from
    depending on which call site remembered.
    """
    if resp.get("successful") is False:
        raise RuntimeError(f"Composio {action} failed: {resp.get('error')}")
    data = resp.get("data") or {}
    inner = data.get("response_data")
    return inner if isinstance(inner, dict) else data


def is_connected(user_id: str) -> bool:
    res = get_composio().connected_accounts.list(
        user_ids=[user_id],
        toolkit_slugs=["googlecalendar"],
        statuses=["ACTIVE"],
    )
    return bool(getattr(res, "items", None))


def initiate_connection(user_id: str, callback_url: str | None = None) -> str:
    """Start the Google Calendar OAuth grant. Returns a redirect URL to send the user to."""
    if not settings.COMPOSIO_GCAL_AUTH_CONFIG_ID:
        raise RuntimeError("COMPOSIO_GCAL_AUTH_CONFIG_ID is not configured")

    request = get_composio().connected_accounts.link(
        user_id=user_id,
        auth_config_id=settings.COMPOSIO_GCAL_AUTH_CONFIG_ID,
        callback_url=callback_url or f"{settings.FRONTEND_BASE_URL}/onboarding/connect",
    )
    return request.redirect_url


def list_events(user_id: str, time_min: datetime, time_max: datetime) -> list[dict]:
    """Return calendar events between time_min and time_max (both tz-aware)."""

    resp = get_composio().tools.execute(
        EVENTS_LIST,
        {
            "timeMin": time_min.isoformat(),
            "timeMax": time_max.isoformat(),
            "singleEvents": True,
            "orderBy": "startTime",
        },
        user_id=user_id,
    )

    data = _payload(resp, EVENTS_LIST)
    return data.get("items") or data.get("events") or []


def _event_bounds(ev: dict) -> tuple[datetime, datetime] | None:
    # `.get("start")` can legitimately be absent — a cancelled instance in a
    # recurring series carries only a status — and chaining .get() off that None
    # used to raise AttributeError. Harmless while this was worker-only code;
    # not harmless now that an unauthenticated booking page walks the same list.
    start = (ev.get("start") or {}).get("dateTime")
    end = (ev.get("end") or {}).get("dateTime")

    if not start or not end:
        return None  # all-day events have no dateTime; ignore for overlap
    try:
        return (datetime.fromisoformat(start.replace("Z", "+00:00")), datetime.fromisoformat(end.replace("Z", "+00:00")))
    except ValueError:
        return None


def find_double_bookings(user_id: str, tz: str, days: int = 1) -> list[tuple[str, str]]:

    tzinfo = ZoneInfo(tz)
    now = datetime.now(tzinfo)
    end = now + timedelta(days=days)
    events = list_events(user_id, now, end)

    timed = []
    for ev in events:
        b = _event_bounds(ev)
        if b:
            timed.append((b[0], b[1], ev.get("summary") or "(untitled)"))
    timed.sort()

    clashes: list[tuple[str, str]] = []
    for i in range(len(timed)):
        s1, e1, n1 = timed[i]
        for j in range(i + 1, len(timed)):
            s2, e2, n2 = timed[j]
            if s2 >= e1:
                break
            if s2 < e1 and s1 < e2:
                clashes.append((n1, n2))
    return clashes


def _busy_periods(user_id: str, time_min: datetime, time_max: datetime, tz: str) -> list[tuple[datetime, datetime]]:
    resp = get_composio().tools.execute(
        FIND_FREE_SLOTS,
        {
            "time_min": time_min.isoformat(), 
            "time_max": time_max.isoformat(), 
            "timezone": tz
        },
        user_id=user_id,
    )

    cals = _payload(resp, FIND_FREE_SLOTS).get("calendars") or {}
    busy = (cals.get("primary") or {}).get("busy") or []
    out = []
    for b in busy:
        try:
            out.append(
                (
                    datetime.fromisoformat(b["start"].replace("Z", "+00:00")),
                    datetime.fromisoformat(b["end"].replace("Z", "+00:00")),
                )
            )
        except (KeyError, ValueError):
            continue
    return out


def free_slots(
    user_id: str,
    tz: str,
    *,
    days: int = 5,
    slot_min: int = 30,
    work_start: int = 9,
    work_end: int = 18,
    max_slots: int = 6,
) -> list[tuple[datetime, datetime]]:
    """Return up to `max_slots` free working-hour windows (>= slot_min) over the
    next `days` weekdays, computed from the calendar's busy periods."""
    tzinfo = ZoneInfo(tz)
    now = datetime.now(tzinfo)
    horizon = now + timedelta(days=days)
    busy = _busy_periods(user_id, now, horizon, tz)

    slots: list[tuple[datetime, datetime]] = []
    for d in range(days + 1):
        day = (now + timedelta(days=d)).date()
        if day.weekday() >= 5:  # skip Sat/Sun
            continue
        win_start = datetime(day.year, day.month, day.day, work_start, tzinfo=tzinfo)
        win_end = datetime(day.year, day.month, day.day, work_end, tzinfo=tzinfo)
        cursor = max(win_start, now)
        # walk busy periods intersecting this window, in order
        for b_start, b_end in sorted(busy):
            if b_end <= cursor or b_start >= win_end:
                continue
            if b_start - cursor >= timedelta(minutes=slot_min):
                slots.append((cursor, b_start))
            cursor = max(cursor, b_end)
        if win_end - cursor >= timedelta(minutes=slot_min):
            slots.append((cursor, win_end))
        if len(slots) >= max_slots:
            break
    return slots[:max_slots]


def format_slots(slots: list[tuple[datetime, datetime]]) -> str:
    return "\n".join(f"  • {s.strftime('%a %d %b, %-I:%M %p')} – {e.strftime('%-I:%M %p')}" for s, e in slots)


def busy_windows(user_id: str, time_min: datetime, time_max: datetime) -> list[tuple[datetime, datetime]]:
    """Timed events in the range, as (start, end) pairs. All-day events ignored.

    The public wrapper over `_event_bounds`. Callers outside this module used to
    reach for the underscore-prefixed helper and reimplement the filter around
    it; there is exactly one correct way to turn an event list into blocked
    intervals, so it lives here.
    """
    return [
        bounds
        for event in list_events(user_id, time_min, time_max)
        if (bounds := _event_bounds(event))
    ]


def create_event(
    user_id: str,
    *,
    title: str,
    starts_at: datetime,
    ends_at: datetime,
    attendee_emails: list[str],
    description: str | None = None,
    send_updates: str = "all",
) -> dict:
    """Create a Google Calendar event and request a Meet conference."""
    resp = get_composio().tools.execute(
        CREATE_EVENT,
        {
            "summary": title,
            "description": description or "",
            "start_datetime": starts_at.isoformat(),
            "end_datetime": ends_at.isoformat(),
            "attendees": attendee_emails,
            "create_meeting_room": True,
            "send_updates": send_updates,
        },
        user_id=user_id,
    )
    return _payload(resp, CREATE_EVENT)


def update_event(
    user_id: str,
    event_id: str,
    *,
    starts_at: datetime,
    ends_at: datetime,
    title: str | None = None,
    description: str | None = None,
    attendee_emails: list[str] | None = None,
) -> dict:
    """Move an existing event, notifying everyone on it.

    **This replaces the event, it does not patch it.** Any field left out of
    the payload is cleared rather than preserved, and the field that matters is
    `attendees`: omitting it removes every guest from the meeting. The host's
    calendar still shows a confirmed event at the new time, so nothing looks
    wrong from the side doing the rescheduling — while the guest, who has just
    been un-invited, sees the meeting vanish.

    Callers must therefore pass the *whole* intended state of the event, not
    only the parts they are changing.
    """
    payload: dict = {
        "event_id": event_id,
        "start_datetime": starts_at.isoformat(),
        "end_datetime": ends_at.isoformat(),
        "send_updates": "all",
    }
    if title is not None:
        payload["summary"] = title
    if description is not None:
        payload["description"] = description
    if attendee_emails is not None:
        payload["attendees"] = attendee_emails

    resp = get_composio().tools.execute(UPDATE_EVENT, payload, user_id=user_id)
    return _payload(resp, UPDATE_EVENT)


def delete_event(user_id: str, event_id: str) -> None:
    """Cancel an event, sending the cancellation to its attendees.

    A already-deleted event is success, not failure: the caller's goal is that
    the event not be on the calendar, and a retry after a partial cancellation
    must not fail the whole operation.
    """
    resp = get_composio().tools.execute(
        DELETE_EVENT,
        {"event_id": event_id, "send_updates": "all"},
        user_id=user_id,
    )
    if resp.get("successful") is False:
        error = str(resp.get("error") or "")
        if "410" in error or "404" in error or "not found" in error.lower():
            return
        raise RuntimeError(f"Composio {DELETE_EVENT} failed: {error}")
