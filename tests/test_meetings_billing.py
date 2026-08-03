from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from core.database import get_db
from core.plans import INTERVAL_MONTHLY, PLAN_PRO, PLAN_STARTER
from main import app
from models.billing import STATUS_ACTIVE, Subscription
from models.meetings import Meeting
from services.auth.dependencies import get_current_user
from services.billing.entitlements import FEATURE_MEETING_BOT, check
from services.billing.usage import add_bot_seconds, get_or_create_counter
from workers.jobs.process_meeting import compute_duration_seconds

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
async def client(db, user):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


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


async def test_empty_transcript_meeting_still_meters(db, user, monkeypatch):
    """A silent call still occupied real bot wall-clock time; it must still meter.

    Before this fix, `_process` returned as soon as it saw an empty transcript,
    before ever reaching the metering block — so a call where nobody spoke was
    billed as zero, making that bot-hour free.
    """
    from integrations.meetingbot.base import Transcript
    from workers.jobs import process_meeting

    class _SilentProvider:
        def fetch_transcript(self, bot_id):
            return Transcript(segments=[])

    monkeypatch.setattr(process_meeting, "get_provider", lambda: _SilentProvider())

    meeting = Meeting(
        user_id=user.id,
        meeting_url="https://meet.example/x",
        bot_id="bot-1",
        joined_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    db.add(meeting)
    await db.flush()

    result = await process_meeting._process(db, str(meeting.id))

    assert result == {"skipped": "empty transcript"}
    assert meeting.duration_seconds is not None
    assert meeting.duration_seconds > 0

    counter = await get_or_create_counter(db, user.id, datetime.now(timezone.utc))
    assert counter.bot_seconds_used == meeting.duration_seconds


async def test_reprocessing_a_meeting_meters_once(db, user, monkeypatch):
    """The `duration_seconds is None` guard is what stops a `process_meeting`
    retry from double-counting a meeting's bot-seconds.

    Runs the same meeting through `_process` twice and checks the usage
    counter — the thing quota decisions actually read — reflects one pass,
    not a return value that could be right for the wrong reason.
    """
    from workers.jobs import process_meeting

    # Short-circuits after metering so this test doesn't need a real LLM call.
    monkeypatch.setattr(process_meeting, "summarize", lambda *a, **k: None)

    meeting = Meeting(
        user_id=user.id,
        meeting_url="https://meet.example/x",
        bot_id="bot-1",
        joined_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        transcript="Alice: hello there",
    )
    db.add(meeting)
    await db.flush()

    await process_meeting._process(db, str(meeting.id))
    first = await get_or_create_counter(db, user.id, datetime.now(timezone.utc))
    assert first.bot_seconds_used > 0

    await process_meeting._process(db, str(meeting.id))
    second = await get_or_create_counter(db, user.id, datetime.now(timezone.utc))
    assert second.bot_seconds_used == first.bot_seconds_used


async def test_join_now_books_no_bot_for_a_locked_user(db, user, monkeypatch):
    """The pasted-link path must not be a way to dodge the quota gate.

    Same intent as `test_sweep_books_no_bot_for_a_locked_user`: assert on the
    absence of the outbound booking call, not merely on the return value.
    """
    from workers.jobs import meetings_sweep

    meeting = Meeting(user_id=user.id, meeting_url="https://meet.example/x")
    db.add(meeting)
    await db.flush()

    booked: list[str] = []
    monkeypatch.setattr(
        meetings_sweep, "_book_bot", lambda m, name, now: booked.append(str(m.id)) or True
    )

    result = await meetings_sweep._join_now(db, str(meeting.id))

    assert booked == []
    assert result["booked"] is False
    assert meeting.status_detail == "bot withheld: locked"


async def test_join_now_books_a_bot_when_entitled(db, user, monkeypatch):
    """Regression guard: the new gate must not block a normal, entitled join."""
    from workers.jobs import meetings_sweep

    db.add(
        Subscription(
            user_id=user.id,
            plan_id=PLAN_PRO,
            interval=INTERVAL_MONTHLY,
            status=STATUS_ACTIVE,
        )
    )
    meeting = Meeting(user_id=user.id, meeting_url="https://meet.example/x")
    db.add(meeting)
    await db.flush()

    booked: list[str] = []
    monkeypatch.setattr(
        meetings_sweep, "_book_bot", lambda m, name, now: booked.append(str(m.id)) or True
    )

    result = await meetings_sweep._join_now(db, str(meeting.id))

    assert booked == [str(meeting.id)]
    assert result["booked"] is True


async def test_join_route_rejects_a_locked_user_with_402_and_enqueues_nothing(
    client, monkeypatch
):
    """The synchronous, user-initiated path must give a real answer, not a
    202 that quietly does nothing: a user with no active subscription hits
    `POST /meetings/join` and should get a 402, with no Celery task ever
    enqueued — checked on the absence of the `.delay()` call itself, not just
    the status code, since a 402 with a task enqueued anyway would still be
    the bug this exists to prevent.
    """
    from workers.jobs.meetings_sweep import join_now

    called: list[tuple] = []
    monkeypatch.setattr(join_now, "delay", lambda *a, **k: called.append(a))

    response = await client.post(
        "/v1/meetings/join", json={"meeting_url": "https://meet.google.com/abc-defg-hij"}
    )

    assert response.status_code == 402
    assert called == []


async def test_join_route_succeeds_for_an_entitled_user(client, db, user, monkeypatch):
    """Regression guard: the new 402 gate must not block a normal join."""
    from workers.jobs.meetings_sweep import join_now

    db.add(
        Subscription(
            user_id=user.id,
            plan_id=PLAN_PRO,
            interval=INTERVAL_MONTHLY,
            status=STATUS_ACTIVE,
        )
    )
    await db.flush()

    called: list[tuple] = []
    monkeypatch.setattr(join_now, "delay", lambda *a, **k: called.append(a))

    response = await client.post(
        "/v1/meetings/join", json={"meeting_url": "https://meet.google.com/abc-defg-hij"}
    )

    assert response.status_code == 202
    assert len(called) == 1


async def test_bot_route_rejects_a_locked_user_with_402_and_enqueues_nothing(
    client, monkeypatch
):
    """Same guarantee as the `/join` route, for the calendar-event path."""
    from workers.jobs.meetings_sweep import join_now

    called: list[tuple] = []
    monkeypatch.setattr(join_now, "delay", lambda *a, **k: called.append(a))

    response = await client.post("/v1/meetings/bot", json={"calendar_event_id": "evt-1"})

    assert response.status_code == 402
    assert called == []


async def test_bot_route_succeeds_for_an_entitled_user(client, db, user, monkeypatch):
    """Regression guard: an entitled user enabling the bot on a real calendar
    event still gets booked, past the new quota gate.
    """
    from api.v1 import meetings as meetings_api
    from workers.jobs.meetings_sweep import join_now

    db.add(
        Subscription(
            user_id=user.id,
            plan_id=PLAN_PRO,
            interval=INTERVAL_MONTHLY,
            status=STATUS_ACTIVE,
        )
    )
    await db.flush()

    now = datetime.now(timezone.utc)
    event = {
        "id": "evt-1",
        "summary": "Standup",
        "hangoutLink": "https://meet.google.com/abc-defg-hij",
        "start": {"dateTime": (now + timedelta(minutes=30)).isoformat()},
        "end": {"dateTime": (now + timedelta(minutes=60)).isoformat()},
    }
    monkeypatch.setattr(meetings_api.calendar, "list_events", lambda *a, **k: [event])

    called: list[tuple] = []
    monkeypatch.setattr(join_now, "delay", lambda *a, **k: called.append(a))

    response = await client.post("/v1/meetings/bot", json={"calendar_event_id": "evt-1"})

    assert response.status_code == 202
    assert len(called) == 1
