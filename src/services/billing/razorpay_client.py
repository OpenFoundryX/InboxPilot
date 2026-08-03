"""Thin wrapper over the Razorpay SDK.

Everything Razorpay-shaped lives behind these functions so the rest of the app
never imports `razorpay` directly and tests can substitute one module.

The SDK is synchronous. These functions are called from async request handlers
and each makes a single short HTTP call, which is acceptable here — wrapping
them in a thread pool would add machinery for no measured benefit. If checkout
latency ever becomes a problem, this module is the one place to change.
"""

from functools import lru_cache

import razorpay

from core.config import settings


@lru_cache(maxsize=1)
def _client() -> razorpay.Client:
    return razorpay.Client(auth=(settings.RAZORPAY_KEY_ID, settings.RAZORPAY_KEY_SECRET))


def create_customer(*, email: str, name: str | None = None) -> str:
    """Create a Razorpay customer and return its id."""
    customer = _client().customer.create(
        {"email": email, "name": name or email, "fail_existing": 0}
    )
    return customer["id"]


def create_subscription(
    *,
    plan_id: str,
    customer_id: str,
    start_at: int,
    total_count: int,
    notes: dict,
) -> dict:
    """Create a subscription that first charges at `start_at`.

    `start_at` in the future is how a trial is expressed: Razorpay collects the
    mandate now and takes the first payment then. `total_count` is required by
    the API — it is the number of billing cycles, not a duration.
    """
    return _client().subscription.create(
        {
            "plan_id": plan_id,
            "customer_id": customer_id,
            "total_count": total_count,
            "start_at": start_at,
            "customer_notify": 1,
            "notes": notes,
        }
    )


def cancel_subscription(*, subscription_id: str, at_cycle_end: bool = True) -> dict:
    """Cancel a subscription, by default at the end of the paid period.

    Cancelling immediately would take away time the customer has already paid
    for, so `at_cycle_end` defaults to True.
    """
    return _client().subscription.cancel(
        subscription_id, {"cancel_at_cycle_end": 1 if at_cycle_end else 0}
    )
