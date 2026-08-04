"""The billing plan catalog — the single source of truth for what a tier allows.

Entitlements live here, in code, rather than in the payment provider's metadata:
they are versioned, reviewable, and testable, and a typo in the Razorpay
dashboard cannot silently change what customers can do. Razorpay holds plan ids
and prices and nothing else.

Every plan is priced in USD for v1. `CURRENCY` exists as a named constant, and
`razorpay_plan_id_for` is keyed so that adding an INR plan set later is
configuration plus one lookup dimension, not a rewrite.

Two rows from the public pricing matrix are deliberately absent. Mailbox limits
(1 vs 2) are unenforceable because `api/v1/integrations.py` supports exactly one
Gmail connection per user, so the limit can never be exceeded. Everything under
Vela scheduling has no implementation to gate.
"""

from dataclasses import dataclass

from core.config import settings
from models.routines import (
    ROUTINE_BRIEFING,
    ROUTINE_CATCHUP,
    ROUTINE_CHASE_THREADS,
    ROUTINE_DEADLINE_SCAN,
    ROUTINE_DOUBLE_BOOKINGS,
    ROUTINE_INVOICES,
    ROUTINE_NEWSLETTER_DIGEST,
    ROUTINE_RECONNECT,
    ROUTINE_SCHEDULE_TRUSTED,
)

PLAN_STARTER = "starter"
PLAN_PRO = "pro"

INTERVAL_MONTHLY = "monthly"
INTERVAL_ANNUAL = "annual"

# v1 bills everyone in USD, including Indian customers paying with
# international cards. See the spec's known gaps: if Razorpay requires INR for
# domestic Indian transactions, an INR plan set is added alongside this.
CURRENCY = "USD"

ALL_ROUTINES = frozenset(
    {
        ROUTINE_BRIEFING,
        ROUTINE_NEWSLETTER_DIGEST,
        ROUTINE_CHASE_THREADS,
        ROUTINE_RECONNECT,
        ROUTINE_DEADLINE_SCAN,
        ROUTINE_CATCHUP,
        ROUTINE_INVOICES,
        ROUTINE_DOUBLE_BOOKINGS,
        ROUTINE_SCHEDULE_TRUSTED,
    }
)


@dataclass(frozen=True)
class Entitlements:
    """What a plan permits. `drafts_per_month = None` means unlimited."""

    bot_hours_per_month: int
    drafts_per_month: int | None
    allowed_routines: frozenset[str]
    custom_categories: bool
    video_retention_days: int
    transcript_retention_days: int

    @property
    def bot_seconds_per_month(self) -> int:
        return self.bot_hours_per_month * 3600


@dataclass(frozen=True)
class Plan:
    id: str
    name: str
    monthly_price_cents: int
    annual_price_cents: int
    entitlements: Entitlements


PLANS: dict[str, Plan] = {
    PLAN_STARTER: Plan(
        id=PLAN_STARTER,
        name="Starter",
        monthly_price_cents=1900,
        annual_price_cents=18000,
        entitlements=Entitlements(
            bot_hours_per_month=5,
            drafts_per_month=20,
            # The public matrix says "Briefing only" for Starter, and the digest
            # rows are off. Digests are routines too, so one set covers both.
            allowed_routines=frozenset({ROUTINE_BRIEFING}),
            custom_categories=False,
            video_retention_days=7,
            transcript_retention_days=90,
        ),
    ),
    PLAN_PRO: Plan(
        id=PLAN_PRO,
        name="Pro",
        monthly_price_cents=3900,
        annual_price_cents=34800,
        entitlements=Entitlements(
            bot_hours_per_month=15,
            drafts_per_month=None,
            allowed_routines=ALL_ROUTINES,
            custom_categories=True,
            video_retention_days=30,
            transcript_retention_days=365,
        ),
    ),
}


def get_plan(plan_id: str) -> Plan:
    """The plan, or KeyError. Callers hold a validated id from our own column."""
    return PLANS[plan_id]


def razorpay_plan_id_for(plan_id: str, interval: str) -> str:
    """The configured Razorpay plan id for a plan and billing interval."""
    table = {
        (PLAN_STARTER, INTERVAL_MONTHLY): settings.RAZORPAY_PLAN_STARTER_MONTHLY,
        (PLAN_STARTER, INTERVAL_ANNUAL): settings.RAZORPAY_PLAN_STARTER_ANNUAL,
        (PLAN_PRO, INTERVAL_MONTHLY): settings.RAZORPAY_PLAN_PRO_MONTHLY,
        (PLAN_PRO, INTERVAL_ANNUAL): settings.RAZORPAY_PLAN_PRO_ANNUAL,
    }
    return table[(plan_id, interval)]
