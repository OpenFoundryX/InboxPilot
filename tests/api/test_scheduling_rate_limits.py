"""What each public limit is keyed on.

The numbers can be tuned freely. What must not regress is the *key*: a limit
keyed on an unidentifiable caller took the reschedule flow down for everyone
behind one Cloudflare edge IP after five requests.
"""

from types import SimpleNamespace

import pytest

from api.v1 import scheduling_public as public


def request_with(headers: dict | None = None, peer: str = "172.66.0.243"):
    return SimpleNamespace(
        headers=headers or {},
        client=SimpleNamespace(host=peer),
    )


@pytest.fixture
def spy(monkeypatch):
    """Record every (key, limit) the route asks for, and always allow."""
    calls: list[tuple[str, int]] = []

    async def fake_allow(key, *, limit, window_seconds):
        calls.append((key, limit))
        return True

    monkeypatch.setattr(public.ratelimit, "allow", fake_allow)
    return calls


# --------------------------------------------------------------------------
# client_key
# --------------------------------------------------------------------------


def test_forwarded_for_identifies_the_guest_when_the_proxy_sets_it():
    assert client_ip({"x-forwarded-for": "203.0.113.9, 172.66.0.243"}) == "203.0.113.9"


def test_without_the_header_we_only_have_the_proxy_address():
    """Not a bug in itself — the bug is trusting it for a tight limit."""
    assert client_ip({}) == "172.66.0.243"


def client_ip(headers: dict) -> str:
    return public.client_key(request_with(headers))


# --------------------------------------------------------------------------
# What the limits are keyed on
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_managing_a_booking_is_limited_per_token_not_per_ip(spy):
    await public._manage_limit("tok_abc", "reschedule")
    key, limit = spy[0]
    assert "tok_abc" in key
    assert limit == public.MANAGE_LIMIT_PER_TOKEN


@pytest.mark.asyncio
async def test_two_guests_behind_one_ip_do_not_share_a_manage_budget(spy):
    await public._manage_limit("tok_one", "reschedule")
    await public._manage_limit("tok_two", "reschedule")
    assert spy[0][0] != spy[1][0], "one guest's retries must not spend another's"


@pytest.mark.asyncio
async def test_the_per_ip_guard_is_loose_enough_not_to_block_a_real_session(spy):
    """It exists to stop a flood. A guest clicking around a booking page makes
    tens of requests; the ceiling has to sit well above that."""
    await public._flood_guard(request_with(), "reschedule")
    _, limit = spy[0]
    assert limit >= 60


@pytest.mark.asyncio
async def test_reschedule_leaves_room_to_retry_after_losing_a_slot(spy):
    """Losing a race returns 409 and the guest picks again. A ceiling of 5 made
    a handful of ordinary retries look like abuse."""
    for _ in range(10):
        await public._manage_limit("tok_abc", "reschedule")
    assert public.MANAGE_LIMIT_PER_TOKEN > 10


@pytest.mark.asyncio
async def test_the_booking_ceiling_is_keyed_on_the_host_whose_account_sends_mail(spy):
    await public._limit(
        f"sched:book:host:{'host-uuid'}",
        limit=public.BOOKING_LIMIT_PER_HOST,
        window=public.BOOKING_WINDOW,
    )
    assert "host-uuid" in spy[0][0]


@pytest.mark.asyncio
async def test_a_refusal_tells_the_caller_how_long_to_wait(monkeypatch):
    from fastapi import HTTPException

    async def deny(key, *, limit, window_seconds):
        return False

    monkeypatch.setattr(public.ratelimit, "allow", deny)
    with pytest.raises(HTTPException) as raised:
        await public._manage_limit("tok_abc", "reschedule")
    assert raised.value.status_code == 429
    assert raised.value.headers["Retry-After"] == str(public.MANAGE_WINDOW)
