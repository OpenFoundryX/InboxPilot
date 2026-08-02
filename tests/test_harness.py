import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from models.users import User
from tests.factories import make_user


async def test_db_fixture_persists_within_a_test(db, user):
    found = await db.scalar(select(User).where(User.id == user.id))
    assert found is not None
    assert found.email == user.email


async def test_commit_inside_db_fixture_does_not_escape_to_another_connection(db, engine):
    """Regression-tests conftest.py's `db` fixture itself (not a reimplementation
    of it): calls `session.commit()` — what service code actually does — through
    the real fixture, then checks visibility from a second, independent
    connection. If `join_transaction_mode="create_savepoint"` were ever changed
    to something that lets commit() escape the outer transaction (e.g.
    "control_fully"), this fails. Confirmed by deliberately breaking it during
    review: switching to "control_fully" makes this assert fail with the row
    visible on the other connection, and leaves a real row in the table.
    Self-contained — creates and checks its own row, so it holds regardless of
    execution order or which other tests exist.
    """
    email = f"user-{uuid.uuid4().hex[:12]}@example.com"
    await make_user(db, email=email)
    await db.commit()

    async with engine.connect() as outside_connection:
        visible = await outside_connection.scalar(select(User).where(User.email == email))
        assert visible is None, "commit() inside the SAVEPOINT escaped to another connection"


async def test_transaction_rollback_removes_committed_savepoint_data(engine):
    """Proves the second half of the same claim: once the outer transaction is
    rolled back (what the `db` fixture does unconditionally at teardown), a row
    committed inside the SAVEPOINT is gone for good, not just invisible to
    other connections while the outer transaction is still open.

    Drives the exact connect/begin/commit/rollback cycle tests/conftest.py's
    `db` fixture uses, but inline, because pytest fixture teardown timing makes
    it awkward to observe *post*-teardown state from within a test that uses
    the `db` fixture itself. Self-contained and order-independent.
    """
    email = f"user-{uuid.uuid4().hex[:12]}@example.com"

    async with engine.connect() as connection:
        transaction = await connection.begin()
        factory = async_sessionmaker(
            bind=connection,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
            join_transaction_mode="create_savepoint",
        )
        async with factory() as session:
            await make_user(session, email=email)
            await session.commit()
        await transaction.rollback()

    async with engine.connect() as verify_connection:
        gone = await verify_connection.scalar(select(User).where(User.email == email))
        assert gone is None, "row survived the outer transaction rollback"
