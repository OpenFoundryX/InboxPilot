from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from core.database import get_db
from core.plans import INTERVAL_MONTHLY, PLAN_PRO
from main import app
from models.billing import (
    STATUS_ACTIVE,
    STATUS_AUTHENTICATED,
    STATUS_CANCELLED,
    Subscription,
)
from services.auth.dependencies import get_current_user


@pytest.fixture
async def client(db, user):
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: user
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def fake_razorpay(monkeypatch):
    """Stand in for the Razorpay API so tests never make a network call."""
    from services.billing import razorpay_client

    calls = {}

    def _create_customer(*, email, name=None):
        calls["customer"] = {"email": email, "name": name}
        return "cust_test123"

    def _create_subscription(*, plan_id, customer_id, start_at, total_count, notes):
        calls["subscription"] = {
            "plan_id": plan_id,
            "customer_id": customer_id,
            "start_at": start_at,
            "total_count": total_count,
            "notes": notes,
        }
        return {"id": "sub_test123", "status": "created"}

    def _cancel_subscription(*, subscription_id, at_cycle_end=True):
        calls["cancel"] = {"subscription_id": subscription_id, "at_cycle_end": at_cycle_end}
        return {"id": subscription_id, "status": "active"}

    monkeypatch.setattr(razorpay_client, "create_customer", _create_customer)
    monkeypatch.setattr(razorpay_client, "create_subscription", _create_subscription)
    monkeypatch.setattr(razorpay_client, "cancel_subscription", _cancel_subscription)
    return calls


async def test_plans_endpoint_lists_both_tiers(client):
    response = await client.get("/v1/billing/plans")
    assert response.status_code == 200
    ids = [p["id"] for p in response.json()["plans"]]
    assert ids == ["starter", "pro"]


async def test_plans_endpoint_exposes_entitlements(client):
    body = (await client.get("/v1/billing/plans")).json()
    pro = next(p for p in body["plans"] if p["id"] == "pro")
    assert pro["monthly_price_cents"] == 3900
    assert pro["bot_hours_per_month"] == 15
    assert pro["drafts_per_month"] is None
    assert pro["currency"] == "USD"


async def test_subscription_endpoint_reports_locked_without_a_row(client):
    body = (await client.get("/v1/billing/subscription")).json()
    assert body["access"] == "locked"
    assert body["plan_id"] is None


async def test_subscription_endpoint_reports_usage(db, user, client):
    db.add(
        Subscription(
            user_id=user.id,
            plan_id=PLAN_PRO,
            interval=INTERVAL_MONTHLY,
            status=STATUS_ACTIVE,
            current_period_end=datetime.now(timezone.utc) + timedelta(days=20),
        )
    )
    await db.flush()

    body = (await client.get("/v1/billing/subscription")).json()
    assert body["access"] == "entitled"
    assert body["plan_id"] == "pro"
    assert body["usage"]["bot_hours_used"] == 0
    assert body["usage"]["bot_hours_included"] == 15


async def test_checkout_returns_subscription_id_and_public_key(client, fake_razorpay):
    response = await client.post(
        "/v1/billing/checkout", json={"plan_id": "pro", "interval": "monthly"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["subscription_id"] == "sub_test123"
    assert "key_id" in body
    # The secret must never be handed to a client.
    assert "key_secret" not in body


async def test_checkout_schedules_the_trial_seven_days_out(client, fake_razorpay):
    before = datetime.now(timezone.utc)
    await client.post("/v1/billing/checkout", json={"plan_id": "pro", "interval": "monthly"})

    start_at = fake_razorpay["subscription"]["start_at"]
    scheduled = datetime.fromtimestamp(start_at, tz=timezone.utc)
    assert timedelta(days=6, hours=23) < scheduled - before < timedelta(days=7, hours=1)


async def test_checkout_persists_the_razorpay_ids(db, user, client, fake_razorpay):
    from sqlalchemy import select

    await client.post("/v1/billing/checkout", json={"plan_id": "pro", "interval": "monthly"})

    sub = await db.scalar(select(Subscription).where(Subscription.user_id == user.id))
    assert sub.razorpay_customer_id == "cust_test123"
    assert sub.razorpay_subscription_id == "sub_test123"
    assert sub.plan_id == "pro"
    assert sub.trial_consumed is True


async def test_checkout_continues_a_still_running_trial_instead_of_restarting(
    db, user, client, fake_razorpay
):
    """Cancel mid-trial and check out again must not mint a fresh 7 days.

    `trial_consumed` is the once-per-customer marker: while the original
    `trial_ends_at` is still in the future, a new checkout must schedule
    Razorpay's `start_at` against that same instant, not `now + TRIAL_DAYS`.
    """
    promised = datetime.now(timezone.utc) + timedelta(days=4, hours=6)
    db.add(
        Subscription(
            user_id=user.id,
            plan_id=PLAN_PRO,
            interval=INTERVAL_MONTHLY,
            status=STATUS_CANCELLED,
            razorpay_customer_id="cust_existing",
            razorpay_subscription_id="sub_old_cancelled",
            trial_ends_at=promised,
            trial_consumed=True,
            cancel_at_period_end=True,
        )
    )
    await db.flush()

    await client.post("/v1/billing/checkout", json={"plan_id": "pro", "interval": "monthly"})

    start_at = datetime.fromtimestamp(
        fake_razorpay["subscription"]["start_at"], tz=timezone.utc
    )
    assert abs((start_at - promised).total_seconds()) < 2

    from sqlalchemy import select

    sub = await db.scalar(select(Subscription).where(Subscription.user_id == user.id))
    assert sub.trial_ends_at == promised
    assert sub.trial_consumed is True
    # A prior cancel-at-period-end must not stick to the new subscription.
    assert sub.cancel_at_period_end is False


async def test_checkout_charges_immediately_once_the_trial_is_consumed(
    db, user, client, fake_razorpay
):
    """Trials are once per customer: after the window has elapsed, a new
    checkout must start charging immediately (`start_at ≈ now`), not grant
    another free week.
    """
    before = datetime.now(timezone.utc)
    db.add(
        Subscription(
            user_id=user.id,
            plan_id=PLAN_PRO,
            interval=INTERVAL_MONTHLY,
            status=STATUS_CANCELLED,
            razorpay_customer_id="cust_existing",
            razorpay_subscription_id="sub_old_cancelled",
            trial_ends_at=before - timedelta(days=1),
            trial_consumed=True,
        )
    )
    await db.flush()

    await client.post("/v1/billing/checkout", json={"plan_id": "pro", "interval": "monthly"})

    start_at = datetime.fromtimestamp(
        fake_razorpay["subscription"]["start_at"], tz=timezone.utc
    )
    assert abs((start_at - before).total_seconds()) < 5


async def test_checkout_reuses_an_existing_customer(db, user, client, fake_razorpay):
    db.add(
        Subscription(
            user_id=user.id,
            plan_id=PLAN_PRO,
            interval=INTERVAL_MONTHLY,
            status=STATUS_AUTHENTICATED,
            razorpay_customer_id="cust_existing",
        )
    )
    await db.flush()

    await client.post("/v1/billing/checkout", json={"plan_id": "pro", "interval": "monthly"})
    assert fake_razorpay["subscription"]["customer_id"] == "cust_existing"
    assert "customer" not in fake_razorpay


async def test_checkout_rejects_an_unknown_plan(client):
    response = await client.post(
        "/v1/billing/checkout", json={"plan_id": "team", "interval": "monthly"}
    )
    assert response.status_code == 422


async def test_checkout_rejects_an_unknown_interval(client):
    response = await client.post(
        "/v1/billing/checkout", json={"plan_id": "pro", "interval": "weekly"}
    )
    assert response.status_code == 422


async def test_checkout_rejects_when_a_live_subscription_already_exists(
    db, user, client, fake_razorpay
):
    """A second checkout over an active subscription must not orphan the first.

    Without a guard, this would call `create_subscription` again and overwrite
    `razorpay_subscription_id` — the original subscription keeps billing at
    Razorpay with no row in our database pointing at it anymore.
    """
    from sqlalchemy import select

    db.add(
        Subscription(
            user_id=user.id,
            plan_id=PLAN_PRO,
            interval=INTERVAL_MONTHLY,
            status=STATUS_ACTIVE,
            razorpay_subscription_id="sub_existing_live",
        )
    )
    await db.flush()

    response = await client.post(
        "/v1/billing/checkout", json={"plan_id": "pro", "interval": "monthly"}
    )
    assert response.status_code == 409
    # No second Razorpay subscription was created.
    assert "subscription" not in fake_razorpay

    sub = await db.scalar(select(Subscription).where(Subscription.user_id == user.id))
    assert sub.razorpay_subscription_id == "sub_existing_live"


async def test_checkout_leaves_a_retryable_trial_row_if_razorpay_fails_after_commit(
    db, user, client, monkeypatch
):
    """The trial row is committed before any Razorpay call, so a failure in
    either call must not lose it or leave it half-written: it should come back
    exactly as `get_or_create_subscription` first wrote it (null Razorpay ids),
    ready for `get_or_create_subscription` to hand back unchanged on retry —
    the same shape a backfilled, never-checked-out account already has.
    """
    from sqlalchemy import select

    from services.billing import razorpay_client

    def _boom(*, email, name=None):
        raise RuntimeError("razorpay unreachable")

    monkeypatch.setattr(razorpay_client, "create_customer", _boom)

    with pytest.raises(RuntimeError):
        await client.post(
            "/v1/billing/checkout", json={"plan_id": "pro", "interval": "monthly"}
        )

    sub = await db.scalar(select(Subscription).where(Subscription.user_id == user.id))
    assert sub is not None
    assert sub.razorpay_customer_id is None
    assert sub.razorpay_subscription_id is None
    assert sub.plan_id == PLAN_PRO  # the store's default, untouched by the failed checkout


async def test_cancel_requires_a_subscription(client, fake_razorpay):
    response = await client.post("/v1/billing/cancel")
    assert response.status_code == 409


async def test_cancel_marks_cancel_at_period_end(db, user, client, fake_razorpay):
    from sqlalchemy import select

    db.add(
        Subscription(
            user_id=user.id,
            plan_id=PLAN_PRO,
            interval=INTERVAL_MONTHLY,
            status=STATUS_ACTIVE,
            razorpay_subscription_id="sub_live",
        )
    )
    await db.flush()

    response = await client.post("/v1/billing/cancel")
    assert response.status_code == 200

    sub = await db.scalar(select(Subscription).where(Subscription.user_id == user.id))
    assert sub.cancel_at_period_end is True
    # Cancel at cycle end: they keep what they paid for until the period closes.
    assert fake_razorpay["cancel"]["at_cycle_end"] is True
