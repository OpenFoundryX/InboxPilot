"""Billing endpoints.

Razorpay has no hosted Billing Portal, so unlike a Stripe integration there is
nowhere to send users for self-service. v1 therefore implements cancellation
itself — the one action users must be able to take without contacting anyone —
and leaves card updates and plan changes to support.
"""

import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.database import get_db
from core.plans import (
    CURRENCY,
    INTERVAL_ANNUAL,
    PLANS,
    get_plan,
    razorpay_plan_id_for,
)
from models.billing import (
    STATUS_ACTIVE,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_EXPIRED,
    STATUS_PENDING,
    Subscription,
)
from models.users import User
from schemas.billing import (
    CheckoutIn,
    CheckoutOut,
    PlanOut,
    PlansOut,
    SubscriptionOut,
    UsageOut,
)
from services.auth.dependencies import get_current_user
from services.billing import razorpay_client
from services.billing.access import effective_plan_id, resolve_access
from services.billing.store import get_or_create_subscription, get_subscription
from services.billing.usage import get_or_create_counter

router = APIRouter(prefix="/billing", tags=["billing"])

CurrentUser = Annotated[User, Depends(get_current_user)]
Db = Annotated[AsyncSession, Depends(get_db)]

# Razorpay requires a cycle count rather than "until cancelled". Ten years of
# billing cycles is effectively unbounded for our purposes and keeps the
# subscription from silently completing while the customer is still paying.
MONTHLY_CYCLES = 120
ANNUAL_CYCLES = 10

# Statuses in which Razorpay's own record of the subscription is already
# finished. Starting a fresh checkout from one of these cannot orphan a live
# mandate — there is nothing left running to abandon. Every other status
# (including `created`, `halted`, `paused`) is treated as potentially still
# live: `/checkout` refuses to run again once `razorpay_subscription_id` is
# set and the row isn't in one of these, rather than risk creating a second
# subscription that keeps billing with nothing pointing at it. See
# `test_checkout_rejects_when_a_live_subscription_already_exists`.
_TERMINAL_SUBSCRIPTION_STATUSES = frozenset({STATUS_CANCELLED, STATUS_EXPIRED, STATUS_COMPLETED})


async def _subscription_out(
    sub: Subscription | None, user_id: uuid.UUID, db: AsyncSession
) -> SubscriptionOut:
    """Build the response body from a subscription row already in hand.

    Callers that already hold `sub` (e.g. `cancel`, right after mutating it)
    should use this instead of re-fetching through `current_subscription` —
    that would re-run `get_subscription` for a row already sitting in memory.
    """
    now = datetime.now(timezone.utc)
    counter = await get_or_create_counter(db, user_id, now)
    entitlements = get_plan(effective_plan_id(sub)).entitlements

    return SubscriptionOut(
        access=resolve_access(sub, now),
        plan_id=sub.plan_id if sub else None,
        interval=sub.interval if sub else None,
        status=sub.status if sub else None,
        trial_ends_at=sub.trial_ends_at if sub else None,
        current_period_end=sub.current_period_end if sub else None,
        cancel_at_period_end=sub.cancel_at_period_end if sub else False,
        comped=sub.comped if sub else False,
        has_payment_method=bool(sub and sub.razorpay_customer_id),
        usage=UsageOut(
            bot_hours_used=round(counter.bot_seconds_used / 3600, 2),
            bot_hours_included=entitlements.bot_hours_per_month,
            drafts_used=counter.drafts_generated,
            drafts_included=entitlements.drafts_per_month,
        ),
    )


@router.get("/plans", response_model=PlansOut)
async def list_plans() -> PlansOut:
    return PlansOut(
        plans=[
            PlanOut(
                id=plan.id,
                name=plan.name,
                currency=CURRENCY,
                monthly_price_cents=plan.monthly_price_cents,
                annual_price_cents=plan.annual_price_cents,
                bot_hours_per_month=plan.entitlements.bot_hours_per_month,
                drafts_per_month=plan.entitlements.drafts_per_month,
                custom_categories=plan.entitlements.custom_categories,
                video_retention_days=plan.entitlements.video_retention_days,
                transcript_retention_days=plan.entitlements.transcript_retention_days,
            )
            for plan in PLANS.values()
        ],
        trial_days=settings.TRIAL_DAYS,
    )


@router.get("/subscription", response_model=SubscriptionOut)
async def current_subscription(user: CurrentUser, db: Db) -> SubscriptionOut:
    sub = await get_subscription(db, user.id)
    return await _subscription_out(sub, user.id, db)


@router.post("/checkout", response_model=CheckoutOut)
async def start_checkout(payload: CheckoutIn, user: CurrentUser, db: Db) -> CheckoutOut:
    """Create the subscription the browser's Razorpay modal will authorise.

    No redirect URL is returned because Razorpay Checkout is a JS modal, not a
    hosted page — the client opens it against `subscription_id`.
    """
    # Captured before `get_or_create_subscription` can create a row, so it's
    # the one place that can tell "this call just created the trial row" from
    # "a row already existed" — the two `trial_ends_at` outcomes below need to
    # know exactly that. `existing_before` and whatever `get_or_create_
    # subscription` hands back are the same ORM-identity row when one already
    # existed, so reading `.trial_consumed`/`.trial_ends_at` off either after
    # the call agrees; the only thing worth capturing early is the None-ness.
    existing_before = await get_subscription(db, user.id)
    sub = await get_or_create_subscription(db, user.id, trial_days=settings.TRIAL_DAYS)

    # Refuse to run checkout again over a subscription that already has a
    # Razorpay record which isn't finished. Without this, a double-click, a
    # back-button resubmit, or a retry after a slow response calls
    # `create_subscription` a second time and overwrites
    # `razorpay_subscription_id` — silently orphaning the first subscription,
    # which keeps billing with no row in our database pointing at it anymore.
    # Rejecting outright (rather than quietly returning the existing
    # subscription) also keeps this from becoming an undocumented plan-change
    # path, which the brief explicitly puts out of v1 scope.
    if sub.razorpay_subscription_id and sub.status not in _TERMINAL_SUBSCRIPTION_STATUSES:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "You already have a subscription. Cancel it before starting a new one.",
        )

    # Commit the trial row (and the guard's read of it) before making any
    # Razorpay calls. `get_or_create_subscription` only flushes, and
    # `get_db` commits solely after this handler returns — so without this,
    # the freshly-inserted row stays uncommitted for the full round-trip time
    # of the two Razorpay calls below. A concurrent duplicate request for the
    # same first-time user would then pass the "no existing row" read before
    # either side commits, and both would call Razorpay before the database
    # ever rejects the second INSERT — leaving the loser's Razorpay customer
    # and subscription created but referenced by no row (see
    # `services/billing/store.py`'s `get_or_create_subscription`). Committing
    # here shrinks the race back to the plain read-then-insert window Task 4
    # already accepted, and stops holding the connection open for the
    # duration of two external HTTP calls.
    await db.commit()

    now = datetime.now(timezone.utc)
    if not sub.razorpay_customer_id:
        sub.razorpay_customer_id = razorpay_client.create_customer(
            email=user.email, name=user.full_name
        )

    if existing_before is None:
        # First-ever checkout for this user: `get_or_create_subscription` just
        # created the row above and already computed the correct trial window
        # (and marked `trial_consumed`) — trust it rather than recomputing a
        # second `now + TRIAL_DAYS` here, which would just be a second,
        # slightly later "now".
        # `get_or_create_subscription` always writes trial_ends_at on insert.
        trial_ends_at = sub.trial_ends_at or now
    elif sub.trial_consumed and sub.trial_ends_at and sub.trial_ends_at > now:
        # A trial was already granted to this user — by a prior checkout, or
        # by the billing backfill migration for a pre-existing account — and
        # it is still running. Continue it exactly as already promised rather
        # than restarting it: this is the fix for both "subscribe, cancel
        # before the first charge, checkout again" (which must not mint a
        # fresh 7 days every cycle) and a backfilled user mid-trial running
        # their first real checkout (which must not become 10 days total by
        # adding a new 7 on top of the days already elapsed).
        trial_ends_at = sub.trial_ends_at
    else:
        # Trial already consumed and, if it ever ran, has elapsed. Trials are
        # once per customer: this subscription starts charging immediately.
        trial_ends_at = now

    created = razorpay_client.create_subscription(
        plan_id=razorpay_plan_id_for(payload.plan_id, payload.interval),
        customer_id=sub.razorpay_customer_id,
        start_at=int(trial_ends_at.timestamp()),
        total_count=ANNUAL_CYCLES if payload.interval == INTERVAL_ANNUAL else MONTHLY_CYCLES,
        notes={"user_id": str(user.id)},
    )

    sub.razorpay_subscription_id = created["id"]
    sub.plan_id = payload.plan_id
    sub.interval = payload.interval
    sub.currency = CURRENCY
    sub.trial_ends_at = trial_ends_at
    sub.trial_consumed = True
    # A prior cancellation must not survive into a new subscription — without
    # this, a customer who cancels and later re-subscribes carries a
    # permanent "cancels at period end" label on an actively billing account
    # (see `SubscribeBanner`/Settings, which both read this flag verbatim).
    # The Razorpay webhook path doesn't need a parallel reset: this is the
    # only writer of the *new* subscription's row before any webhook for it
    # can arrive, so there is nothing left for the webhook to clear.
    sub.cancel_at_period_end = False
    await db.flush()

    plan = get_plan(payload.plan_id)
    amount = (
        plan.annual_price_cents
        if payload.interval == INTERVAL_ANNUAL
        else plan.monthly_price_cents
    )
    return CheckoutOut(
        subscription_id=created["id"],
        key_id=settings.RAZORPAY_KEY_ID,
        plan_id=payload.plan_id,
        interval=payload.interval,
        currency=CURRENCY,
        amount_cents=amount,
    )


@router.post("/cancel", response_model=SubscriptionOut)
async def cancel(user: CurrentUser, db: Db) -> SubscriptionOut:
    sub = await get_subscription(db, user.id)
    if not sub or not sub.razorpay_subscription_id:
        raise HTTPException(status.HTTP_409_CONFLICT, "No active subscription to cancel.")

    # `cancel_at_cycle_end` only works once a billing cycle is running
    # (`active` / `pending`). During the trial (`authenticated`) — or before
    # the mandate is signed (`created`) — Razorpay rejects cycle-end cancel
    # with "Subscription cannot be cancelled since no billing cycle is going
    # on". Immediate cancel is the right semantics there anyway: nothing has
    # been charged yet, so there is no prepaid period to honour.
    at_cycle_end = sub.status in {STATUS_ACTIVE, STATUS_PENDING}
    razorpay_client.cancel_subscription(
        subscription_id=sub.razorpay_subscription_id, at_cycle_end=at_cycle_end
    )
    # Cycle-end: keep access until the period closes; the webhook flips
    # `status` later. Immediate (trial): mark cancelled locally now so the UI
    # doesn't keep looking entitled while we wait for the webhook.
    if at_cycle_end:
        sub.cancel_at_period_end = True
    else:
        sub.cancel_at_period_end = False
        sub.status = STATUS_CANCELLED
    await db.flush()

    return await _subscription_out(sub, user.id, db)
