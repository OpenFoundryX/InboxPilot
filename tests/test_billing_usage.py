from datetime import date, datetime, timezone

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
