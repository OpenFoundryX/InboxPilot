"""The invite allowlist's storage contract.

Only two things about this table are load-bearing enough to test: the unique
index (two invites for one mailbox is a bug, not a second slot) and that
`claimed_at`/`claimed_by_user_id` start null, since `claim` distinguishes
claimed from unclaimed by exactly that.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from models.invites import InvitedEmail
from tests.factories import make_user


async def test_a_new_invite_starts_unclaimed(db):
    db.add(InvitedEmail(email="someone@example.com", note="met at a call"))
    await db.flush()

    row = await db.scalar(select(InvitedEmail).where(InvitedEmail.email == "someone@example.com"))
    assert row.claimed_at is None
    assert row.claimed_by_user_id is None
    assert row.invited_at is not None
    assert row.note == "met at a call"


async def test_the_same_email_cannot_be_invited_twice(db):
    db.add(InvitedEmail(email="dup@example.com"))
    await db.flush()
    db.add(InvitedEmail(email="dup@example.com"))
    with pytest.raises(IntegrityError):
        await db.flush()


async def test_an_invite_records_who_claimed_it(db):
    user = await make_user(db, email="claimer@example.com")
    row = InvitedEmail(email="claimer@example.com")
    db.add(row)
    await db.flush()

    row.claimed_by_user_id = user.id
    await db.flush()

    assert row.claimed_by_user_id == user.id
