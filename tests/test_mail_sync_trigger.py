"""What starts the mail pipeline, now that connecting Google no longer does.

Onboarding and checkout can finish in either order, so both call this and
whichever runs second is the one that actually starts the sync. It has to be
safe to call from both without double-firing.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from models.billing import STATUS_AUTHENTICATED, STATUS_CREATED, Subscription
from models.users import User
from services.billing.gate import maybe_start_mail_sync

NOW = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)


class _FakeSession:
    """Just enough session for `get_subscription`'s single `scalar` call."""

    def __init__(self, sub: Subscription | None):
        self._sub = sub

    async def scalar(self, *args, **kwargs):
        return self._sub


@pytest.fixture
def enqueued(monkeypatch):
    """Capture what the trigger hands to Celery instead of hitting a broker."""
    from workers.jobs import sync_last_7_days as sync_mod

    calls = []
    monkeypatch.setattr(
        sync_mod.sync_last_7_days,
        "apply_async",
        lambda args=(), **kwargs: calls.append(args),
    )
    return calls


def _user(*, onboarded: bool, synced: bool = False) -> User:
    return User(
        id=uuid.uuid4(),
        email="someone@example.com",
        onboarded_at=NOW - timedelta(days=1) if onboarded else None,
        initial_sync_at=NOW - timedelta(hours=1) if synced else None,
    )


def _trialing() -> Subscription:
    # `maybe_start_mail_sync` (via `resolve_access`) checks this against the
    # real wall clock, not the fixture's fixed `NOW` used elsewhere in this
    # file — so the deadline has to be anchored to actual now, or this
    # subscription silently becomes "expired" once real time catches up to
    # whatever `NOW` was when the fixture was written.
    return Subscription(
        status=STATUS_AUTHENTICATED, trial_ends_at=datetime.now(timezone.utc) + timedelta(days=7)
    )


async def test_starts_when_onboarding_completes_last(enqueued):
    user = _user(onboarded=True)

    started = await maybe_start_mail_sync(_FakeSession(_trialing()), user)

    assert started is True
    assert enqueued == [(str(user.id),)]


async def test_does_not_start_before_checkout(enqueued):
    """Onboarding done, no card yet — the reported bug, at the trigger."""
    user = _user(onboarded=True)

    started = await maybe_start_mail_sync(_FakeSession(Subscription(status=STATUS_CREATED)), user)

    assert started is False
    assert enqueued == []


async def test_does_not_start_before_onboarding_completes(enqueued):
    user = _user(onboarded=False)

    started = await maybe_start_mail_sync(_FakeSession(_trialing()), user)

    assert started is False
    assert enqueued == []


async def test_does_not_start_twice(enqueued):
    """Both callers fire on a user who already synced; neither may re-enqueue."""
    user = _user(onboarded=True, synced=True)

    started = await maybe_start_mail_sync(_FakeSession(_trialing()), user)

    assert started is False
    assert enqueued == []
