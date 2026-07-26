"""Join rules — every reason the notetaker stays out of a call, in isolation."""

from datetime import datetime, timedelta, timezone

from models.meetings import MeetingSettings
from services.meetings.rules import attendee_emails, event_bounds, skip_reason

NOW = datetime(2026, 7, 26, 10, 0, tzinfo=timezone.utc)


def settings(**overrides) -> MeetingSettings:
    """A settings row with defaults applied — SQLAlchemy only applies them on flush."""
    values = {
        "enabled": True,
        "auto_join": True,
        "bot_name": "InboxPilot Notetaker",
        "min_attendees": 2,
        "skip_titles": [],
        "lookahead_minutes": 30,
        "email_recap": True,
        "create_reminders": True,
        "include_in_digest": True,
    }
    values.update(overrides)
    return MeetingSettings(**values)


def event(*, start_offset_min=10, duration_min=30, attendees=2, **overrides) -> dict:
    start = NOW + timedelta(minutes=start_offset_min)
    ev = {
        "id": "evt-1",
        "summary": "Design review",
        "hangoutLink": "https://meet.google.com/abc-defg-hij",
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": (start + timedelta(minutes=duration_min)).isoformat()},
        "attendees": [{"email": f"person{i}@acme.com"} for i in range(attendees)],
    }
    ev.update(overrides)
    return ev


def test_joins_a_normal_upcoming_meeting():
    assert skip_reason(event(), settings(), now=NOW) is None


def test_skips_when_disabled():
    assert skip_reason(event(), settings(enabled=False), now=NOW) == "notetaker disabled"


def test_skips_when_auto_join_off():
    assert skip_reason(event(), settings(auto_join=False), now=NOW) == "auto-join off"


def test_skips_cancelled_event():
    assert skip_reason(event(status="cancelled"), settings(), now=NOW) == "event cancelled"


def test_skips_all_day_event():
    ev = event()
    ev["start"] = {"date": "2026-07-26"}
    ev["end"] = {"date": "2026-07-27"}
    assert skip_reason(ev, settings(), now=NOW) == "all-day or untimed event"


def test_skips_meeting_that_already_started():
    assert skip_reason(event(start_offset_min=-30), settings(), now=NOW) == "already started"


def test_tolerates_a_meeting_that_just_started():
    """A sweep landing seconds late shouldn't abandon the meeting."""
    assert skip_reason(event(start_offset_min=-2), settings(), now=NOW) is None


def test_skips_meeting_beyond_lookahead():
    assert (
        skip_reason(event(start_offset_min=90), settings(), now=NOW)
        == "outside lookahead window"
    )


def test_skips_event_without_link():
    ev = event()
    del ev["hangoutLink"]
    assert skip_reason(ev, settings(), now=NOW) == "no meeting link"


def test_skips_solo_hold():
    assert skip_reason(event(attendees=1), settings(), now=NOW) == "fewer than 2 attendees"


def test_skips_title_on_denylist():
    reason = skip_reason(event(summary="1:1 with Sam"), settings(skip_titles=["1:1"]), now=NOW)
    assert reason == "title matches skip rule '1:1'"


def test_title_denylist_is_case_insensitive():
    assert skip_reason(event(summary="Weekly STANDUP"), settings(skip_titles=["standup"]), now=NOW)


def test_declined_attendees_dont_count():
    ev = event(attendees=1)
    ev["attendees"].append({"email": "nope@acme.com", "responseStatus": "declined"})
    assert skip_reason(ev, settings(), now=NOW) == "fewer than 2 attendees"


def test_rooms_dont_count_as_attendees():
    ev = event(attendees=1)
    ev["attendees"].append({"email": "room-4@acme.com", "resource": True})
    assert attendee_emails(ev) == ["person0@acme.com"]


def test_event_bounds_returns_none_for_untimed():
    assert event_bounds({"start": {"date": "2026-07-26"}, "end": {"date": "2026-07-27"}}) is None
    assert event_bounds({"start": {"dateTime": "not-a-date"}, "end": {"dateTime": "x"}}) is None


def test_event_bounds_parses_zulu_times():
    bounds = event_bounds(
        {"start": {"dateTime": "2026-07-26T10:00:00Z"}, "end": {"dateTime": "2026-07-26T10:30:00Z"}}
    )
    assert bounds == (
        datetime(2026, 7, 26, 10, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 26, 10, 30, tzinfo=timezone.utc),
    )
