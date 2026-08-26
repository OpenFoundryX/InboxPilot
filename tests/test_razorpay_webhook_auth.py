"""The Razorpay webhook's front door.

This route is public and its signature check is the only thing standing between
a stranger and `handle_event`, which will happily bind a `subscription.charged`
to whatever `notes.user_id` the body names. Nothing exercised it before: it is
reached only by a live POST, so every regression here would have shipped.

Three properties are pinned, and the third is what keeps the first two from
being satisfied by an endpoint that rejects everything:

  * no secret configured  -> 503, and nothing downstream runs. An empty HMAC key
    is one anyone can reproduce, so "unconfigured" must fail closed rather than
    validate forgeries.
  * wrong signature       -> 400, and nothing downstream runs.
  * correct signature     -> `handle_event` is reached.

`handle_event` is replaced by a stub that raises if called (the pattern in
`test_mail_gate_call_sites.py`), because "returned 4xx" and "did not act on the
body" are different claims and only the second one matters.
"""

import hashlib
import hmac
import json

import pytest
from fastapi import HTTPException, status

from api.v1 import webhooks as webhooks_mod
from core.config import settings

SECRET = "a-real-webhook-secret"
BODY = json.dumps(
    {
        "event": "subscription.charged",
        "payload": {"subscription": {"entity": {"id": "sub_test_1", "status": "active"}}},
    }
).encode()


class _Request:
    """Minimal stand-in: the handler reads the raw body and one header."""

    def __init__(self, body: bytes, signature: str | None):
        self._body = body
        # No `x-razorpay-event-id`: the dedupe claim needs Redis, and the
        # signature gate is what is under test. The handler logs and continues.
        self.headers = {} if signature is None else {"x-razorpay-signature": signature}

    async def body(self) -> bytes:
        return self._body


def _sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def exploding_handler(monkeypatch):
    """`handle_event` must not be reached for an unverified body."""

    async def _boom(db, event):
        raise AssertionError("handle_event ran on an unverified body")

    monkeypatch.setattr(webhooks_mod, "handle_event", _boom)


async def test_rejects_when_secret_unset(db, monkeypatch, exploding_handler):
    """The forgery window: with no secret, the HMAC key is the empty string."""
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", "")
    # Signed with "" — exactly what any caller can compute unaided.
    forged = _sign(BODY, "")

    with pytest.raises(HTTPException) as exc:
        await webhooks_mod.razorpay_webhook(_Request(BODY, forged), db)

    assert exc.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE


async def test_rejects_a_bad_signature(db, monkeypatch, exploding_handler):
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", SECRET)

    with pytest.raises(HTTPException) as exc:
        await webhooks_mod.razorpay_webhook(_Request(BODY, "not-the-signature"), db)

    assert exc.value.status_code == status.HTTP_400_BAD_REQUEST


async def test_rejects_a_missing_signature_header(db, monkeypatch, exploding_handler):
    """Absent, not merely wrong — the handler defaults it to "" and must not
    then match the empty-key digest."""
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", SECRET)

    with pytest.raises(HTTPException) as exc:
        await webhooks_mod.razorpay_webhook(_Request(BODY, None), db)

    assert exc.value.status_code == status.HTTP_400_BAD_REQUEST


async def test_accepts_a_valid_signature(db, monkeypatch):
    """The guard must not have become an unconditional off switch."""
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", SECRET)

    seen = []

    async def _record(db_arg, event):
        seen.append(event)
        return "applied"

    monkeypatch.setattr(webhooks_mod, "handle_event", _record)

    result = await webhooks_mod.razorpay_webhook(_Request(BODY, _sign(BODY, SECRET)), db)

    assert result == {"status": "applied"}
    assert seen and seen[0]["event"] == "subscription.charged"
