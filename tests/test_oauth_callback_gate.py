"""The callback's behaviour on a refused signup.

The Google round-trip is patched out — what is under test is the callback's own
branching, not Google. Two things are easy to get wrong and both are pinned
here: the redirect target, and clearing the PKCE cookies on the way out. A
refused visitor who keeps a live 10-minute `oauth_state` carries it into their
next attempt.
"""

import pytest
from fastapi import status

from api.v1 import auth as auth_mod
from core.config import settings
from models.invites import InvitedEmail
from tests.factories import make_user

NEWCOMER = {
    "sub": "google-sub-callback-new",
    "email": "callback-new@example.com",
    "name": "Call Back",
}


@pytest.fixture
def google_returns(monkeypatch):
    """Make `exchange_code_for_profile` hand back a chosen profile."""

    def _set(profile):
        async def _fake(code, verifier):
            return profile

        monkeypatch.setattr(auth_mod.service, "exchange_code_for_profile", _fake)

    return _set


async def _call(db, request):
    return await auth_mod.google_callback(request, db, code="c", state="s")


class _Request:
    """Minimal stand-in carrying just the cookies the callback reads."""

    def __init__(self, state="s", verifier="v"):
        self.cookies = {"oauth_state": state, "oauth_verifier": verifier}


async def test_uninvited_signup_redirects_to_login_with_the_error(db, google_returns):
    google_returns(NEWCOMER)
    resp = await _call(db, _Request())

    assert resp.status_code == status.HTTP_303_SEE_OTHER
    assert resp.headers["location"] == f"{settings.LOGIN_URL}?error=not_invited"


async def test_a_refused_signup_clears_the_pkce_cookies(db, google_returns):
    google_returns(NEWCOMER)
    resp = await _call(db, _Request())

    cleared = "".join(resp.headers.getlist("set-cookie"))
    assert "oauth_state=" in cleared
    assert "oauth_verifier=" in cleared


async def test_an_invited_signup_is_let_in_and_the_invite_is_claimed(db, google_returns):
    db.add(InvitedEmail(email=NEWCOMER["email"]))
    await db.flush()
    google_returns(NEWCOMER)

    resp = await _call(db, _Request())

    assert resp.status_code == status.HTTP_303_SEE_OTHER
    assert resp.headers["location"] == settings.POST_LOGIN_REDIRECT_URL

    from sqlalchemy import select

    row = await db.scalar(select(InvitedEmail).where(InvitedEmail.email == NEWCOMER["email"]))
    assert row.claimed_at is not None


async def test_an_existing_user_is_let_in_without_an_invite(db, google_returns):
    await make_user(db, email="cb-existing@example.com", google_sub="google-sub-cb-existing")
    google_returns(
        {"sub": "google-sub-cb-existing", "email": "cb-existing@example.com", "name": "X"}
    )

    resp = await _call(db, _Request())

    assert resp.status_code == status.HTTP_303_SEE_OTHER
    assert resp.headers["location"] == settings.POST_LOGIN_REDIRECT_URL
