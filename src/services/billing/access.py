"""One rule for "may this account use the product", asked in one place.

Both the API and every Celery sweep need this answer, and a second copy of the
rule is how a locked account keeps getting billable work done.
"""

from datetime import datetime

from core.plans import PLAN_PRO, PLAN_STARTER
from models.billing import (
    ENTITLED_STATUSES,
    STATUS_AUTHENTICATED,
    Subscription,
)
from models.users import User

ACCESS_ENTITLED = "entitled"
ACCESS_LOCKED = "locked"


def resolve_access(sub: Subscription | None, now: datetime) -> str:
    """Whether `sub` grants access right now."""
    if sub is None:
        return ACCESS_LOCKED
    if sub.comped:
        return ACCESS_ENTITLED
    if sub.status not in ENTITLED_STATUSES:
        return ACCESS_LOCKED
    # Razorpay moves a converted trial to `active` itself once the first charge
    # succeeds, but a backfilled account sits in `authenticated` with no Razorpay
    # record at all — nothing external will ever change its status, so the
    # deadline has to be enforced here.
    if sub.status == STATUS_AUTHENTICATED:
        if sub.trial_ends_at is None or sub.trial_ends_at <= now:
            return ACCESS_LOCKED
    return ACCESS_ENTITLED


def may_process_mail(user: User, sub: Subscription | None, now: datetime) -> bool:
    """Whether InboxPilot may do work inside this user's mailbox.

    Two conditions, both required. Connecting Google is consent to read the
    mailbox; it is not permission to start writing labels into it, and it says
    nothing about whether the account is paying. Until the wizard is finished
    *and* a trial or paid plan is running, the mail pipeline stays off.

    This is deliberately stricter than `resolve_access`. Entitlement alone lets
    a user drive the API; touching their inbox unprompted is the one thing that
    happens without them asking, so it carries the extra condition.
    """
    return user.onboarded_at is not None and resolve_access(sub, now) == ACCESS_ENTITLED


def effective_plan_id(sub: Subscription | None) -> str:
    """Which plan's entitlements apply. Comped accounts get Pro."""
    if sub is None:
        return PLAN_STARTER
    if sub.comped:
        return PLAN_PRO
    return sub.plan_id
