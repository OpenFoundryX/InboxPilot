from datetime import datetime, timedelta, timezone

from core.plans import INTERVAL_MONTHLY, PLAN_STARTER
from integrations.meetingbot.base import RecordingMedia
from models.billing import STATUS_ACTIVE, Subscription
from models.meetings import STATUS_PROCESSED, Meeting
from services.meetings import recording as recording_module
from workers.jobs.retention_sweep import prune_user

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


async def _starter(db, user):
    db.add(
        Subscription(
            user_id=user.id,
            plan_id=PLAN_STARTER,
            interval=INTERVAL_MONTHLY,
            status=STATUS_ACTIVE,
        )
    )
    await db.flush()


async def _meeting(db, user, days_ago: int) -> Meeting:
    meeting = Meeting(
        user_id=user.id,
        meeting_url="https://meet.example/x",
        status=STATUS_PROCESSED,
        starts_at=NOW - timedelta(days=days_ago),
        recording_id="rec_1",
        recording_url="https://cdn.example/v.mp4",
        transcript="hello",
        summary="a summary",
        decisions=["ship it"],
        action_items=[{"text": "follow up"}],
    )
    db.add(meeting)
    await db.flush()
    return meeting


async def test_video_is_dropped_past_the_starter_window(db, user):
    await _starter(db, user)
    meeting = await _meeting(db, user, days_ago=8)
    await prune_user(db, user.id, NOW)
    await db.refresh(meeting)
    assert meeting.recording_id is None
    assert meeting.recording_url is None


async def test_video_inside_the_window_is_kept(db, user):
    await _starter(db, user)
    meeting = await _meeting(db, user, days_ago=6)
    await prune_user(db, user.id, NOW)
    await db.refresh(meeting)
    assert meeting.recording_id == "rec_1"


async def test_pruning_keeps_the_summary_and_actions(db, user):
    await _starter(db, user)
    meeting = await _meeting(db, user, days_ago=200)
    await prune_user(db, user.id, NOW)
    await db.refresh(meeting)
    assert meeting.transcript is None
    assert meeting.summary == "a summary"
    assert meeting.decisions == ["ship it"]
    assert meeting.action_items == [{"text": "follow up"}]


async def test_transcript_inside_the_window_is_kept(db, user):
    await _starter(db, user)
    meeting = await _meeting(db, user, days_ago=89)
    await prune_user(db, user.id, NOW)
    await db.refresh(meeting)
    assert meeting.transcript == "hello"


async def test_pruning_is_idempotent(db, user):
    await _starter(db, user)
    await _meeting(db, user, days_ago=200)
    first = await prune_user(db, user.id, NOW)
    second = await prune_user(db, user.id, NOW)
    assert first["transcripts"] == 1
    assert second["transcripts"] == 0


async def test_pruning_marks_the_meeting_as_pruned(db, user):
    """`recording_pruned_at` is the signal `resolve_recording_url` relies on to
    refuse to re-fetch a deliberately cleared recording — prove pruning sets it.
    """
    await _starter(db, user)
    meeting = await _meeting(db, user, days_ago=8)
    await prune_user(db, user.id, NOW)
    await db.refresh(meeting)
    assert meeting.recording_pruned_at is not None


async def test_adhoc_meeting_with_no_starts_at_is_pruned_via_created_at(db, user):
    """Paste-a-link meetings never get a calendar `starts_at` (it stays NULL for
    the life of the row), so the prune falls back to `created_at` — otherwise
    these meetings would never age out on any plan.
    """
    await _starter(db, user)
    meeting = Meeting(
        user_id=user.id,
        meeting_url="https://meet.example/x",
        status=STATUS_PROCESSED,
        starts_at=None,
        created_at=NOW - timedelta(days=200),
        recording_id="rec_1",
        recording_url="https://cdn.example/v.mp4",
        transcript="hello",
        summary="a summary",
        decisions=["ship it"],
        action_items=[{"text": "follow up"}],
    )
    db.add(meeting)
    await db.flush()

    result = await prune_user(db, user.id, NOW)
    await db.refresh(meeting)

    assert result == {"videos": 1, "transcripts": 1}
    assert meeting.recording_id is None
    assert meeting.recording_url is None
    assert meeting.transcript is None


async def test_adhoc_meeting_inside_the_created_at_window_is_kept(db, user):
    await _starter(db, user)
    meeting = Meeting(
        user_id=user.id,
        meeting_url="https://meet.example/x",
        status=STATUS_PROCESSED,
        starts_at=None,
        created_at=NOW - timedelta(days=6),
        recording_id="rec_1",
        transcript="hello",
    )
    db.add(meeting)
    await db.flush()

    await prune_user(db, user.id, NOW)
    await db.refresh(meeting)

    assert meeting.recording_id == "rec_1"
    assert meeting.transcript == "hello"


async def test_resolve_recording_url_does_not_resurrect_a_pruned_video(db, user, monkeypatch):
    """The critical regression: a pruned meeting must stay pruned across a
    `resolve_recording_url` call (the path `GET /meetings/{id}` hits on every
    read), even though `bot_id` and `status` still look exactly like a meeting
    whose video simply hasn't been fetched yet.
    """
    calls: list[str] = []

    class _StillHasItProvider:
        def fetch_recording(self, bot_id):
            calls.append(bot_id)
            return RecordingMedia(
                video_url="https://cdn.example/resurrected.mp4",
                recording_id="rec_new",
                expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
            )

    monkeypatch.setattr(recording_module, "get_provider", lambda: _StillHasItProvider())

    meeting = Meeting(
        user_id=user.id,
        meeting_url="https://meet.example/x",
        status=STATUS_PROCESSED,
        bot_id="bot-1",
        recording_id=None,
        recording_url=None,
        recording_url_expires_at=None,
        recording_pruned_at=NOW - timedelta(days=1),
    )
    db.add(meeting)
    await db.flush()

    result = await recording_module.resolve_recording_url(db, meeting)

    assert result is None
    assert calls == []
    assert meeting.recording_id is None
    assert meeting.recording_url is None


async def test_resolve_recording_url_still_refreshes_a_stale_link_when_not_pruned(
    db, user, monkeypatch
):
    """The legitimate case this function exists for must keep working: an
    in-window meeting (never pruned) with an expired cached link still gets a
    fresh one from the provider.
    """
    real_now = datetime.now(timezone.utc)

    class _FreshProvider:
        def fetch_recording(self, bot_id):
            return RecordingMedia(
                video_url="https://cdn.example/fresh.mp4",
                recording_id="rec_1",
                expires_at=real_now + timedelta(hours=2),
            )

    monkeypatch.setattr(recording_module, "get_provider", lambda: _FreshProvider())

    meeting = Meeting(
        user_id=user.id,
        meeting_url="https://meet.example/x",
        status=STATUS_PROCESSED,
        bot_id="bot-1",
        recording_id="rec_1",
        recording_url="https://cdn.example/old.mp4",
        recording_url_expires_at=real_now - timedelta(hours=1),
    )
    db.add(meeting)
    await db.flush()

    result = await recording_module.resolve_recording_url(db, meeting)

    assert result == "https://cdn.example/fresh.mp4"
    assert meeting.recording_url == "https://cdn.example/fresh.mp4"
