"""Turns a profile plus an event type into the times a guest can actually pick.

This is the seam between the pure arithmetic in `slots` and the two slow,
failure-prone things it needs: Google Calendar and Postgres. It exists so the
routers never orchestrate that themselves, and so the two callers that must
agree — the availability list a guest reads and the re-check that runs when
they submit — agree *by construction* rather than by both being written
carefully.

The other job here is not calling Google more than necessary. A month view is
one round trip for the whole month, cached briefly, rather than one per date
the guest clicks.
"""

import json
import uuid
from datetime import date, datetime, time, timedelta, timezone

from fastapi.concurrency import run_in_threadpool
from sqlalchemy.ext.asyncio import AsyncSession

from core.logging import get_logger
from core.redis import redis_client
from integrations.google import calendar
from models.scheduling import SchedulingEventType, SchedulingSettings
from services.scheduling import store
from services.scheduling.slots import Interval, available_slots, timezone_for, windows_for

log = get_logger(__name__)

#: Busy windows change when the host edits their calendar, which we don't get
#: told about. A minute is short enough that a host who blocks time sees it take
#: effect while they watch, and long enough to collapse the burst of requests a
#: single guest makes clicking through a month.
BUSY_CACHE_SECONDS = 60


class CalendarUnavailable(RuntimeError):
    """The host's calendar could not be read, so no answer is safe to give.

    Deliberately not "assume free". Publishing slots we could not verify is how
    a host ends up double-booked over something already in their calendar.
    """


def _cache_key(user_id: uuid.UUID, day: date) -> str:
    return f"sched:busy:{user_id}:{day.isoformat()}"


def _days(first: date, last: date) -> list[date]:
    return [first + timedelta(days=n) for n in range((last - first).days + 1)]


def _decode(raw: str) -> list[Interval] | None:
    try:
        return [
            (datetime.fromisoformat(start), datetime.fromisoformat(end))
            for start, end in json.loads(raw)
        ]
    except (ValueError, TypeError):
        return None


async def _cached_days(
    user_id: uuid.UUID, days: list[date]
) -> dict[date, list[Interval]]:
    """Whichever of these days is already cached.

    Cached per day rather than per requested range. Keying on the range meant
    the month the calendar UI asks for and the single day the booking re-check
    asks for were different keys, so the check that runs on every book and
    reschedule always missed and always paid for another Google round trip —
    immediately after the page had just fetched the very same information.
    """
    try:
        raw = await redis_client.mget([_cache_key(user_id, d) for d in days])
    except Exception:
        return {}
    found: dict[date, list[Interval]] = {}
    for day, value in zip(days, raw):
        if value is None:
            continue
        decoded = _decode(value)
        if decoded is not None:
            found[day] = decoded
    return found


async def _store_days(user_id: uuid.UUID, by_day: dict[date, list[Interval]]) -> None:
    """Store each day, including the empty ones — free days are worth caching."""
    try:
        pipe = redis_client.pipeline()
        for day, intervals in by_day.items():
            pipe.set(
                _cache_key(user_id, day),
                json.dumps([[s.isoformat(), e.isoformat()] for s, e in intervals]),
                ex=BUSY_CACHE_SECONDS,
            )
        await pipe.execute()
    except Exception:
        log.warning("scheduling.busy_cache_write_failed", user_id=str(user_id))


async def invalidate_busy(user_id: uuid.UUID) -> None:
    """Drop every cached day for a host after their bookings change.

    Collected then deleted in one call. Deleting inside the scan loop was one
    Redis round trip per key, which was tolerable when a host had a handful of
    range-shaped entries and is not now that the cache holds a key per day —
    a warmed month turned every booking into a burst of sequential deletes.
    """
    try:
        keys = [key async for key in redis_client.scan_iter(match=f"sched:busy:{user_id}:*")]
        if keys:
            await redis_client.delete(*keys)
    except Exception:
        log.warning("scheduling.busy_cache_purge_failed", user_id=str(user_id))


async def busy_between(user_id: uuid.UUID, first: date, last: date, tz) -> list[Interval]:
    """The host's blocked calendar intervals across a date range, cached per day.

    Only the days not already cached are fetched, and they are fetched in one
    call spanning them rather than one call each — so a month view costs a
    single round trip, and the single-day re-check that follows it costs none.
    """
    days = _days(first, last)
    cached = await _cached_days(user_id, days)
    missing = [day for day in days if day not in cached]

    if missing:
        window_start = datetime.combine(min(missing), time.min, tz)
        window_end = datetime.combine(max(missing), time.min, tz) + timedelta(days=1)
        try:
            fetched = await run_in_threadpool(
                calendar.busy_windows, str(user_id), window_start, window_end
            )
        except Exception as exc:
            log.exception("scheduling.calendar_unavailable", user_id=str(user_id))
            raise CalendarUnavailable(str(exc)) from exc

        # An event is filed under every day it touches, so a meeting running
        # past midnight still blocks the morning it runs into.
        by_day: dict[date, list[Interval]] = {day: [] for day in missing}
        for start, end in fetched:
            for day in missing:
                day_start = datetime.combine(day, time.min, tz)
                if start < day_start + timedelta(days=1) and end > day_start:
                    by_day[day].append((start, end))
        await _store_days(user_id, by_day)
        cached.update(by_day)

    merged: dict[tuple[datetime, datetime], None] = {}
    for day in days:
        for interval in cached.get(day, []):
            merged[interval] = None
    return list(merged)


def bookable_range(event_type: SchedulingEventType, tz) -> tuple[date, date]:
    """The window a guest may book inside, in the host's zone."""
    today = datetime.now(tz).date()
    return today, today + timedelta(days=event_type.booking_horizon_days)


async def slots_between(
    db: AsyncSession,
    settings: SchedulingSettings,
    event_type: SchedulingEventType,
    first: date,
    last: date,
    *,
    exclude_booking_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> dict[date, list[datetime]]:
    """Bookable start times for every date in `[first, last]`.

    One calendar read, one reservations read, one overrides read — then the
    pure slot pass per day. Dates outside the event type's booking window are
    returned as empty rather than omitted, so the calendar UI can render them
    greyed instead of guessing.
    """
    tz = timezone_for(settings.timezone)
    earliest, latest = bookable_range(event_type, tz)
    first = max(first, earliest)
    last = min(last, latest)
    if first > last:
        return {}

    busy = await busy_between(settings.user_id, first, last, tz)
    overrides = await store.overrides_between(db, settings.user_id, first, last)

    range_start = datetime.combine(first, time.min, tz)
    range_end = datetime.combine(last, time.min, tz) + timedelta(days=1)
    reserved = await store.live_bookings_between(
        db, settings.user_id, range_start, range_end, exclude_id=exclude_booking_id
    )

    result: dict[date, list[datetime]] = {}
    day = first
    while day <= last:
        day_start = datetime.combine(day, time.min, tz)
        day_end = day_start + timedelta(days=1)
        booked_today = 0
        if event_type.max_bookings_per_day is not None:
            booked_today = await store.count_bookings_on(
                db,
                settings.user_id,
                event_type.id,
                day_start.astimezone(timezone.utc),
                day_end.astimezone(timezone.utc),
                exclude_id=exclude_booking_id,
            )
        result[day] = available_slots(
            day=day,
            timezone_name=settings.timezone,
            windows=windows_for(day, settings.weekly_hours, overrides.get(day)),
            duration_minutes=event_type.duration_minutes,
            interval_minutes=event_type.slot_interval_minutes,
            minimum_notice_minutes=event_type.minimum_notice_minutes,
            busy=busy,
            reserved=reserved,
            buffer_before_minutes=event_type.buffer_before_minutes,
            buffer_after_minutes=event_type.buffer_after_minutes,
            booked_today=booked_today,
            max_bookings_per_day=event_type.max_bookings_per_day,
            now=now,
        )
        day += timedelta(days=1)
    return result


async def slots_on(
    db: AsyncSession,
    settings: SchedulingSettings,
    event_type: SchedulingEventType,
    day: date,
    *,
    exclude_booking_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> list[datetime]:
    """Bookable start times on one date. The booking re-check uses this."""
    days = await slots_between(
        db, settings, event_type, day, day, exclude_booking_id=exclude_booking_id, now=now
    )
    return days.get(day, [])


async def is_bookable(
    db: AsyncSession,
    settings: SchedulingSettings,
    event_type: SchedulingEventType,
    starts_at: datetime,
    *,
    exclude_booking_id: uuid.UUID | None = None,
) -> bool:
    """Whether an exact instant is still on offer.

    Compared as instants, not as local datetimes: the guest's payload carries
    their own offset and the generated slots carry the host's, and the same
    moment written two ways must match.
    """
    tz = timezone_for(settings.timezone)
    host_day = starts_at.astimezone(tz).date()
    offered = await slots_on(
        db, settings, event_type, host_day, exclude_booking_id=exclude_booking_id
    )
    wanted = starts_at.astimezone(timezone.utc)
    return any(slot.astimezone(timezone.utc) == wanted for slot in offered)
