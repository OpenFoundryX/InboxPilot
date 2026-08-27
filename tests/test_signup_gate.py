"""The gate itself: may this Google identity become a user?

`upsert_user_from_google` is where signup and login diverge, so it is where the
refusal belongs. Two properties matter and are easy to lose in a refactor: an
*existing* user is never gated, and a refused signup writes nothing.
"""

import pytest
from sqlalchemy import func, select

from core.exceptions import SignupNotInvited
from models.users import User
from services.auth.oauth import upsert_user_from_google
from tests.factories import make_user

PROFILE = {
    "sub": "google-sub-newcomer",
    "email": "newcomer@example.com",
    "name": "New Comer",
    "picture": "https://example.com/p.png",
}


async def test_uninvited_signup_is_refused(db):
    with pytest.raises(SignupNotInvited):
        await upsert_user_from_google(db, PROFILE, signup_allowed=False)


async def test_a_refused_signup_writes_no_user(db):
    """The refusal must land before `db.add`, or a rejected visitor still exists."""
    before = await db.scalar(select(func.count()).select_from(User))
    with pytest.raises(SignupNotInvited):
        await upsert_user_from_google(db, PROFILE, signup_allowed=False)
    after = await db.scalar(select(func.count()).select_from(User))
    assert after == before


async def test_allowed_signup_creates_the_user_and_reports_created(db):
    user, created = await upsert_user_from_google(db, PROFILE, signup_allowed=True)
    assert created is True
    assert user.email == "newcomer@example.com"
    assert user.google_sub == "google-sub-newcomer"
    assert user.last_login_at is not None


async def test_an_existing_user_logs_in_even_when_signups_are_closed(db):
    """The whole point: login keeps working while the front door is shut."""
    existing = await make_user(db, email="already@example.com", google_sub="google-sub-existing")

    user, created = await upsert_user_from_google(
        db,
        {"sub": "google-sub-existing", "email": "already@example.com", "name": "Already In"},
        signup_allowed=False,
    )

    assert created is False
    assert user.id == existing.id


async def test_an_existing_user_has_their_profile_refreshed(db):
    await make_user(db, email="old@example.com", google_sub="google-sub-refresh")

    user, created = await upsert_user_from_google(
        db,
        {"sub": "google-sub-refresh", "email": "new@example.com", "name": "Renamed"},
        signup_allowed=False,
    )

    assert created is False
    assert user.email == "new@example.com"
    assert user.full_name == "Renamed"


async def test_the_exception_carries_the_email_for_logs(db):
    with pytest.raises(SignupNotInvited) as caught:
        await upsert_user_from_google(db, PROFILE, signup_allowed=False)
    assert "newcomer@example.com" in str(caught.value)
