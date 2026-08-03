"""Billing endpoints.

Razorpay has no hosted Billing Portal, so unlike a Stripe integration there is
nowhere to send users for self-service. v1 therefore implements cancellation
itself — the one action users must be able to take without contacting anyone —
and leaves card updates and plan changes to support.
"""

from datetime import datetime, timedelta, timezone
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
    now = datetime.now(timezone.utc)
    sub = await get_subscription(db, user.id)
    counter = await get_or_create_counter(db, user.id, now)
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


@router.post("/checkout", response_model=CheckoutOut)
async def start_checkout(payload: CheckoutIn, user: CurrentUser, db: Db) -> CheckoutOut:
    """Create the subscription the browser's Razorpay modal will authorise.

    No redirect URL is returned because Razorpay Checkout is a JS modal, not a
    hosted page — the client opens it against `subscription_id`.
    """
    now = datetime.now(timezone.utc)
    sub = await get_or_create_subscription(db, user.id, trial_days=settings.TRIAL_DAYS)

    if not sub.razorpay_customer_id:
        sub.razorpay_customer_id = razorpay_client.create_customer(
            email=user.email, name=user.full_name
        )

    trial_ends_at = now + timedelta(days=settings.TRIAL_DAYS)
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

    razorpay_client.cancel_subscription(
        subscription_id=sub.razorpay_subscription_id, at_cycle_end=True
    )
    # The webhook will move `status` when Razorpay actually ends it. Recording
    # the intent now means the UI reflects the cancellation immediately instead
    # of looking like the click did nothing.
    sub.cancel_at_period_end = True
    await db.flush()

    return await current_subscription(user, db)
