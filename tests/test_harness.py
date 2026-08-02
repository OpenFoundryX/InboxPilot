from sqlalchemy import select

from models.users import User


async def test_db_fixture_persists_within_a_test(db, user):
    found = await db.scalar(select(User).where(User.id == user.id))
    assert found is not None
    assert found.email == user.email


async def test_db_fixture_rolls_back_between_tests(db):
    # The user created by the previous test must not survive into this one.
    # Scoped to factory-made rows (the "user-<hex>@example.com" pattern from
    # tests/factories.py) because this suite points at the ordinary
    # development database, which may already hold real, unrelated user rows.
    count = len((await db.scalars(select(User).where(User.email.like("user-%@example.com")))).all())
    assert count == 0
