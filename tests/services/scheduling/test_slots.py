from datetime import date, datetime, timezone

import pytest

from services.scheduling.slots import available_slots, timezone_for, windows_for

MONDAY = date(2026, 8, 10)
YESTERDAY = datetime(2026, 8, 9, tzinfo=timezone.utc)


def slots(**overrides) -> list[str]:
    """Run the engine with workable defaults and return HH:MM strings."""
    kwargs = {
        "day": MONDAY,
        "timezone_name": "UTC",
        "windows": [{"start": "09:00", "end": "11:00"}],
        "duration_minutes": 30,
        "interval_minutes": 30,
        "minimum_notice_minutes": 0,
        "busy": [],
        "reserved": [],
        "now": YESTERDAY,
    }
    kwargs.update(overrides)
    return [slot.strftime("%H:%M") for slot in available_slots(**kwargs)]


def utc(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, 10, hour, minute, tzinfo=timezone.utc)


# --------------------------------------------------------------------------
# Slot generation
# --------------------------------------------------------------------------


def test_generates_interval_slots_inside_the_window():
    assert slots(
        timezone_name="Asia/Kolkata",
        windows=[{"start": "09:00", "end": "10:00"}],
        interval_minutes=15,
    ) == ["09:00", "09:15", "09:30"]


def test_a_slot_must_finish_inside_the_window():
    """10:45 would run to 11:15 and overhang an 11:00 close."""
    assert slots(interval_minutes=15)[-1] == "10:30"


def test_excludes_calendar_and_reserved_overlaps():
    assert slots(
        busy=[(utc(9, 20), utc(9, 40))],
        reserved=[(utc(10, 30), utc(11, 0))],
    ) == ["10:00"]


def test_respects_notice_in_the_hosts_timezone():
    assert slots(
        timezone_name="Asia/Kolkata",
        windows=[{"start": "09:00", "end": "12:00"}],
        minimum_notice_minutes=120,
        now=datetime(2026, 8, 10, 3, 45, tzinfo=timezone.utc),  # 09:15 IST
    ) == ["11:30"]


def test_overlapping_windows_do_not_double_list_a_time():
    """Two windows can offer 10:00. A guest should see one button, not two."""
    assert slots(
        windows=[{"start": "09:00", "end": "11:00"}, {"start": "10:00", "end": "11:00"}]
    ) == ["09:00", "09:30", "10:00", "10:30"]


def test_results_are_sorted_even_when_windows_are_not():
    assert slots(
        windows=[{"start": "15:00", "end": "16:00"}, {"start": "09:00", "end": "10:00"}]
    ) == ["09:00", "09:30", "15:00", "15:30"]


# --------------------------------------------------------------------------
# Buffers
# --------------------------------------------------------------------------


def test_buffer_before_blocks_a_slot_that_starts_too_soon_after_a_meeting():
    """A 10:00 slot needs 09:45-10:00 clear, and a 09:00-09:50 event isn't."""
    assert slots(
        windows=[{"start": "09:00", "end": "12:00"}],
        busy=[(utc(9, 0), utc(9, 50))],
        buffer_before_minutes=15,
    ) == ["10:30", "11:00", "11:30"]


def test_buffer_after_blocks_a_slot_that_ends_too_close_to_the_next_meeting():
    """09:30 would end at 10:00, inside the 15 minutes owed before the 10:10
    event. 11:00 is refused for the mirror reason — the 11:00 event's own
    trailing buffer runs to 11:15."""
    assert slots(
        windows=[{"start": "09:00", "end": "12:00"}],
        busy=[(utc(10, 10), utc(11, 0))],
        buffer_after_minutes=15,
    ) == ["09:00", "11:30"]


def test_buffers_apply_to_bookings_not_only_calendar_events():
    """A buffer protects against any adjacent commitment, however it was made."""
    assert slots(
        windows=[{"start": "09:00", "end": "12:00"}],
        reserved=[(utc(9, 0), utc(9, 30))],
        buffer_after_minutes=30,
    ) == ["10:00", "10:30", "11:00", "11:30"]


def test_zero_buffers_allow_back_to_back_meetings():
    assert slots(
        windows=[{"start": "09:00", "end": "11:00"}],
        reserved=[(utc(9, 0), utc(9, 30))],
    ) == ["09:30", "10:00", "10:30"]


# --------------------------------------------------------------------------
# Daily cap
# --------------------------------------------------------------------------


def test_daily_cap_closes_the_day_once_it_is_reached():
    assert slots(booked_today=3, max_bookings_per_day=3) == []


def test_below_the_cap_the_day_is_normal():
    assert slots(booked_today=2, max_bookings_per_day=3) != []


def test_no_cap_means_no_cap():
    assert slots(booked_today=99, max_bookings_per_day=None) != []


# --------------------------------------------------------------------------
# Window resolution
# --------------------------------------------------------------------------


def test_weekly_hours_apply_when_no_override_exists():
    weekly = [{"weekday": 0, "start": "09:00", "end": "17:00"}]
    assert windows_for(MONDAY, weekly, None) == [{"start": "09:00", "end": "17:00"}]


def test_other_weekdays_are_ignored():
    weekly = [{"weekday": 2, "start": "09:00", "end": "17:00"}]
    assert windows_for(MONDAY, weekly, None) == []


def test_an_override_replaces_the_weekly_pattern():
    weekly = [{"weekday": 0, "start": "09:00", "end": "17:00"}]
    override = [{"start": "13:00", "end": "15:00"}]
    assert windows_for(MONDAY, weekly, override) == [{"start": "13:00", "end": "15:00"}]


def test_an_empty_override_blocks_the_whole_day():
    """This is the distinction the API depends on: [] is not the same as None."""
    weekly = [{"weekday": 0, "start": "09:00", "end": "17:00"}]
    assert windows_for(MONDAY, weekly, []) == []
    assert slots(windows=windows_for(MONDAY, weekly, [])) == []


# --------------------------------------------------------------------------
# Time zones
# --------------------------------------------------------------------------


def test_slots_are_emitted_in_the_hosts_zone():
    result = available_slots(
        day=MONDAY,
        timezone_name="Asia/Kolkata",
        windows=[{"start": "09:00", "end": "09:30"}],
        duration_minutes=30,
        interval_minutes=30,
        minimum_notice_minutes=0,
        busy=[],
        reserved=[],
        now=YESTERDAY,
    )
    assert result[0].utcoffset().total_seconds() == 5.5 * 3600
    assert result[0].astimezone(timezone.utc).hour == 3  # 09:00 IST is 03:30 UTC


def test_busy_intervals_in_another_zone_still_block():
    """The host's calendar and the booking engine must agree on instants."""
    assert slots(
        timezone_name="Asia/Kolkata",
        windows=[{"start": "09:00", "end": "10:00"}],
        busy=[(utc(3, 30), utc(4, 0))],  # 09:00-09:30 IST
    ) == ["09:30"]


@pytest.mark.parametrize("name", ["Not/AZone", "../../etc/passwd", ""])
def test_bad_timezones_raise_one_predictable_error(name):
    """Routers catch ValueError to return 422; a bare ValueError from ZoneInfo
    for path-shaped input used to escape as a 500."""
    with pytest.raises(ValueError):
        timezone_for(name)


def test_spring_forward_never_emits_a_wall_time_that_does_not_exist():
    """On 2026-03-08 US/Eastern jumps 02:00 -> 03:00. Nothing may report 02:xx."""
    emitted = available_slots(
        day=date(2026, 3, 8),
        timezone_name="America/New_York",
        windows=[{"start": "01:00", "end": "05:00"}],
        duration_minutes=30,
        interval_minutes=30,
        minimum_notice_minutes=0,
        busy=[],
        reserved=[],
        now=datetime(2026, 3, 1, tzinfo=timezone.utc),
    )
    assert emitted, "the day should still be bookable"
    assert not [slot for slot in emitted if slot.hour == 2]
    # Every emitted slot round-trips through UTC unchanged — i.e. is a real instant.
    for slot in emitted:
        assert slot.astimezone(timezone.utc).astimezone(slot.tzinfo) == slot
