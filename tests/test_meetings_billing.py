from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from core.plans import INTERVAL_MONTHLY, PLAN_STARTER
from models.billing import STATUS_ACTIVE, Subscription
from models.meetings import Meeting
from services.billing.entitlements import FEATURE_MEETING_BOT, check
from services.billing.usage import add_bot_seconds
from workers.jobs.process_meeting import compute_duration_seconds

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


async def _returns(value):
    """Adapts a plain value into the awaitable the sweep expects."""
    return value


def test_duration_prefers_the_provider_value():
    meeting = Meeting(meeting_url="x", joined_at=NOW)
    assert compute_duration_seconds(meeting, {"duration_seconds": 1234}, now=NOW) == 1234


def test_duration_falls_back_to_wall_clock_since_join():
    meeting = Meeting(meeting_url="x", joined_at=NOW - timedelta(minutes=30))
    assert compute_duration_seconds(meeting, {}, now=NOW) == 1800


def test_duration_is_zero_when_the_bot_never_joined():
    meeting = Meeting(meeting_url="x", joined_at=None)
    assert compute_duration_seconds(meeting, {}, now=NOW) == 0


async def test_starter_over_cap_may_not_book(db, user):
    db.add(
        Subscription(
            user_id=user.id,
            plan_id=PLAN_STARTER,
            interval=INTERVAL_MONTHLY,
            status=STATUS_ACTIVE,
        )
    )
    await db.flush()
    await add_bot_seconds(db, user.id, 5 * 3600, NOW)

    assert (await check(db, user.id, FEATURE_MEETING_BOT, now=NOW)).allowed is False


async def test_locked_user_may_not_book(db, user):
    assert (await check(db, user.id, FEATURE_MEETING_BOT, now=NOW)).allowed is False


async def test_sweep_books_no_bot_for_a_locked_user(db, user, monkeypatch):
    """Assert on the absence of the provider call, not just the return value.

    The cost being avoided is the outbound booking, so that is what the test
    has to watch.
    """
    from models.meetings import MeetingSettings
    from workers.jobs import meetings_sweep

    db.add(MeetingSettings(user_id=user.id, enabled=True, auto_join=True))
    meeting = Meeting(
        user_id=user.id,
        meeting_url="https://meet.example/x",
        starts_at=NOW + timedelta(minutes=10),
    )
    db.add(meeting)
    await db.flush()

    booked: list[str] = []
    monkeypatch.setattr(
        meetings_sweep, "_book_bot", lambda m, name, now: booked.append(str(m.id)) or True
    )
    monkeypatch.setattr(meetings_sweep.calendar, "list_events", lambda *a, **k: [])
    monkeypatch.setattr(
        meetings_sweep, "list_awaiting_bots", lambda db_, uid: _returns([meeting])
    )
    monkeypatch.setattr(meetings_sweep, "list_scheduled_upcoming", lambda db_, uid: _returns([]))

    settings_row = await db.scalar(
        select(MeetingSettings).where(MeetingSettings.user_id == user.id)
    )
    await meetings_sweep._sweep_user(db, settings_row)

    assert booked == []
