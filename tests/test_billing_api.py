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
    STATUS_CREATED,
    STATUS_EXPIRED,
    STATUS_HALTED,
    STATUS_PENDING,
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


async def test_subscription_started_is_false_without_a_row(client):
    """No subscription row at all: the dashboard paywall gate must bounce
    this user rather than let a never-checked-out account through."""
    body = (await client.get("/v1/billing/subscription")).json()
    assert body["subscription_started"] is False


async def test_subscription_started_is_false_for_created(db, user, client):
    """`created` is exactly the paywall bypass this field exists to close:
    the browser opened the Razorpay modal (so `plan_id` got set by
    `start_checkout`) and the user closed it without signing a mandate. No
    card, no authorisation — the dashboard gate must not treat this as
    started."""
    db.add(
        Subscription(
            user_id=user.id,
            plan_id=PLAN_PRO,
            interval=INTERVAL_MONTHLY,
            status=STATUS_CREATED,
        )
    )
    await db.flush()

    body = (await client.get("/v1/billing/subscription")).json()
    assert body["subscription_started"] is False


async def test_subscription_started_is_false_for_expired(db, user, client):
    """`expired` means the subscription's `start_at` passed without the user
    ever authenticating a mandate — authorisation never happened here
    either."""
    db.add(
        Subscription(
            user_id=user.id,
            plan_id=PLAN_PRO,
            interval=INTERVAL_MONTHLY,
            status=STATUS_EXPIRED,
        )
    )
    await db.flush()

    body = (await client.get("/v1/billing/subscription")).json()
    assert body["subscription_started"] is False


async def test_subscription_started_is_true_for_authenticated(db, user, client):
    """`authenticated` is the trial: the mandate is signed. This is the
    minimum bar for having actually started a subscription."""
    db.add(
        Subscription(
            user_id=user.id,
            plan_id=PLAN_PRO,
            interval=INTERVAL_MONTHLY,
            status=STATUS_AUTHENTICATED,
            trial_ends_at=datetime.now(timezone.utc) + timedelta(days=3),
        )
    )
    await db.flush()

    body = (await client.get("/v1/billing/subscription")).json()
    assert body["subscription_started"] is True


async def test_subscription_started_is_true_for_cancelled(db, user, client):
    """The one that proves this isn't just an alias for `access == entitled`:
    a cancelled subscription is locked (see `resolve_access`) but the user
    unquestionably authorised a mandate at some point, so the dashboard's
    paywall gate must still let them through read-only — that's what lets
    `SubscribeBanner`/`TrialPill` render on the dashboard for a churned
    customer instead of making those components unreachable."""
    db.add(
        Subscription(
            user_id=user.id,
            plan_id=PLAN_PRO,
            interval=INTERVAL_MONTHLY,
            status=STATUS_CANCELLED,
        )
    )
    await db.flush()

    body = (await client.get("/v1/billing/subscription")).json()
    assert body["access"] == "locked"
    assert body["subscription_started"] is True


async def test_subscription_started_is_true_for_a_comped_row_never_checked_out(
    db, user, client
):
    """A design partner comped straight onto Pro (see `Subscription.comped`'s
    "needs no Razorpay record and never locks") never authorised a mandate —
    the row can sit at the store's `created` default forever. `comped`
    already overrides every other status for `resolve_access`/
    `effective_plan_id`; the paywall gate must honour the same override
    rather than bouncing this account to `/onboarding/plan`."""
    db.add(
        Subscription(
            user_id=user.id,
            plan_id=PLAN_PRO,
            interval=INTERVAL_MONTHLY,
            status=STATUS_CREATED,
            comped=True,
        )
    )
    await db.flush()

    body = (await client.get("/v1/billing/subscription")).json()
    assert body["subscription_started"] is True


async def test_subscription_endpoint_reports_trial_available_without_a_row(client):
    """No row yet means no trial has ever been granted — the plan picker must
    be able to promise a free trial to a brand-new signup."""
    body = (await client.get("/v1/billing/subscription")).json()
    assert body["trial_available"] is True


async def test_subscription_endpoint_reports_trial_available_once_consumed(
    db, user, client
):
    """Once a trial has run its course, checkout charges immediately (see
    `start_checkout`'s `trial_consumed`-and-elapsed branch) — the plan picker
    must stop promising a free trial to these users."""
    db.add(
        Subscription(
            user_id=user.id,
            plan_id=PLAN_PRO,
            interval=INTERVAL_MONTHLY,
            status=STATUS_CANCELLED,
            trial_consumed=True,
            trial_ends_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
    )
    await db.flush()

    body = (await client.get("/v1/billing/subscription")).json()
    assert body["trial_available"] is False


async def test_subscription_endpoint_reports_trial_available_while_trial_runs(
    db, user, client
):
    """A trial already granted and still running is not "unavailable" — a
    checkout retry during it (e.g. subscribe, cancel before the first charge,
    checkout again) continues the same free trial rather than charging."""
    db.add(
        Subscription(
            user_id=user.id,
            plan_id=PLAN_PRO,
            interval=INTERVAL_MONTHLY,
            status=STATUS_AUTHENTICATED,
            trial_consumed=True,
            trial_ends_at=datetime.now(timezone.utc) + timedelta(days=3),
        )
    )
    await db.flush()

    body = (await client.get("/v1/billing/subscription")).json()
    assert body["trial_available"] is True


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


async def test_cancel_during_trial_cancels_immediately(db, user, client, fake_razorpay):
    """Razorpay rejects cancel_at_cycle_end while the subscription is still
    `authenticated` (trial, no billing cycle yet). Cancel must be immediate."""
    from sqlalchemy import select

    db.add(
        Subscription(
            user_id=user.id,
            plan_id=PLAN_PRO,
            interval=INTERVAL_MONTHLY,
            status=STATUS_AUTHENTICATED,
            razorpay_subscription_id="sub_trial",
            trial_ends_at=datetime.now(timezone.utc) + timedelta(days=5),
            trial_consumed=True,
        )
    )
    await db.flush()

    response = await client.post("/v1/billing/cancel")
    assert response.status_code == 200

    sub = await db.scalar(select(Subscription).where(Subscription.user_id == user.id))
    assert fake_razorpay["cancel"]["at_cycle_end"] is False
    assert sub.cancel_at_period_end is False
    assert sub.status == STATUS_CANCELLED
    assert response.json()["access"] == "locked"


async def test_checkout_after_cancel_clears_cancel_at_period_end(
    db, user, client, fake_razorpay
):
    """Re-subscribe must not carry a stale `cancel_at_period_end` flag onto the
    new subscription — that is what made Settings keep saying "cancels at
    period end" on an actively billing account after a successful
    reactivation. `status` is deliberately not asserted here: the row stays
    locked (whatever status it already had) until the webhook for the new
    Razorpay subscription confirms the mandate — see
    `test_checkout_leaves_status_untouched_for_the_webhook_to_confirm`."""
    from sqlalchemy import select

    db.add(
        Subscription(
            user_id=user.id,
            plan_id=PLAN_PRO,
            interval=INTERVAL_MONTHLY,
            status=STATUS_CANCELLED,
            razorpay_customer_id="cust_existing",
            razorpay_subscription_id="sub_old_cancelled",
            trial_consumed=True,
            trial_ends_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
    )
    await db.flush()

    response = await client.post(
        "/v1/billing/checkout", json={"plan_id": "pro", "interval": "monthly"}
    )
    assert response.status_code == 200

    sub = await db.scalar(select(Subscription).where(Subscription.user_id == user.id))
    assert sub.razorpay_subscription_id == "sub_test123"
    assert sub.cancel_at_period_end is False


async def test_checkout_does_not_lock_a_first_time_subscriber(
    db, user, client, fake_razorpay
):
    """`get_or_create_subscription` grants the trial — and with it, entitled
    access — the moment a first-time subscriber's row is created, before any
    Razorpay call runs. Checkout must not then stomp that back down to
    Razorpay's `create_subscription` response, which is always
    `status: "created"` (mandate not yet signed) regardless of whether this
    call ever reaches the modal. Overwriting `sub.status` with it locked out
    every brand-new subscriber the instant they picked a plan — this is the
    blocker the fix removes."""
    from sqlalchemy import select

    response = await client.post(
        "/v1/billing/checkout", json={"plan_id": "pro", "interval": "monthly"}
    )
    assert response.status_code == 200

    sub = await db.scalar(select(Subscription).where(Subscription.user_id == user.id))
    # Set by `get_or_create_subscription` on row creation and left alone —
    # proof checkout did not overwrite it with Razorpay's "created" response.
    assert sub.status == "authenticated"
    assert sub.razorpay_subscription_id == "sub_test123"
    sub_body = (await client.get("/v1/billing/subscription")).json()
    assert sub_body["access"] == "entitled"


async def test_checkout_leaves_an_existing_non_entitled_status_for_the_webhook(
    db, user, client, fake_razorpay
):
    """Re-checkout over a row that already exists (e.g. `cancelled`, from a
    prior subscription) must not mirror Razorpay's `created` response onto
    `status` either. Unlike the first-checkout case, there is no freshly
    granted trial to protect here — the row simply stays locked, exactly as
    it already was, until the new subscription's authenticated webhook
    arrives and confirms the mandate."""
    from sqlalchemy import select

    db.add(
        Subscription(
            user_id=user.id,
            plan_id=PLAN_PRO,
            interval=INTERVAL_MONTHLY,
            status=STATUS_CANCELLED,
            razorpay_customer_id="cust_existing",
            razorpay_subscription_id="sub_old_cancelled",
            trial_consumed=True,
            trial_ends_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
    )
    await db.flush()

    response = await client.post(
        "/v1/billing/checkout", json={"plan_id": "pro", "interval": "monthly"}
    )
    assert response.status_code == 200

    sub = await db.scalar(select(Subscription).where(Subscription.user_id == user.id))
    # Untouched by checkout — still whatever it was before, not Razorpay's
    # transient "created" response, and not silently flipped to "cancelled"
    # meaning something new either.
    assert sub.status == STATUS_CANCELLED
    assert sub.razorpay_subscription_id == "sub_test123"
    sub_body = (await client.get("/v1/billing/subscription")).json()
    assert sub_body["access"] == "locked"


async def test_abandoned_checkout_can_be_retried_instead_of_409ing(
    db, user, client, fake_razorpay
):
    """A user who opens the Razorpay modal and closes it without authorising
    is left with a subscription row pinned at `created` — no card, no signed
    mandate, nothing running at Razorpay to orphan. Before `created` was
    added to `_TERMINAL_SUBSCRIPTION_STATUSES`, retrying checkout from that
    state hit the "already have a subscription" 409 forever, since no webhook
    was ever coming to move it off `created`. It must now be retryable, the
    same as `cancelled`/`expired`/`completed`."""
    from sqlalchemy import select

    db.add(
        Subscription(
            user_id=user.id,
            plan_id=PLAN_PRO,
            interval=INTERVAL_MONTHLY,
            status=STATUS_CREATED,
            razorpay_customer_id="cust_existing",
            razorpay_subscription_id="sub_abandoned",
            trial_consumed=True,
            trial_ends_at=datetime.now(timezone.utc) + timedelta(days=6),
        )
    )
    await db.flush()

    response = await client.post(
        "/v1/billing/checkout", json={"plan_id": "pro", "interval": "monthly"}
    )
    assert response.status_code == 200
    assert fake_razorpay["subscription"]["customer_id"] == "cust_existing"

    sub = await db.scalar(select(Subscription).where(Subscription.user_id == user.id))
    # The retry's subscription id replaced the abandoned one — there is
    # nothing live at Razorpay under the old id to orphan.
    assert sub.razorpay_subscription_id == "sub_test123"


# --- Checkout must key off `resolve_access`, not a hand-maintained status
# list, so a locked subscription never becomes a permanent dead end. See
# `access.py::resolve_access` and `start_checkout`'s guard. ---


async def test_checkout_permitted_over_an_authenticated_row_past_its_trial(
    db, user, client, fake_razorpay
):
    """This is the reported bug: a backfilled or webhook-less account sits in
    `authenticated` forever once its trial elapses. `resolve_access` already
    treats that as locked, but the old guard only consulted a status list
    that didn't include `authenticated` — so this row could never check out
    again despite serving the user nothing. The stale Razorpay subscription
    must be cancelled (immediately, not at cycle end — it isn't serving
    them) before the new one is created."""
    from sqlalchemy import select

    db.add(
        Subscription(
            user_id=user.id,
            plan_id=PLAN_PRO,
            interval=INTERVAL_MONTHLY,
            status=STATUS_AUTHENTICATED,
            razorpay_customer_id="cust_existing",
            razorpay_subscription_id="sub_stale_authenticated",
            trial_consumed=True,
            trial_ends_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
    )
    await db.flush()

    response = await client.post(
        "/v1/billing/checkout", json={"plan_id": "pro", "interval": "monthly"}
    )
    assert response.status_code == 200

    assert fake_razorpay["cancel"]["subscription_id"] == "sub_stale_authenticated"
    assert fake_razorpay["cancel"]["at_cycle_end"] is False

    sub = await db.scalar(select(Subscription).where(Subscription.user_id == user.id))
    assert sub.razorpay_subscription_id == "sub_test123"


async def test_checkout_permitted_over_a_halted_subscription(db, user, client, fake_razorpay):
    """`halted` (retries exhausted) is the other status `resolve_access` locks
    that isn't Razorpay-terminal — same dead end as `authenticated` past its
    trial, same fix."""
    from sqlalchemy import select

    db.add(
        Subscription(
            user_id=user.id,
            plan_id=PLAN_PRO,
            interval=INTERVAL_MONTHLY,
            status=STATUS_HALTED,
            razorpay_customer_id="cust_existing",
            razorpay_subscription_id="sub_halted",
            trial_consumed=True,
            trial_ends_at=datetime.now(timezone.utc) - timedelta(days=30),
        )
    )
    await db.flush()

    response = await client.post(
        "/v1/billing/checkout", json={"plan_id": "pro", "interval": "monthly"}
    )
    assert response.status_code == 200

    assert fake_razorpay["cancel"]["subscription_id"] == "sub_halted"

    sub = await db.scalar(select(Subscription).where(Subscription.user_id == user.id))
    assert sub.razorpay_subscription_id == "sub_test123"


async def test_checkout_still_rejects_a_pending_subscription(db, user, client, fake_razorpay):
    """`pending` is Razorpay still retrying the card — genuinely entitled (see
    `ENTITLED_STATUSES`), so this must still 409 rather than orphan a
    mandate that may yet recover."""
    from sqlalchemy import select

    db.add(
        Subscription(
            user_id=user.id,
            plan_id=PLAN_PRO,
            interval=INTERVAL_MONTHLY,
            status=STATUS_PENDING,
            razorpay_subscription_id="sub_pending",
        )
    )
    await db.flush()

    response = await client.post(
        "/v1/billing/checkout", json={"plan_id": "pro", "interval": "monthly"}
    )
    assert response.status_code == 409
    assert "cancel" not in fake_razorpay
    assert "subscription" not in fake_razorpay

    sub = await db.scalar(select(Subscription).where(Subscription.user_id == user.id))
    assert sub.razorpay_subscription_id == "sub_pending"


async def test_checkout_still_rejects_authenticated_with_a_trial_still_running(
    db, user, client, fake_razorpay
):
    """The double-click / back-button-resubmit protection the guard exists
    for: a signed mandate, trial still running, is genuinely entitled and
    must still 409 rather than silently cancel a live mandate."""
    from sqlalchemy import select

    db.add(
        Subscription(
            user_id=user.id,
            plan_id=PLAN_PRO,
            interval=INTERVAL_MONTHLY,
            status=STATUS_AUTHENTICATED,
            razorpay_subscription_id="sub_live_trial",
            trial_consumed=True,
            trial_ends_at=datetime.now(timezone.utc) + timedelta(days=3),
        )
    )
    await db.flush()

    response = await client.post(
        "/v1/billing/checkout", json={"plan_id": "pro", "interval": "monthly"}
    )
    assert response.status_code == 409
    assert "cancel" not in fake_razorpay
    assert "subscription" not in fake_razorpay

    sub = await db.scalar(select(Subscription).where(Subscription.user_id == user.id))
    assert sub.razorpay_subscription_id == "sub_live_trial"


async def test_checkout_succeeds_even_when_cancelling_the_stale_subscription_fails(
    db, user, client, fake_razorpay, monkeypatch
):
    """Cancelling the old subscription is best-effort: a `halted` or
    already-dead subscription can legitimately error on cancel at Razorpay,
    and that must never block the user from subscribing again — that would
    recreate the exact dead end this fix closes."""
    from sqlalchemy import select

    from services.billing import razorpay_client

    def _boom(*, subscription_id, at_cycle_end=True):
        raise RuntimeError("razorpay: subscription already halted, cannot cancel")

    monkeypatch.setattr(razorpay_client, "cancel_subscription", _boom)

    db.add(
        Subscription(
            user_id=user.id,
            plan_id=PLAN_PRO,
            interval=INTERVAL_MONTHLY,
            status=STATUS_HALTED,
            razorpay_customer_id="cust_existing",
            razorpay_subscription_id="sub_dead",
            trial_consumed=True,
            trial_ends_at=datetime.now(timezone.utc) - timedelta(days=30),
        )
    )
    await db.flush()

    response = await client.post(
        "/v1/billing/checkout", json={"plan_id": "pro", "interval": "monthly"}
    )
    assert response.status_code == 200

    sub = await db.scalar(select(Subscription).where(Subscription.user_id == user.id))
    assert sub.razorpay_subscription_id == "sub_test123"
