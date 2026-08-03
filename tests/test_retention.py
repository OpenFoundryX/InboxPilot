from datetime import datetime, timedelta, timezone

from core.plans import INTERVAL_MONTHLY, PLAN_STARTER
from models.billing import STATUS_ACTIVE, Subscription
from models.meetings import STATUS_PROCESSED, Meeting
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
