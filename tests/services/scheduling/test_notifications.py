"""The manage link is the reason this mail exists, so it is what's asserted."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from models.scheduling import SchedulingBooking
from services.scheduling import notifications

IST = ZoneInfo("Asia/Kolkata")


def booking(**overrides) -> SchedulingBooking:
    row = SchedulingBooking(
        starts_at=datetime(2026, 8, 10, 3, 30, tzinfo=timezone.utc),  # 09:00 IST
        ends_at=datetime(2026, 8, 10, 4, 0, tzinfo=timezone.utc),
        booker_name="Ada",
        booker_email="ada@example.com",
        attendee_emails=[],
        title="30 Minute Meeting",
        answers={},
        status="confirmed",
        management_token="tok_abc123",
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


def test_manage_url_is_built_from_the_token():
    assert notifications.manage_url(booking()).endswith("/booking/tok_abc123")


def test_confirmation_carries_the_manage_link():
    body = notifications.confirmation_body(booking(), "Nilesh", IST)
    assert "/booking/tok_abc123" in body


def test_times_are_rendered_in_the_hosts_zone():
    body = notifications.confirmation_body(booking(), "Nilesh", IST)
    assert "09:00" in body
    assert "Monday 10 August 2026" in body


def test_the_calendar_description_also_carries_the_link():
    """Belt and braces: if confirmation email is off, this is the guest's only
    route back to their booking."""
    assert "/booking/tok_abc123" in notifications.event_description(booking(), IST)


def test_the_description_includes_notes_and_answers():
    text = notifications.event_description(
        booking(notes="Prep the deck", answers={"Company": "Acme"}), IST
    )
    assert "Prep the deck" in text
    assert "Company: Acme" in text


def test_confirmation_includes_the_meeting_url_when_there_is_one():
    body = notifications.confirmation_body(
        booking(meeting_url="https://meet.google.com/abc"), "Nilesh", IST
    )
    assert "https://meet.google.com/abc" in body


def test_cancellation_names_who_cancelled():
    assert "by you" in notifications.cancellation_body(booking(), "Nilesh", IST, "guest")
    assert "by Nilesh" in notifications.cancellation_body(booking(), "Nilesh", IST, "host")


def test_cancellation_includes_a_reason_when_given():
    body = notifications.cancellation_body(
        booking(cancel_reason="Conflict came up"), "Nilesh", IST, "host"
    )
    assert "Conflict came up" in body


def test_reschedule_shows_both_times():
    previous = datetime(2026, 8, 9, 3, 30, tzinfo=timezone.utc)
    body = notifications.reschedule_body(booking(), "Nilesh", IST, previous)
    assert "Sunday 09 August 2026" in body  # was
    assert "Monday 10 August 2026" in body  # now


def test_reminder_offers_a_way_out():
    body = notifications.reminder_body(booking(), "Nilesh", IST)
    assert "/booking/tok_abc123" in body
