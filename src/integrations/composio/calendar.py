"""Google Calendar integration via Composio (blocking; call from workers)."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from core.config import settings
from integrations.composio.composio_client import get_composio

EVENTS_LIST = "GOOGLECALENDAR_EVENTS_LIST"
FIND_FREE_SLOTS = "GOOGLECALENDAR_FIND_FREE_SLOTS"


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

    if resp.get("successful") is False:
        raise RuntimeError(f"Composio {EVENTS_LIST} failed: {resp.get('error')}")

    data = resp.get("data") or {}
    return data.get("items") or data.get("events") or []


def _event_bounds(ev: dict) -> tuple[datetime, datetime] | None:
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

    if resp.get("successful") is False:
        raise RuntimeError(f"Composio {FIND_FREE_SLOTS} failed: {resp.get('error')}")
    cals = ((resp.get("data") or {}).get("calendars") or {})
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
