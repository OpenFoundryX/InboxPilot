"""Apply Razorpay subscription events to our mirror of the subscription state.

Razorpay redelivers events and does not guarantee ordering. Two properties keep
that from corrupting state: every handler is idempotent, and an event carrying
an older `current_end` than the row already holds is discarded, so a late
redelivery cannot resurrect a period that has already advanced.

Events are matched to a row by `razorpay_subscription_id`. The very first event
for a new subscriber may arrive before that id is stored — in practice this is
always `subscription.authenticated`, since it fires first, but any handled
event that misses the id lookup falls back the same way: to the `user_id` we
set in the subscription's `notes` at creation time.
"""

import hashlib
import hmac
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.logging import get_logger
from models.billing import Subscription
from models.users import User

log = get_logger(__name__)

APPLIED = "applied"
IGNORED = "ignored"
STALE = "stale"

# Every event whose payload we apply. Anything else is ignored outright.
HANDLED_EVENTS = frozenset(
    {
        "subscription.authenticated",
        "subscription.activated",
        "subscription.charged",
        "subscription.pending",
        "subscription.halted",
        "subscription.paused",
        "subscription.resumed",
        "subscription.cancelled",
        "subscription.completed",
        "subscription.updated",
    }
)


def verify_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    """Whether `signature` is Razorpay's HMAC-SHA256 of these exact bytes.

    `raw_body` must be the untouched request body. Parsing the JSON and
    re-serialising it changes the byte sequence and every check would fail.
    """
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _ts(value: int | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc)


async def handle_event(db: AsyncSession, event: dict) -> str:
    """Apply one Razorpay event, returning `"applied"`, `"ignored"`, or `"stale"`.

    Every handled event shares the same shape: find the row (by id, falling
    back to the `notes.user_id` linkage on the first event), refuse to move
    `current_period_end` backwards, then copy status/period/customer straight
    across. Razorpay's event types differ only in which fields they carry —
    there is no per-event branching to simulate.
    """
    event_type = event.get("event")
    if event_type not in HANDLED_EVENTS:
        return IGNORED

    entity = event.get("payload", {}).get("subscription", {}).get("entity", {})
    subscription_id = entity.get("id")
    if not subscription_id:
        return IGNORED

    sub = await db.scalar(
        select(Subscription).where(Subscription.razorpay_subscription_id == subscription_id)
    )

    if sub is None:
        sub = await _link_by_notes(db, entity, subscription_id)
        if sub is None:
            return IGNORED

    period_end = _ts(entity.get("current_end"))
    if (
        period_end is not None
        and sub.current_period_end is not None
        and period_end < sub.current_period_end
    ):
        # A redelivery from before the period rolled. Applying it would move the
        # account backwards in time.
        return STALE

    status = entity.get("status")
    if status:
        sub.status = status
    if period_end is not None:
        sub.current_period_end = period_end
    if entity.get("customer_id"):
        sub.razorpay_customer_id = entity["customer_id"]

    await db.flush()
    return APPLIED


async def _link_by_notes(
    db: AsyncSession, entity: dict, subscription_id: str
) -> Subscription | None:
    """Attach a subscription id to the row named in the subscription's notes.

    Checkout writes `user_id` into the subscription's notes precisely so the
    first webhook can find its row even if it arrives before the id was stored.
    """
    raw_user_id = entity.get("notes", {}).get("user_id")
    if not raw_user_id:
        return None
    try:
        user_id = uuid.UUID(raw_user_id)
    except ValueError:
        log.warning("billing.bad_notes_user_id", value=raw_user_id)
        return None

    # A deleted / wiped test user leaves live Razorpay subscriptions behind;
    # their webhooks still carry the old notes.user_id. Creating a row for
    # them hits `subscriptions_user_id_fkey` and 500s — which Razorpay then
    # retries forever. Ignore cleanly instead.
    user = await db.get(User, user_id)
    if user is None:
        log.info(
            "billing.webhook_unknown_user",
            user_id=str(user_id),
            razorpay_subscription_id=subscription_id,
        )
        return None

    sub = await db.scalar(select(Subscription).where(Subscription.user_id == user_id))
    if sub is None:
        sub = Subscription(user_id=user_id)
        db.add(sub)
    sub.razorpay_subscription_id = subscription_id
    return sub
