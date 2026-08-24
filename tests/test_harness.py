"""The recovered harness must actually roll back.

`tests/conftest.py` was deleted by accident in 6db459e and recovered here. Its
whole value is the rollback isolation, and a harness that silently stopped
rolling back would leave the suite passing while filling the development
database with rows. These two tests fail if that happens: the first writes a
user and commits, the second counts users with that email and must find none.
"""

from sqlalchemy import func, select

from models.users import User
from tests.factories import make_user

LEAKED_EMAIL = "harness-isolation-probe@example.com"


async def test_writes_and_commits_are_visible_inside_the_test(db):
    user = await make_user(db, email=LEAKED_EMAIL)
    # A service-code commit must not end the test's isolation — that is what
    # join_transaction_mode="create_savepoint" buys.
    await db.commit()
    found = await db.scalar(select(User).where(User.email == LEAKED_EMAIL))
    assert found is not None
    assert found.id == user.id


async def test_the_previous_test_left_nothing_behind(db):
    count = await db.scalar(
        select(func.count()).select_from(User).where(User.email == LEAKED_EMAIL)
    )
    assert count == 0, "harness stopped rolling back — it is writing to the real database"
