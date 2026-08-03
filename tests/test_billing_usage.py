from datetime import date, datetime, timezone

from sqlalchemy import update

from models.billing import UsageCounter
from services.billing.usage import (
    add_bot_seconds,
    add_drafts,
    get_or_create_counter,
    period_start_for,
)

AUG = datetime(2026, 8, 15, 9, 30, tzinfo=timezone.utc)
SEP = datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc)


def test_period_start_is_the_first_of_the_month():
    assert period_start_for(AUG) == date(2026, 8, 1)


def test_period_start_on_the_boundary_belongs_to_the_new_month():
    assert period_start_for(SEP) == date(2026, 9, 1)


async def test_counter_is_created_on_demand(db, user):
    counter = await get_or_create_counter(db, user.id, AUG)
    assert counter.period_start == date(2026, 8, 1)
    assert counter.bot_seconds_used == 0


async def test_counter_is_reused_within_a_period(db, user):
    first = await get_or_create_counter(db, user.id, AUG)
    second = await get_or_create_counter(db, user.id, AUG.replace(day=28))
    assert first.id == second.id


async def test_get_or_create_counter_returns_the_winner_on_a_concurrent_insert(db, user):
    """Two first-time GET /billing/subscription callers both INSERT the same
    `(user_id, period_start)` row. `ON CONFLICT DO NOTHING` makes the loser's
    insert a no-op; they must still get the winner's row back, not a 500 —
    that is what was breaking the dashboard when TrialPill/SubscribeBanner/
    layout all fetched at once.
    """
    from models.billing import UsageCounter

    winner = UsageCounter(user_id=user.id, period_start=date(2026, 8, 1))
    db.add(winner)
    await db.flush()

    # This call's INSERT conflicts with `winner` and must not raise.
    result = await get_or_create_counter(db, user.id, AUG)

    assert result.id == winner.id
    assert result.bot_seconds_used == 0


async def test_bot_seconds_accumulate(db, user):
    await add_bot_seconds(db, user.id, 1800, AUG)
    counter = await add_bot_seconds(db, user.id, 900, AUG)
    assert counter.bot_seconds_used == 2700


async def test_drafts_accumulate(db, user):
    await add_drafts(db, user.id, 1, AUG)
    counter = await add_drafts(db, user.id, 2, AUG)
    assert counter.drafts_generated == 3


async def test_a_new_month_starts_from_zero(db, user):
    await add_bot_seconds(db, user.id, 3600, AUG)
    september = await get_or_create_counter(db, user.id, SEP)
    assert september.bot_seconds_used == 0


async def test_counters_do_not_leak_between_users(db, user):
    from tests.factories import make_user

    other = await make_user(db)
    await add_bot_seconds(db, user.id, 3600, AUG)
    counter = await get_or_create_counter(db, other.id, AUG)
    assert counter.bot_seconds_used == 0


async def test_add_bot_seconds_increments_atomically_in_sql(db, user):
    """`add_bot_seconds` must add via `UPDATE ... SET x = x + n` in SQL, not a
    Python `counter.x += n` read-modify-write — the latter is a lost-update
    race when two `process_meeting` runs for different meetings of the same
    user touch the same counter row concurrently, and it fails silently.

    True concurrent sessions aren't reachable in this harness: the `db`
    fixture is one connection wrapped in one transaction, so two ORM sessions
    sharing it can't race the way two independent Celery-task sessions would.
    What *is* reachable, and is exactly the mechanism a real race exploits, is
    a stale in-memory counter object: `get_or_create_counter`'s SELECT returns
    the same identity-mapped instance on a second lookup without refreshing
    it from the row, so a concurrent writer's committed change is invisible to
    it — precisely what happens when a second session loads the counter
    before a concurrent writer's commit. Forcing that staleness with a raw,
    ORM-bypassing UPDATE reproduces the bug a Python `+=` has under
    concurrency: the old implementation would compute `0 + 300 = 300` here,
    silently discarding the "other session's" 500. This test only proves the
    SQL is atomic; it does not exercise two real concurrent connections.
    """
    counter = await get_or_create_counter(db, user.id, AUG)

    # Stand-in for a concurrent writer that already committed +500 to this
    # row without going through this session's ORM object, which is exactly
    # what a second, independent session's writer would look like from here.
    # `synchronize_session=False` is what makes this a faithful stand-in:
    # without it, SQLAlchemy's ORM-aware bulk UPDATE would "helpfully" patch
    # this session's already-loaded `counter` object to match, which is
    # exactly the synchronization a second, truly independent session
    # wouldn't get — and would silently defeat this test.
    await db.execute(
        update(UsageCounter)
        .where(UsageCounter.id == counter.id)
        .values(bot_seconds_used=500)
        .execution_options(synchronize_session=False)
    )

    result = await add_bot_seconds(db, user.id, 300, AUG)

    assert result.bot_seconds_used == 800


async def test_add_drafts_increments_atomically_in_sql(db, user):
    """Same race, same fix, same proof — for the drafts counter Task 10 uses."""
    counter = await get_or_create_counter(db, user.id, AUG)

    await db.execute(
        update(UsageCounter)
        .where(UsageCounter.id == counter.id)
        .values(drafts_generated=7)
        .execution_options(synchronize_session=False)
    )

    result = await add_drafts(db, user.id, 2, AUG)

    assert result.drafts_generated == 9
