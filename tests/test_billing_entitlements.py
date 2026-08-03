from datetime import datetime, timedelta, timezone

import pytest

from core.plans import INTERVAL_MONTHLY, PLAN_PRO, PLAN_STARTER
from models.billing import STATUS_ACTIVE, STATUS_CANCELLED, Subscription
from models.routines import ROUTINE_BRIEFING, ROUTINE_INVOICES
from services.billing.entitlements import (
    FEATURE_CUSTOM_CATEGORIES,
    FEATURE_DRAFT,
    FEATURE_MEETING_BOT,
    FEATURE_ROUTINE,
    REASON_LOCKED,
    REASON_PLAN,
    REASON_QUOTA,
    check,
)
from services.billing.usage import add_bot_seconds, add_drafts

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


async def _subscribe(db, user, plan_id=PLAN_PRO, status=STATUS_ACTIVE):
    sub = Subscription(
        user_id=user.id,
        plan_id=plan_id,
        interval=INTERVAL_MONTHLY,
        status=status,
        trial_ends_at=NOW + timedelta(days=3),
    )
    db.add(sub)
    await db.flush()
    return sub


async def test_user_without_a_subscription_is_denied(db, user):
    decision = await check(db, user.id, FEATURE_MEETING_BOT, now=NOW)
    assert decision.allowed is False
    assert decision.reason == REASON_LOCKED


async def test_canceled_user_is_denied(db, user):
    await _subscribe(db, user, status=STATUS_CANCELLED)
    decision = await check(db, user.id, FEATURE_MEETING_BOT, now=NOW)
    assert decision.reason == REASON_LOCKED


async def test_pro_under_quota_may_book_a_bot(db, user):
    await _subscribe(db, user, PLAN_PRO)
    await add_bot_seconds(db, user.id, 14 * 3600, NOW)
    assert (await check(db, user.id, FEATURE_MEETING_BOT, now=NOW)).allowed is True


async def test_starter_at_exactly_the_cap_is_denied(db, user):
    await _subscribe(db, user, PLAN_STARTER)
    await add_bot_seconds(db, user.id, 5 * 3600, NOW)
    decision = await check(db, user.id, FEATURE_MEETING_BOT, now=NOW)
    assert decision.allowed is False
    assert decision.reason == REASON_QUOTA


async def test_starter_just_under_the_cap_is_allowed(db, user):
    await _subscribe(db, user, PLAN_STARTER)
    await add_bot_seconds(db, user.id, 5 * 3600 - 1, NOW)
    assert (await check(db, user.id, FEATURE_MEETING_BOT, now=NOW)).allowed is True


async def test_starter_draft_quota_stops_at_twenty(db, user):
    await _subscribe(db, user, PLAN_STARTER)
    await add_drafts(db, user.id, 19, NOW)
    assert (await check(db, user.id, FEATURE_DRAFT, now=NOW)).allowed is True
    await add_drafts(db, user.id, 1, NOW)
    denied = await check(db, user.id, FEATURE_DRAFT, now=NOW)
    assert denied.allowed is False
    assert denied.reason == REASON_QUOTA


async def test_pro_drafts_are_unlimited(db, user):
    await _subscribe(db, user, PLAN_PRO)
    await add_drafts(db, user.id, 5000, NOW)
    assert (await check(db, user.id, FEATURE_DRAFT, now=NOW)).allowed is True


async def test_starter_gets_the_briefing_routine_only(db, user):
    await _subscribe(db, user, PLAN_STARTER)
    ok = await check(db, user.id, FEATURE_ROUTINE, routine_type=ROUTINE_BRIEFING, now=NOW)
    denied = await check(db, user.id, FEATURE_ROUTINE, routine_type=ROUTINE_INVOICES, now=NOW)
    assert ok.allowed is True
    assert denied.allowed is False
    assert denied.reason == REASON_PLAN


async def test_pro_gets_every_routine(db, user):
    await _subscribe(db, user, PLAN_PRO)
    decision = await check(db, user.id, FEATURE_ROUTINE, routine_type=ROUTINE_INVOICES, now=NOW)
    assert decision.allowed is True


async def test_custom_categories_are_pro_only(db, user):
    await _subscribe(db, user, PLAN_STARTER)
    assert (await check(db, user.id, FEATURE_CUSTOM_CATEGORIES, now=NOW)).reason == REASON_PLAN


async def test_comped_user_gets_pro_regardless_of_status(db, user):
    sub = await _subscribe(db, user, PLAN_STARTER, status=STATUS_CANCELLED)
    sub.comped = True
    await db.flush()
    assert (await check(db, user.id, FEATURE_CUSTOM_CATEGORIES, now=NOW)).allowed is True


async def test_unknown_feature_is_a_programming_error(db, user):
    import pytest

    await _subscribe(db, user)
    with pytest.raises(ValueError):
        await check(db, user.id, "meetings.teleport", now=NOW)


async def test_require_entitled_rejects_a_locked_user(db, user):
    from fastapi import HTTPException

    from services.billing.dependencies import require_entitled

    with pytest.raises(HTTPException) as excinfo:
        await require_entitled(user, db)
    assert excinfo.value.status_code == 402


async def test_require_entitled_passes_an_active_user(db, user):
    from services.billing.dependencies import require_entitled

    await _subscribe(db, user)
    assert await require_entitled(user, db) is user
