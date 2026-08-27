"""Delivery-slot scheduling logic (pure functions, timezone-aware).

Given a user's settings and the current instant, decide whether *now* is a
delivery slot and whether we're inside the Do-Not-Disturb window. Called every
minute by the `mailman.tick` beat task, so slot matching is minute-granular.
"""

from datetime import datetime, timedelta

from models.mailman import (
    MODE_CUSTOM_DAILY,
    MODE_INTERVAL,
    MODE_TIMES,
    MailmanSettings,
)


def _hm(value: str) -> int:
    """ "HH:MM" -> minutes since midnight."""
    h, m = value.split(":")
    return int(h) * 60 + int(m)


def _minutes_of_day(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute


def slot_minutes(s: MailmanSettings) -> set[int]:
    """The clock-time delivery slots (minutes-since-midnight) for times/custom modes."""
    if s.delivery_mode == MODE_CUSTOM_DAILY:
        return {_hm(t) for t in (s.custom_times or [])}

    if s.delivery_mode == MODE_TIMES:
        n = s.times_per_day or 0
        if n <= 0:
            return set()
        start = _hm(s.active_window_start)
        end = _hm(s.active_window_end)
        span = end - start
        if n == 1 or span <= 0:
            return {start}
        step = span // n
        return {start + i * step for i in range(n)}

    return set()


def in_dnd(s: MailmanSettings, now_local: datetime) -> bool:
    """True if now_local falls inside the DND window (handles midnight wrap)."""
    if not s.dnd_enabled or not s.dnd_start or not s.dnd_end:
        return False
    now = _minutes_of_day(now_local)
    start = _hm(s.dnd_start)
    end = _hm(s.dnd_end)
    if start == end:
        return False
    if start < end:
        return start <= now < end
    # window wraps past midnight, e.g. 17:30 -> 07:30
    return now >= start or now < end


def is_delivery_due(s: MailmanSettings, now_local: datetime, now_utc: datetime) -> bool:
    """Whether a batch should be released right now.

    - interval: elapsed since the last delivery >= interval_hours.
    - times / custom_daily: the current minute matches a configured slot.
    DND suppresses all of the above.
    """
    if in_dnd(s, now_local):
        return False

    if s.delivery_mode == MODE_INTERVAL:
        # interval_minutes wins when set; otherwise fall back to interval_hours.
        minutes = s.interval_minutes or (s.interval_hours or 0) * 60
        if minutes <= 0:
            return False
        if s.last_delivery_at is None:
            return True
        return now_utc - s.last_delivery_at >= timedelta(minutes=minutes)

    return _minutes_of_day(now_local) in slot_minutes(s)
