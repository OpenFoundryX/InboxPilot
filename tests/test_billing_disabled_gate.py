"""The dashboard's own paywall gate, while billing is switched off.

`services.billing.access` is short-circuited for testing (see its BILLING
DISABLED note), but the hard gate that actually keeps a user out of the
dashboard is not `access` — it is `subscription_started`, read by
`inboxos-web/src/app/dashboard/layout.tsx`, which redirects to
/onboarding/plan when it is false. An account with no Razorpay mandate is
exactly the case that field reports false for, so leaving it untouched left
the plan picker in front of every test account.

Delete this file when payments are turned back on: the real spec for
`_subscription_started` is "did this user ever authorise a subscription", and
these assertions contradict it on purpose.
"""

from datetime import datetime, timedelta, timezone

from api.v1.billing import _subscription_started
from models.billing import STATUS_CREATED, STATUS_EXPIRED, Subscription

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)


def _sub(status: str) -> Subscription:
    return Subscription(
        plan_id="starter",
        status=status,
        trial_ends_at=NOW + timedelta(days=7),
    )


def test_account_with_no_subscription_row_reaches_the_dashboard():
    """The never-checked-out case — every account created since billing was off."""
    assert _subscription_started(None) is True


def test_abandoned_checkout_reaches_the_dashboard():
    """`created` is the row left behind by closing the Razorpay modal."""
    assert _subscription_started(_sub(STATUS_CREATED)) is True


def test_expired_subscription_reaches_the_dashboard():
    assert _subscription_started(_sub(STATUS_EXPIRED)) is True
