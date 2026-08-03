import uuid
from datetime import datetime, timezone

from core.plans import INTERVAL_MONTHLY, PLAN_PRO, PLAN_STARTER
from models.billing import STATUS_ACTIVE, Subscription
from schemas.email import EmailSummary
from services.billing.entitlements import FEATURE_DRAFT, REASON_QUOTA, check
from services.billing.usage import add_drafts
from services.drafts.context import DraftConfig
from workers.jobs.drafts_sweep import may_draft, remaining_drafts

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


async def _subscribe(db, user, plan_id):
    db.add(
        Subscription(
            user_id=user.id,
            plan_id=plan_id,
            interval=INTERVAL_MONTHLY,
            status=STATUS_ACTIVE,
        )
    )
    await db.flush()


def _draft_config(**overrides) -> DraftConfig:
    base = dict(
        is_enabled=True,
        category_keys=("work",),
        selectivity="when_needed",
        tone="friendly",
        length="medium",
        custom_instructions=None,
        signature=None,
        follow_up_enabled=True,
        follow_up_days=3,
        model=None,
    )
    base.update(overrides)
    return DraftConfig(**base)


async def test_starter_stops_at_twenty_drafts(db, user):
    await _subscribe(db, user, PLAN_STARTER)
    await add_drafts(db, user.id, 20, NOW)
    decision = await check(db, user.id, FEATURE_DRAFT, now=NOW)
    assert decision.reason == REASON_QUOTA


async def test_may_draft_is_true_for_pro(db, user):
    await _subscribe(db, user, PLAN_PRO)
    assert await may_draft(db, user.id, NOW) is True


async def test_may_draft_is_false_when_locked(db, user):
    assert await may_draft(db, user.id, NOW) is False


async def test_remaining_is_none_for_pro(db, user):
    await _subscribe(db, user, PLAN_PRO)
    assert await remaining_drafts(db, user.id, NOW) is None


async def test_remaining_counts_down_for_starter(db, user):
    await _subscribe(db, user, PLAN_STARTER)
    assert await remaining_drafts(db, user.id, NOW) == 20
    await add_drafts(db, user.id, 18, NOW)
    assert await remaining_drafts(db, user.id, NOW) == 2


async def test_remaining_never_goes_negative(db, user):
    await _subscribe(db, user, PLAN_STARTER)
    await add_drafts(db, user.id, 25, NOW)
    assert await remaining_drafts(db, user.id, NOW) == 0


def test_effective_limit_caps_at_the_callers_own_max_per_sweep():
    """`effective_limit` never returns more than the caller's own per-sweep cap.

    `sweep.py` and `follow_up.py` each pass their own `MAX_PER_SWEEP` (10 and 5
    respectively) — this only pins the pure arithmetic, not that either
    `sweep_user` actually stops at it. See `test_sweep_user_stops_exactly_at_budget`
    and `test_follow_up_sweep_user_stops_exactly_at_budget` below for that.
    """
    from services.drafts import sweep as drafts_sweep_service

    assert drafts_sweep_service.effective_limit(budget=2) == 2
    assert drafts_sweep_service.effective_limit(budget=None) == drafts_sweep_service.MAX_PER_SWEEP
    assert drafts_sweep_service.effective_limit(budget=99) == drafts_sweep_service.MAX_PER_SWEEP


def _fake_emails(n: int) -> list[EmailSummary]:
    return [
        EmailSummary(
            id=f"msg-{i}",
            thread_id=f"thread-{i}",
            sender="someone@example.com",
            subject="Question",
            body="body text",
            snippet="body text",
        )
        for i in range(n)
    ]


def test_sweep_user_stops_exactly_at_budget(monkeypatch):
    """A Starter user with 2 drafts left must get exactly 2, not a full
    MAX_PER_SWEEP (10) batch — the property `effective_limit` alone cannot
    prove, since it never calls `sweep_user`.
    """
    from services.drafts import sweep as drafts_sweep_service

    async def _fake_labels_for_keys(db, user_id, keys):
        return [("work", "Label_Work")]

    monkeypatch.setattr(drafts_sweep_service, "_labels_for_keys", _fake_labels_for_keys)
    monkeypatch.setattr(
        drafts_sweep_service.gmail, "fetch_by_query", lambda *a, **k: _fake_emails(5)
    )

    calls: list[str] = []

    def _fake_draft_reply(*args, **kwargs):
        calls.append(kwargs.get("message_id") or args[0])
        return "draft_id"

    monkeypatch.setattr(drafts_sweep_service, "draft_reply", _fake_draft_reply)

    config = _draft_config()
    created = drafts_sweep_service.sweep_user(str(uuid.uuid4()), config, budget=2)

    assert created == 2
    assert len(calls) == 2


def test_sweep_user_unlimited_budget_gets_full_batch(monkeypatch):
    """A Pro user (budget=None) still gets a full MAX_PER_SWEEP batch, not zero."""
    from services.drafts import sweep as drafts_sweep_service

    async def _fake_labels_for_keys(db, user_id, keys):
        return [("work", "Label_Work")]

    monkeypatch.setattr(drafts_sweep_service, "_labels_for_keys", _fake_labels_for_keys)
    monkeypatch.setattr(
        drafts_sweep_service.gmail, "fetch_by_query", lambda *a, **k: _fake_emails(50)
    )

    calls: list[str] = []
    monkeypatch.setattr(
        drafts_sweep_service, "draft_reply", lambda *a, **k: calls.append(1) or "draft_id"
    )

    created = drafts_sweep_service.sweep_user(str(uuid.uuid4()), _draft_config(), budget=None)

    assert created == drafts_sweep_service.MAX_PER_SWEEP == 10
    assert len(calls) == 10


def test_follow_up_sweep_user_stops_exactly_at_budget(monkeypatch):
    """Follow-up nudges draw on the same quota: a Starter user with 2 drafts
    left must get exactly 2 nudges, not a full follow_up.MAX_PER_SWEEP (5).
    """
    from services.drafts import follow_up as follow_up_service

    candidates = [(email, 3) for email in _fake_emails(5)]
    monkeypatch.setattr(follow_up_service, "find_quiet_threads", lambda *a, **k: candidates)

    calls: list[str] = []

    def _fake_draft_follow_up(*args, **kwargs):
        calls.append(kwargs.get("message_id") or args[0])
        return "draft_id"

    monkeypatch.setattr(follow_up_service, "draft_follow_up", _fake_draft_follow_up)

    created = follow_up_service.sweep_user(str(uuid.uuid4()), _draft_config(), budget=2)

    assert created == 2
    assert len(calls) == 2


def test_follow_up_sweep_user_unlimited_budget_gets_its_own_full_batch(monkeypatch):
    """budget=None must map to follow_up's own MAX_PER_SWEEP (5), not sweep.py's (10)."""
    from services.drafts import follow_up as follow_up_service

    candidates = [(email, 3) for email in _fake_emails(20)]
    monkeypatch.setattr(follow_up_service, "find_quiet_threads", lambda *a, **k: candidates)

    calls: list[str] = []
    monkeypatch.setattr(
        follow_up_service, "draft_follow_up", lambda *a, **k: calls.append(1) or "draft_id"
    )

    created = follow_up_service.sweep_user(str(uuid.uuid4()), _draft_config(), budget=None)

    assert created == follow_up_service.MAX_PER_SWEEP == 5
    assert len(calls) == 5
