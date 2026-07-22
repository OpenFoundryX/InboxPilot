"""Google Calendar integration via Composio (blocking; call from workers)."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from integrations.composio.composio_client import get_composio

EVENTS_LIST = "GOOGLECALENDAR_EVENTS_LIST"
FIND_FREE_SLOTS = "GOOGLECALENDAR_FIND_FREE_SLOTS"


def is_connected(user_id: str) -> bool:
    res = get_composio().connected_accounts.list(
        user_ids=[user_id], toolkit_slugs=["googlecalendar"], statuses=["ACTIVE"]
    )
    return bool(getattr(res, "items", None))


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
    """Return pairs of overlapping event summaries in the next `days` days."""
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
                break  # sorted by start; no more overlaps with i
            if s2 < e1 and s1 < e2:
                clashes.append((n1, n2))
    return clashes
