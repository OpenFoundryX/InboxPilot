from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient

from core.database import get_db
from core.plans import INTERVAL_MONTHLY, PLAN_PRO
from main import app
from models.billing import STATUS_ACTIVE, STATUS_AUTHENTICATED, Subscription
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
