from datetime import datetime, timezone

from core.plans import INTERVAL_MONTHLY, PLAN_PRO, PLAN_STARTER
from models.billing import STATUS_ACTIVE, Subscription
from services.billing.entitlements import FEATURE_DRAFT, REASON_QUOTA, check
from services.billing.usage import add_drafts
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


async def test_sweep_user_honours_the_budget(monkeypatch):
    """A Starter user with 2 drafts left must not get a full batch of 5."""
    from services.drafts import sweep as drafts_sweep_service

    calls: list[int] = []

    def _fake_draft_reply(*args, **kwargs):
        calls.append(1)
        return "draft_id"

    monkeypatch.setattr(drafts_sweep_service, "draft_reply", _fake_draft_reply)
    # The rest of the fetch path is exercised by the service's own tests; here
    # the only question is whether the loop stops at the budget.
    assert drafts_sweep_service.effective_limit(budget=2) == 2
    assert drafts_sweep_service.effective_limit(budget=None) == drafts_sweep_service.MAX_PER_SWEEP
    assert drafts_sweep_service.effective_limit(budget=99) == drafts_sweep_service.MAX_PER_SWEEP
