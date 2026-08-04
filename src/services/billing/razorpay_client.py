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
    """Create a Razorpay customer and return its id.

    `fail_existing` must be the string `"0"`, not the integer 0 — Razorpay's
    API documents it as a string, and an integer 0 is treated as omitted
    (default `"1"`), which throws "Customer already exists for the merchant"
    on retry. That retry path is common here: checkout commits the local
    trial row, then calls Razorpay; if subscription creation fails after the
    customer was created, the next attempt has no `razorpay_customer_id` on
    our row but the email already exists at Razorpay.
    """
    customer = _client().customer.create(
        {"email": email, "name": name or email, "fail_existing": "0"}
    )
    return customer["id"]


def create_subscription(
    *,
    plan_id: str,
    customer_id: str,
    start_at: int | None,
    total_count: int,
    notes: dict,
) -> dict:
    """Create a subscription, optionally deferring its first charge to `start_at`.

    `start_at` in the future is how a trial is expressed: Razorpay collects the
    mandate now and takes the first payment then. `start_at` is optional to
    Razorpay's API — when omitted, billing starts immediately once the
    customer authorises the mandate, which is exactly the semantics wanted
    when there is no meaningful future instant to give it (see
    `start_checkout`'s lead-time check, which decides when that's the case).
    `None` is therefore left out of the payload entirely rather than passed
    through: the SDK sends whatever dict it's given verbatim, and a literal
    `null` is not the same thing to Razorpay as the key being absent.

    `total_count` is required by the API — it is the number of billing
    cycles, not a duration.
    """
    payload = {
        "plan_id": plan_id,
        "customer_id": customer_id,
        "total_count": total_count,
        "customer_notify": 1,
        "notes": notes,
    }
    if start_at is not None:
        payload["start_at"] = start_at
    return _client().subscription.create(payload)


def cancel_subscription(*, subscription_id: str, at_cycle_end: bool = True) -> dict:
    """Cancel a subscription, by default at the end of the paid period.

    Cancelling immediately would take away time the customer has already paid
    for, so `at_cycle_end` defaults to True.
    """
    return _client().subscription.cancel(
        subscription_id, {"cancel_at_cycle_end": 1 if at_cycle_end else 0}
    )
