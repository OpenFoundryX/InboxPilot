from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class PlanOut(BaseModel):
    id: str
    name: str
    currency: str
    monthly_price_cents: int
    annual_price_cents: int
    bot_hours_per_month: int
    drafts_per_month: int | None
    custom_categories: bool
    video_retention_days: int
    transcript_retention_days: int


class PlansOut(BaseModel):
    plans: list[PlanOut]
    trial_days: int


class UsageOut(BaseModel):
    bot_hours_used: float
    bot_hours_included: int
    drafts_used: int
    drafts_included: int | None


class SubscriptionOut(BaseModel):
    access: Literal["entitled", "locked"]
    plan_id: str | None
    interval: str | None
    status: str | None
    trial_ends_at: datetime | None
    current_period_end: datetime | None
    cancel_at_period_end: bool
    comped: bool
    has_payment_method: bool
    # Whether checking out *right now* would grant a free trial rather than
    # charge immediately — i.e. whether `start_checkout`'s trial branch (no
    # row yet, or a trial already running) applies instead of its
    # trial-consumed-and-elapsed branch. Named for what the user gets, not
    # for the `trial_consumed` column it's derived from: the plan picker
    # needs an answer to "will I be charged today", and `trial_consumed`
    # alone doesn't answer that (a still-running trial has it set to `True`).
    trial_available: bool
    usage: UsageOut


class CheckoutIn(BaseModel):
    # Literals rather than free strings: an unknown tier is a 422 from the
    # schema, so no handler has to defend against "team" arriving here.
    #
    # mypy (per PEP 586) rejects variable references inside `Literal[...]`
    # even when the variable is `Final`, so these are spelled out rather than
    # built from core.plans.PLAN_STARTER etc. They must match those constants
    # exactly; test_checkout_returns_subscription_id_and_public_key and
    # friends exercise the "pro"/"monthly" success path and would fail loudly
    # on drift.
    plan_id: Literal["starter", "pro"]
    interval: Literal["monthly", "annual"]


class CheckoutOut(BaseModel):
    """What the browser needs to open the Razorpay modal.

    `key_id` is the publishable key. The secret never appears in any response.
    """

    subscription_id: str
    key_id: str
    plan_id: str
    interval: str
    currency: str
    amount_cents: int
