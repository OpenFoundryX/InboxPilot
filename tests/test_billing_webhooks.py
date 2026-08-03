import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from core.config import settings
from core.database import get_db
from core.plans import INTERVAL_MONTHLY, PLAN_PRO
from main import app
from models.billing import (
    STATUS_ACTIVE,
    STATUS_AUTHENTICATED,
    STATUS_CANCELLED,
    STATUS_HALTED,
    STATUS_PENDING,
    Subscription,
)
from services.billing.webhooks import handle_event, verify_signature

SEP_1 = int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())
AUG_1 = int(datetime(2026, 8, 1, tzinfo=timezone.utc).timestamp())

ROUTE_SECRET = "whsec_route_test"


def _event(event_type, *, status="active", end=SEP_1, sub_id="sub_test", user_id=None):
    return {
        "event": event_type,
        "payload": {
            "subscription": {
                "entity": {
                    "id": sub_id,
                    "status": status,
                    "current_end": end,
                    "customer_id": "cust_test",
                    "notes": {"user_id": str(user_id)} if user_id else {},
                }
            }
        },
    }


@pytest.fixture
async def client(db, monkeypatch):
    """A real ASGI client against `/v1/webhooks/razorpay`.

    `verify_signature` is proven correct as a pure function above, but the raw-
    body requirement is a property of the *route* — whether it hands
    `verify_signature` the untouched bytes it read off the wire, before any
    parsing. Only a request that actually goes through FastAPI's body-reading
    machinery can prove that; a unit test calling the function directly cannot.
    """
    app.dependency_overrides[get_db] = lambda: db
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", ROUTE_SECRET)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


def _sign(body: bytes, secret: str = ROUTE_SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _headers(signature: str) -> dict:
    return {
        "x-razorpay-signature": signature,
        "x-razorpay-event-id": f"evt_{uuid.uuid4()}",
        "content-type": "application/json",
    }


def test_signature_accepts_a_correct_digest():
    body = b'{"event":"subscription.activated"}'
    secret = "whsec_test"
    good = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert verify_signature(body, good, secret) is True


def test_signature_rejects_a_wrong_digest():
    assert verify_signature(b'{"a":1}', "deadbeef", "whsec_test") is False


def test_signature_is_computed_over_raw_bytes_not_reparsed_json():
    """Re-serialising the body changes the bytes and must not still verify."""
    secret = "whsec_test"
    raw = b'{"event":"subscription.activated",  "spaced": true}'
    digest = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    reserialised = json.dumps(json.loads(raw)).encode()
    assert verify_signature(raw, digest, secret) is True
    assert verify_signature(reserialised, digest, secret) is False


async def test_authenticated_links_the_subscription_by_notes(db, user):
    result = await handle_event(
        db, _event("subscription.authenticated", status="authenticated", user_id=user.id)
    )
    assert result == "applied"

    sub = await db.scalar(select(Subscription).where(Subscription.user_id == user.id))
    assert sub.razorpay_subscription_id == "sub_test"
    assert sub.status == STATUS_AUTHENTICATED


async def test_activated_moves_an_existing_row_to_active(db, user):
    db.add(
        Subscription(
            user_id=user.id,
            plan_id=PLAN_PRO,
            interval=INTERVAL_MONTHLY,
            status=STATUS_AUTHENTICATED,
            razorpay_subscription_id="sub_test",
        )
    )
    await db.flush()

    await handle_event(db, _event("subscription.activated", status="active"))

    sub = await db.scalar(select(Subscription).where(Subscription.user_id == user.id))
    assert sub.status == STATUS_ACTIVE
    assert sub.current_period_end.timestamp() == SEP_1


async def test_out_of_order_event_does_not_rewind_the_period(db, user):
    await handle_event(
        db, _event("subscription.authenticated", status="authenticated", user_id=user.id)
    )
    await handle_event(db, _event("subscription.charged", end=SEP_1))

    assert await handle_event(db, _event("subscription.charged", end=AUG_1)) == "stale"

    sub = await db.scalar(select(Subscription).where(Subscription.user_id == user.id))
    assert sub.current_period_end.timestamp() == SEP_1


async def test_replaying_the_same_event_is_harmless(db, user):
    await handle_event(
        db, _event("subscription.authenticated", status="authenticated", user_id=user.id)
    )
    await handle_event(db, _event("subscription.activated"))
    await handle_event(db, _event("subscription.activated"))

    rows = (await db.scalars(select(Subscription).where(Subscription.user_id == user.id))).all()
    assert len(rows) == 1
    assert rows[0].status == STATUS_ACTIVE


async def test_pending_marks_payment_trouble(db, user):
    await handle_event(
        db, _event("subscription.authenticated", status="authenticated", user_id=user.id)
    )
    await handle_event(db, _event("subscription.pending", status="pending"))

    sub = await db.scalar(select(Subscription).where(Subscription.user_id == user.id))
    assert sub.status == STATUS_PENDING


async def test_halted_locks_the_account(db, user):
    await handle_event(
        db, _event("subscription.authenticated", status="authenticated", user_id=user.id)
    )
    await handle_event(db, _event("subscription.halted", status="halted"))

    sub = await db.scalar(select(Subscription).where(Subscription.user_id == user.id))
    assert sub.status == STATUS_HALTED


async def test_cancelled_locks_the_account(db, user):
    await handle_event(
        db, _event("subscription.authenticated", status="authenticated", user_id=user.id)
    )
    await handle_event(db, _event("subscription.cancelled", status="cancelled"))

    sub = await db.scalar(select(Subscription).where(Subscription.user_id == user.id))
    assert sub.status == STATUS_CANCELLED


async def test_unrelated_events_are_ignored(db, user):
    assert await handle_event(db, {"event": "payment.captured", "payload": {}}) == "ignored"


async def test_events_for_unknown_subscriptions_are_ignored(db, user):
    assert await handle_event(db, _event("subscription.charged")) == "ignored"


# --- Route-level tests -------------------------------------------------------
#
# Everything above calls `handle_event`/`verify_signature` directly. That
# proves the functions are correct but not that the route feeds them the right
# bytes — a refactor that swapped `request.body()` for `request.json()` and
# re-dumped it would pass every test above while reintroducing exactly the
# failure the brief warns about. These go through the real ASGI route instead.


async def test_route_applies_a_correctly_signed_event(db, user, client):
    db.add(
        Subscription(
            user_id=user.id,
            plan_id=PLAN_PRO,
            interval=INTERVAL_MONTHLY,
            status=STATUS_AUTHENTICATED,
            razorpay_subscription_id="sub_test",
        )
    )
    await db.flush()

    body = json.dumps(_event("subscription.activated", status="active")).encode()
    response = await client.post(
        "/v1/webhooks/razorpay", content=body, headers=_headers(_sign(body))
    )

    assert response.status_code == 200
    assert response.json()["status"] == "applied"

    sub = await db.scalar(select(Subscription).where(Subscription.user_id == user.id))
    assert sub.status == STATUS_ACTIVE


async def test_route_rejects_a_wrong_signature_and_writes_nothing(db, user, client):
    original_end = datetime(2026, 6, 1, tzinfo=timezone.utc)
    db.add(
        Subscription(
            user_id=user.id,
            plan_id=PLAN_PRO,
            interval=INTERVAL_MONTHLY,
            status=STATUS_AUTHENTICATED,
            razorpay_subscription_id="sub_test",
            current_period_end=original_end,
        )
    )
    await db.flush()

    body = json.dumps(_event("subscription.activated", status="active")).encode()
    response = await client.post(
        "/v1/webhooks/razorpay", content=body, headers=_headers("deadbeef" * 8)
    )

    assert response.status_code == 400

    sub = await db.scalar(select(Subscription).where(Subscription.user_id == user.id))
    assert sub.status == STATUS_AUTHENTICATED
    assert sub.current_period_end == original_end


async def test_route_rejects_a_missing_signature_header(db, user, client):
    db.add(
        Subscription(
            user_id=user.id,
            plan_id=PLAN_PRO,
            interval=INTERVAL_MONTHLY,
            status=STATUS_AUTHENTICATED,
            razorpay_subscription_id="sub_test",
        )
    )
    await db.flush()

    body = json.dumps(_event("subscription.activated", status="active")).encode()
    response = await client.post(
        "/v1/webhooks/razorpay",
        content=body,
        headers={"content-type": "application/json", "x-razorpay-event-id": "evt_no_sig"},
    )

    assert response.status_code == 400

    sub = await db.scalar(select(Subscription).where(Subscription.user_id == user.id))
    assert sub.status == STATUS_AUTHENTICATED


async def test_route_verifies_the_raw_bytes_not_a_reserialised_form(db, user, client):
    """The single most important property from the brief, proven at the ASGI
    layer: a signature computed over a *reserialised* form of the same JSON
    object must not verify against the differently-formatted raw bytes the
    route actually receives on the wire.
    """
    db.add(
        Subscription(
            user_id=user.id,
            plan_id=PLAN_PRO,
            interval=INTERVAL_MONTHLY,
            status=STATUS_AUTHENTICATED,
            razorpay_subscription_id="sub_test",
        )
    )
    await db.flush()

    payload = _event("subscription.activated", status="active")
    raw = json.dumps(payload, indent=2).encode()  # what the route actually receives
    reserialised_signature = _sign(json.dumps(payload).encode())  # compact re-dump, signed

    response = await client.post(
        "/v1/webhooks/razorpay", content=raw, headers=_headers(reserialised_signature)
    )

    assert response.status_code == 400

    sub = await db.scalar(select(Subscription).where(Subscription.user_id == user.id))
    assert sub.status == STATUS_AUTHENTICATED
