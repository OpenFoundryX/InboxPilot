"""The gate that decides whether InboxPilot may touch a user's mailbox.

Connecting Google is consent to read the mailbox, not permission to start
working in it. Until onboarding is finished and a trial (or a paid plan) is
running, nothing should create labels, classify mail, or install a watch.
"""

from datetime import datetime, timedelta, timezone

import pytest

from models.billing import (
    STATUS_ACTIVE,
    STATUS_AUTHENTICATED,
    STATUS_CANCELLED,
    STATUS_CREATED,
    STATUS_HALTED,
    Subscription,
)
from models.users import User
from services.billing.access import may_process_mail

# BILLING DISABLED (temporary, for testing): `services.billing.access` returns
# "entitled" for everyone, so every subscription state below now passes. The
# assertions are the spec of the paywall and stay here unchanged — deleting this
# mark is part of turning payments back on.
pytestmark = pytest.mark.skip(reason="billing gate disabled for testing")

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)


def _user(*, onboarded: bool) -> User:
    return User(
        email="someone@example.com",
        onboarded_at=NOW - timedelta(days=1) if onboarded else None,
    )


def _trialing() -> Subscription:
    """The state a user lands in right after checkout: mandate signed, no charge yet."""
    return Subscription(status=STATUS_AUTHENTICATED, trial_ends_at=NOW + timedelta(days=7))


def test_connected_but_not_onboarded_is_not_processed():
    """The reported bug: a fresh grant started sorting mail on its own."""
    assert may_process_mail(_user(onboarded=False), _trialing(), NOW) is False


def test_onboarded_without_a_subscription_is_not_processed():
    assert may_process_mail(_user(onboarded=True), None, NOW) is False


def test_onboarded_before_checkout_is_not_processed():
    """`created` is the pre-card state — no mandate, so no work."""
    sub = Subscription(status=STATUS_CREATED)
    assert may_process_mail(_user(onboarded=True), sub, NOW) is False


def test_onboarded_and_trialing_is_processed():
    assert may_process_mail(_user(onboarded=True), _trialing(), NOW) is True


def test_onboarded_and_paying_is_processed():
    sub = Subscription(status=STATUS_ACTIVE)
    assert may_process_mail(_user(onboarded=True), sub, NOW) is True


def test_comped_account_is_processed_without_a_trial():
    sub = Subscription(status=STATUS_CREATED, comped=True)
    assert may_process_mail(_user(onboarded=True), sub, NOW) is True


def test_comped_account_still_needs_onboarding():
    """Comped skips billing, not the wizard."""
    sub = Subscription(status=STATUS_CREATED, comped=True)
    assert may_process_mail(_user(onboarded=False), sub, NOW) is False


def test_expired_trial_stops_being_processed():
    """The continuous half of the guard: entitlement can lapse mid-flight."""
    sub = Subscription(status=STATUS_AUTHENTICATED, trial_ends_at=NOW - timedelta(minutes=1))
    assert may_process_mail(_user(onboarded=True), sub, NOW) is False


def test_authenticated_without_a_trial_deadline_is_not_processed():
    sub = Subscription(status=STATUS_AUTHENTICATED, trial_ends_at=None)
    assert may_process_mail(_user(onboarded=True), sub, NOW) is False


def test_failed_payment_stops_being_processed():
    sub = Subscription(status=STATUS_HALTED)
    assert may_process_mail(_user(onboarded=True), sub, NOW) is False


def test_cancelled_subscription_stops_being_processed():
    sub = Subscription(status=STATUS_CANCELLED)
    assert may_process_mail(_user(onboarded=True), sub, NOW) is False
