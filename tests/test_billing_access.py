from datetime import datetime, timedelta, timezone

from core.plans import INTERVAL_MONTHLY, PLAN_PRO, PLAN_STARTER
from models.billing import (
    STATUS_ACTIVE,
    STATUS_AUTHENTICATED,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_CREATED,
    STATUS_EXPIRED,
    STATUS_HALTED,
    STATUS_PAUSED,
    STATUS_PENDING,
    Subscription,
)
from services.billing.access import (
    ACCESS_ENTITLED,
    ACCESS_LOCKED,
    effective_plan_id,
    resolve_access,
)
from services.billing.store import get_or_create_subscription, get_subscription

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)


def _sub(**overrides) -> Subscription:
    defaults = {
        "plan_id": PLAN_PRO,
        "interval": INTERVAL_MONTHLY,
        "status": STATUS_ACTIVE,
        "comped": False,
        "trial_ends_at": None,
    }
    return Subscription(**{**defaults, **overrides})


def test_no_subscription_is_locked():
    assert resolve_access(None, NOW) == ACCESS_LOCKED


def test_active_is_entitled():
    assert resolve_access(_sub(status=STATUS_ACTIVE), NOW) == ACCESS_ENTITLED


def test_authenticated_within_the_trial_window_is_entitled():
    sub = _sub(status=STATUS_AUTHENTICATED, trial_ends_at=NOW + timedelta(days=3))
    assert resolve_access(sub, NOW) == ACCESS_ENTITLED


def test_authenticated_past_the_trial_window_is_locked():
    sub = _sub(status=STATUS_AUTHENTICATED, trial_ends_at=NOW - timedelta(seconds=1))
    assert resolve_access(sub, NOW) == ACCESS_LOCKED


def test_pending_keeps_access_while_razorpay_retries():
    assert resolve_access(_sub(status=STATUS_PENDING), NOW) == ACCESS_ENTITLED


def test_created_is_locked_because_no_mandate_exists():
    assert resolve_access(_sub(status=STATUS_CREATED), NOW) == ACCESS_LOCKED


def test_terminal_and_stopped_statuses_are_locked():
    for status in (
        STATUS_CANCELLED,
        STATUS_EXPIRED,
        STATUS_COMPLETED,
        STATUS_HALTED,
        STATUS_PAUSED,
    ):
        assert resolve_access(_sub(status=status), NOW) == ACCESS_LOCKED


def test_comped_beats_every_status():
    assert resolve_access(_sub(status=STATUS_CANCELLED, comped=True), NOW) == ACCESS_ENTITLED


def test_effective_plan_of_a_comped_row_is_pro():
    assert effective_plan_id(_sub(plan_id=PLAN_STARTER, comped=True)) == PLAN_PRO


def test_effective_plan_follows_the_column():
    assert effective_plan_id(_sub(plan_id=PLAN_STARTER)) == PLAN_STARTER


def test_effective_plan_without_a_row_is_starter():
    assert effective_plan_id(None) == PLAN_STARTER


async def test_get_or_create_starts_a_trial(db, user):
    sub = await get_or_create_subscription(db, user.id, trial_days=7)
    assert sub.status == STATUS_AUTHENTICATED
    assert sub.plan_id == PLAN_PRO
    assert sub.trial_ends_at is not None


async def test_get_or_create_is_idempotent(db, user):
    first = await get_or_create_subscription(db, user.id, trial_days=7)
    second = await get_or_create_subscription(db, user.id, trial_days=7)
    assert first.id == second.id
    assert first.trial_ends_at == second.trial_ends_at


async def test_get_subscription_returns_none_for_a_fresh_user(db, user):
    assert await get_subscription(db, user.id) is None
