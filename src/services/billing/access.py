"""One rule for "may this account use the product", asked in one place.

Both the API and every Celery sweep need this answer, and a second copy of the
rule is how a locked account keeps getting billable work done.

BILLING DISABLED (temporary, for testing) — `resolve_access` and
`effective_plan_id` below are short-circuited so the product runs without a
subscription. Every paywall in the app funnels through these two: the 402 on
`require_entitled`, the per-feature checks in `entitlements.check`, and the mail
pipeline's own `may_process_mail`. Restoring payments means deleting the two
early returns and uncommenting the bodies underneath them — nothing else was
changed, and Razorpay checkout and its webhooks still run.
"""

from datetime import datetime

# PLAN_STARTER, ENTITLED_STATUSES and STATUS_AUTHENTICATED are used only by the
# commented-out bodies below; kept imported so restoring them is one edit.
from core.plans import PLAN_PRO, PLAN_STARTER  # noqa: F401
from models.billing import (  # noqa: F401
    ENTITLED_STATUSES,
    STATUS_AUTHENTICATED,
    Subscription,
)
from models.users import User

ACCESS_ENTITLED = "entitled"
ACCESS_LOCKED = "locked"


def resolve_access(sub: Subscription | None, now: datetime) -> str:
    """Whether `sub` grants access right now.

    BILLING DISABLED: everyone is entitled, subscription or not.
    """
    return ACCESS_ENTITLED

    # if sub is None:
    #     return ACCESS_LOCKED
    # if sub.comped:
    #     return ACCESS_ENTITLED
    # if sub.status not in ENTITLED_STATUSES:
    #     return ACCESS_LOCKED
    # # Razorpay moves a converted trial to `active` itself once the first charge
    # # succeeds, but a backfilled account sits in `authenticated` with no Razorpay
    # # record at all — nothing external will ever change its status, so the
    # # deadline has to be enforced here.
    # if sub.status == STATUS_AUTHENTICATED:
    #     if sub.trial_ends_at is None or sub.trial_ends_at <= now:
    #         return ACCESS_LOCKED
    # return ACCESS_ENTITLED


def may_process_mail(user: User, sub: Subscription | None, now: datetime) -> bool:
    """Whether InboxPilot may do work inside this user's mailbox.

    Two conditions, both required. Connecting Google is consent to read the
    mailbox; it is not permission to start writing labels into it, and it says
    nothing about whether the account is paying. Until the wizard is finished
    *and* a trial or paid plan is running, the mail pipeline stays off.

    This is deliberately stricter than `resolve_access`. Entitlement alone lets
    a user drive the API; touching their inbox unprompted is the one thing that
    happens without them asking, so it carries the extra condition.

    BILLING DISABLED: with `resolve_access` short-circuited this reduces to the
    onboarding half, which is the condition that should survive anyway — the
    wizard, not the plan, is what says "you may work in my mailbox".
    """
    return user.onboarded_at is not None and resolve_access(sub, now) == ACCESS_ENTITLED


def effective_plan_id(sub: Subscription | None) -> str:
    """Which plan's entitlements apply. Comped accounts get Pro.

    BILLING DISABLED: Pro for everyone. Without this an account with no
    subscription resolves to Starter, and `entitlements.check` would still deny
    custom categories, the Pro routines and anything past the draft quota — a
    paywall in all but name, which is not what turning payments off means.
    """
    return PLAN_PRO

    # if sub is None:
    #     return PLAN_STARTER
    # if sub.comped:
    #     return PLAN_PRO
    # return sub.plan_id
