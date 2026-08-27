"""Pure availability arithmetic.

Nothing here touches the database or the calendar API. Everything a slot
depends on arrives as an argument, which is what makes the rules — buffers,
notice, caps, override precedence — testable without a Postgres or a Google
round trip, and reusable by the drafting agent when it proposes times in a
reply rather than serving a booking page.
"""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_WEEKLY_HOURS = [{"weekday": day, "start": "09:00", "end": "18:00"} for day in range(5)]

#: An interval the host is not free in, as (start, end), both tz-aware.
Interval = tuple[datetime, datetime]


def timezone_for(name: str) -> ZoneInfo:
    """Resolve an IANA name, turning every rejection into one ValueError.

    `ZoneInfo` raises `ZoneInfoNotFoundError` for names it doesn't know but a
    bare `ValueError` for structurally invalid keys (absolute paths, `..`
    segments) and `TypeError` for non-strings. Callers want one thing to catch,
    and a 422 rather than a 500 for all three.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ValueError(f"Unknown IANA time zone: {name!r}") from exc


def windows_for(
    day: date,
    weekly_hours: list[dict],
    override: list[dict] | None = None,
) -> list[dict]:
    """The `{start, end}` windows that apply to one date.

    An override replaces the weekly pattern outright rather than merging with
    it — that is what makes `[]` mean "day off" and lets a host block a public
    holiday without editing their recurring hours. `None` means no override
    exists for the date, which is different from an override that exists and is
    empty; conflating the two is the whole reason this takes `None` at all.
    """
    if override is not None:
        return [{"start": w["start"], "end": w["end"]} for w in override]
    return [
        {"start": w["start"], "end": w["end"]}
        for w in weekly_hours
        if w["weekday"] == day.weekday()
    ]


def _wall(day: date, clock: str, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, time.fromisoformat(clock), tz)


def _real_instant(moment: datetime, tz: ZoneInfo) -> datetime:
    """Snap a wall-clock time onto an instant that actually exists.

    On a spring-forward date, 02:30 is not a time — `ZoneInfo` still hands back
    a datetime for it (with `fold=0`, so the pre-transition offset), and that
    value round-trips to a *different* wall clock than the one written. Passing
    it through UTC resolves it to the instant Postgres and Google will agree
    on, so we never publish a slot at a time that did not happen.
    """
    return moment.astimezone(timezone.utc).astimezone(tz)


def available_slots(
    *,
    day: date,
    timezone_name: str,
    windows: list[dict],
    duration_minutes: int,
    interval_minutes: int,
    minimum_notice_minutes: int,
    busy: list[Interval],
    reserved: list[Interval],
    buffer_before_minutes: int = 0,
    buffer_after_minutes: int = 0,
    booked_today: int = 0,
    max_bookings_per_day: int | None = None,
    now: datetime | None = None,
) -> list[datetime]:
    """Bookable start times on `day`, in the host's zone, ascending.

    `windows` is already resolved for this date — see `windows_for`. `busy` and
    `reserved` are the host's calendar events and their existing bookings; both
    block a slot, because a buffer is protection against *any* adjacent
    commitment, not just ones made through this product.

    Buffers pad **both sides of the comparison**. A buffer is a property of a
    meeting — "leave me 15 minutes after anything I attend" — so it belongs to
    the existing commitment exactly as much as to the one being booked. Padding
    only the candidate gets the obvious case right (don't start a meeting too
    soon before an existing one) and the mirror case wrong: a 10:00-11:00 event
    with a 15-minute trailing buffer would still offer an 11:00 start, which is
    precisely the back-to-back the host asked not to have.
    """
    if max_bookings_per_day is not None and booked_today >= max_bookings_per_day:
        return []

    tz = timezone_for(timezone_name)
    now = (now or datetime.now(timezone.utc)).astimezone(tz)
    earliest = now + timedelta(minutes=minimum_notice_minutes)
    duration = timedelta(minutes=duration_minutes)
    interval = timedelta(minutes=interval_minutes)
    pad_before = timedelta(minutes=buffer_before_minutes)
    pad_after = timedelta(minutes=buffer_after_minutes)

    occupied = [(start.astimezone(tz), end.astimezone(tz)) for start, end in [*busy, *reserved]]

    found: set[datetime] = set()
    for window in windows:
        cursor = _wall(day, window["start"], tz)
        window_end = _wall(day, window["end"], tz)
        while cursor + duration <= window_end:
            start, end = cursor, cursor + duration
            cursor += interval
            if start < earliest:
                continue
            if any(
                start - pad_before < busy_end + pad_after
                and end + pad_after > busy_start - pad_before
                for busy_start, busy_end in occupied
            ):
                continue
            found.add(_real_instant(start, tz))

    # Windows may overlap or repeat a weekday, and two of them can offer the
    # same start. Guests should see one button per time, in order.
    return sorted(found)
