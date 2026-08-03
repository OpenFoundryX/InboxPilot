from datetime import datetime, timedelta, timezone

from core.plans import INTERVAL_MONTHLY, PLAN_STARTER
from integrations.meetingbot.base import RecordingMedia
from models.billing import STATUS_ACTIVE, STATUS_CANCELLED, Subscription
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


# --- Ruling 1: pruning pauses entirely while an account is locked ---------


async def test_locked_user_with_no_subscription_is_not_pruned(db, user):
    """No subscription row means locked (`resolve_access(None, now) ==
    ACCESS_LOCKED`), not "give them Starter's windows". A merely-locked account
    must not have its data destroyed faster than a paying one's.
    """
    meeting = await _meeting(db, user, days_ago=200)

    result = await prune_user(db, user.id, NOW)
    await db.refresh(meeting)

    assert result == {"videos": 0, "transcripts": 0}
    assert meeting.recording_id == "rec_1"
    assert meeting.recording_url == "https://cdn.example/v.mp4"
    assert meeting.transcript == "hello"


async def test_locked_user_with_a_terminal_subscription_is_not_pruned(db, user):
    """A `cancelled` (or `halted`/`expired`) subscription row is equally locked
    — the same rule must apply whether the row is missing or terminal.
    """
    db.add(
        Subscription(
            user_id=user.id,
            plan_id=PLAN_STARTER,
            interval=INTERVAL_MONTHLY,
            status=STATUS_CANCELLED,
        )
    )
    await db.flush()
    meeting = await _meeting(db, user, days_ago=200)

    result = await prune_user(db, user.id, NOW)
    await db.refresh(meeting)

    assert result == {"videos": 0, "transcripts": 0}
    assert meeting.recording_id == "rec_1"
    assert meeting.transcript == "hello"


# --- Ruling 2: existing recordings keep the window they were made under ---


async def test_meeting_recorded_under_pro_survives_a_downgrade_to_starter(db, user):
    """A meeting recorded while the user was on Pro (30-day video window) keeps
    that window even after the account downgrades to Starter (7-day window) —
    the plan a meeting was captured under grandfathers it, a later downgrade
    does not retroactively shorten an already-fixed deadline.
    """
    await _starter(db, user)  # current plan, post-downgrade
    meeting = Meeting(
        user_id=user.id,
        meeting_url="https://meet.example/x",
        status=STATUS_PROCESSED,
        starts_at=NOW - timedelta(days=20),  # past Starter's 7d, inside Pro's 30d
        recording_id="rec_1",
        recording_url="https://cdn.example/v.mp4",
        transcript="hello",
        retention_video_days=30,
        retention_transcript_days=365,
    )
    db.add(meeting)
    await db.flush()

    result = await prune_user(db, user.id, NOW)
    await db.refresh(meeting)

    assert result == {"videos": 0, "transcripts": 0}
    assert meeting.recording_id == "rec_1"
    assert meeting.transcript == "hello"


async def test_meeting_recorded_after_the_downgrade_prunes_on_starters_schedule(db, user):
    """A meeting captured after the downgrade was stamped with Starter's own
    windows at processing time, so it prunes on Starter's schedule like any
    other Starter meeting.
    """
    await _starter(db, user)
    meeting = Meeting(
        user_id=user.id,
        meeting_url="https://meet.example/x",
        status=STATUS_PROCESSED,
        starts_at=NOW - timedelta(days=8),
        recording_id="rec_1",
        recording_url="https://cdn.example/v.mp4",
        transcript="hello",
        retention_video_days=7,
        retention_transcript_days=90,
    )
    db.add(meeting)
    await db.flush()

    result = await prune_user(db, user.id, NOW)
    await db.refresh(meeting)

    assert result == {"videos": 1, "transcripts": 0}
    assert meeting.recording_id is None


async def test_legacy_meeting_with_null_retention_columns_uses_the_current_plan(db, user):
    """A row from before this column existed has no stored window at all — the
    only sane fallback is the plan the account is on right now.
    """
    await _starter(db, user)
    meeting = Meeting(
        user_id=user.id,
        meeting_url="https://meet.example/x",
        status=STATUS_PROCESSED,
        starts_at=NOW - timedelta(days=8),
        recording_id="rec_1",
        recording_url="https://cdn.example/v.mp4",
        transcript="hello",
        retention_video_days=None,
        retention_transcript_days=None,
    )
    db.add(meeting)
    await db.flush()

    result = await prune_user(db, user.id, NOW)
    await db.refresh(meeting)

    assert result == {"videos": 1, "transcripts": 0}
    assert meeting.recording_id is None
