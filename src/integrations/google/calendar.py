"""Google Calendar, called directly (blocking; call from workers).

Same signatures as the Composio module it replaces. Two behaviours differ
deliberately, both documented at their call sites below: updates PATCH rather
than replace, and a freeBusy query that Google could not answer now raises
instead of silently reading as "free".
"""

import uuid
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from core.logging import get_logger
from integrations.google.client import (
    CALENDAR_BASE,
    GoogleNotFound,
    google_request,
)
from integrations.google.credentials import get_connection
from models.google import CALENDAR_REQUIRED_SCOPES

log = get_logger(__name__)

CALENDAR_ID = "primary"
EVENTS_PATH = f"/calendars/{CALENDAR_ID}/events"

# How many pages of events to walk before giving up. events.list caps at 250
# per page, so this is 2,500 events in a window — far past anything the callers
# here ask for, and a bound rather than an unbounded loop.
MAX_EVENT_PAGES = 10


def _request(user_id: str, method: str, path: str, **kwargs: Any) -> Any:
    return google_request(
        user_id,
        method,
        f"{CALENDAR_BASE}{path}",
        required_scopes=CALENDAR_REQUIRED_SCOPES,
        **kwargs,
    )


def _require_aware(value: datetime, field: str) -> str:
    """RFC 3339 with an offset, which Calendar requires.

    A naive datetime serialises without a zone and Google answers 400. Composio
    coerced these; catching it at the boundary keeps the error next to the
    caller that produced it.
    """
    if value.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.isoformat()


def is_connected(user_id: str) -> bool:
    state = get_connection(user_id)
    return bool(state and not state.revoked and CALENDAR_REQUIRED_SCOPES <= state.scopes)


def initiate_connection(user_id: str, callback_url: str | None = None) -> str:
    """Kept for signature compatibility; the grant is started from the API route."""
    raise NotImplementedError(
        "start the Google grant via GET /v1/integrations/google/connect"
    )


def list_events(user_id: str, time_min: datetime, time_max: datetime) -> list[dict]:
    """Calendar events between time_min and time_max (both tz-aware)."""
    events: list[dict] = []
    token: str | None = None

    for _ in range(MAX_EVENT_PAGES):
        params: dict[str, Any] = {
            "timeMin": _require_aware(time_min, "time_min"),
            "timeMax": _require_aware(time_max, "time_max"),
            "singleEvents": True,
            "orderBy": "startTime",
            "maxResults": 250,
        }
        if token:
            params["pageToken"] = token

        page = _request(user_id, "GET", EVENTS_PATH, params=params)
        events.extend(page.get("items") or [])
        token = page.get("nextPageToken")
        if not token:
            break

    return events


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
        return (
            datetime.fromisoformat(start.replace("Z", "+00:00")),
            datetime.fromisoformat(end.replace("Z", "+00:00")),
        )
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


def _busy_periods(
    user_id: str, time_min: datetime, time_max: datetime, tz: str
) -> list[tuple[datetime, datetime]]:
    """Busy intervals from Google's freeBusy endpoint.

    An `errors` entry on the calendar is treated as fatal, and that is the
    important part. Google reports a calendar it could not read by returning an
    error alongside an *empty* busy list — which every caller here would
    otherwise read as "entirely free", and hand out slots on top of real
    meetings. A booking page that errors is much better than one that
    double-books.
    """
    response = _request(
        user_id,
        "POST",
        "/freeBusy",
        json={
            "timeMin": _require_aware(time_min, "time_min"),
            "timeMax": _require_aware(time_max, "time_max"),
            "timeZone": tz,
            "items": [{"id": CALENDAR_ID}],
        },
    )

    calendar = (response.get("calendars") or {}).get(CALENDAR_ID) or {}
    if errors := calendar.get("errors"):
        raise RuntimeError(f"freeBusy could not read the calendar: {errors}")

    out: list[tuple[datetime, datetime]] = []
    for period in calendar.get("busy") or []:
        try:
            out.append(
                (
                    datetime.fromisoformat(period["start"].replace("Z", "+00:00")),
                    datetime.fromisoformat(period["end"].replace("Z", "+00:00")),
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
    return "\n".join(
        f"  • {s.strftime('%a %d %b, %-I:%M %p')} – {e.strftime('%-I:%M %p')}" for s, e in slots
    )


def busy_windows(
    user_id: str, time_min: datetime, time_max: datetime
) -> list[tuple[datetime, datetime]]:
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
    """Create a Google Calendar event with a Meet conference."""
    body = {
        "summary": title,
        "description": description or "",
        "start": {"dateTime": _require_aware(starts_at, "starts_at")},
        "end": {"dateTime": _require_aware(ends_at, "ends_at")},
        "attendees": [{"email": email} for email in attendee_emails],
        "conferenceData": {
            "createRequest": {
                "requestId": uuid.uuid4().hex,
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        },
    }

    event = _request(
        user_id,
        "POST",
        EVENTS_PATH,
        # conferenceDataVersion=1 is mandatory. Without it Google accepts the
        # request, ignores conferenceData entirely, and returns an event with no
        # Meet link — no error anywhere.
        params={"conferenceDataVersion": 1, "sendUpdates": send_updates},
        json=body,
        idempotent=False,
    )

    return _with_conference_link(user_id, event)


def _with_conference_link(user_id: str, event: dict) -> dict:
    """Re-read the event once if its Meet link has not materialised yet.

    Conference creation is asynchronous: the insert response can come back with
    `createRequest.status.statusCode == "pending"` and no `hangoutLink`. Callers
    persist that field straight away, so a booking created in that window would
    store a null meeting URL and never learn otherwise.
    """
    if event.get("hangoutLink") or not event.get("id"):
        return event

    status = (
        ((event.get("conferenceData") or {}).get("createRequest") or {}).get("status") or {}
    ).get("statusCode")
    if status and status != "pending":
        return event

    try:
        refetched = _request(user_id, "GET", f"{EVENTS_PATH}/{event['id']}")
    except Exception:
        log.warning("calendar.conference_refetch_failed", event_id=event.get("id"))
        return event

    if refetched.get("hangoutLink"):
        return refetched

    log.warning("calendar.no_meet_link", event_id=event.get("id"), status=status)
    return event


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

    **This patches the event, it does not replace it.** Fields left out keep
    their current values, so a caller changing only the time does not have to
    restate the rest — and, importantly, the Meet conference survives, which a
    full replace would drop.

    The one field that does not merge is `attendees`: supplying it replaces the
    whole guest list rather than adding to it, so pass either the complete
    intended list or nothing at all.
    """
    body: dict[str, Any] = {
        "start": {"dateTime": _require_aware(starts_at, "starts_at")},
        "end": {"dateTime": _require_aware(ends_at, "ends_at")},
    }
    if title is not None:
        body["summary"] = title
    if description is not None:
        body["description"] = description
    if attendee_emails is not None:
        body["attendees"] = [{"email": email} for email in attendee_emails]

    return _request(
        user_id,
        "PATCH",
        f"{EVENTS_PATH}/{event_id}",
        params={"conferenceDataVersion": 1, "sendUpdates": "all"},
        json=body,
        idempotent=False,
    )


def delete_event(user_id: str, event_id: str) -> None:
    """Cancel an event, sending the cancellation to its attendees.

    An already-deleted event is success, not failure: the caller's goal is that
    the event not be on the calendar, and a retry after a partial cancellation
    must not fail the whole operation.
    """
    try:
        _request(
            user_id,
            "DELETE",
            f"{EVENTS_PATH}/{event_id}",
            params={"sendUpdates": "all"},
            expect_json=False,
        )
    except GoogleNotFound:
        # 404 (never existed) and 410 (already cancelled) both land here.
        return
