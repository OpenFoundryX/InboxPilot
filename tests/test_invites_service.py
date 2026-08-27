"""The allowlist's read/write rules.

Case-insensitivity is the interesting property here. Google hands us the address
as the user's provider spells it, so every one of these has to agree on what
"the same mailbox" means or an invited customer gets turned away at the door.
"""

from sqlalchemy import select

from models.invites import InvitedEmail
from services.auth import invites
from tests.factories import make_user


def test_normalize_lowercases_and_strips():
    assert invites.normalize_email("  Nilesh@X.COM ") == "nilesh@x.com"


def test_normalize_is_idempotent():
    once = invites.normalize_email(" A@B.com ")
    assert invites.normalize_email(once) == once


async def test_uninvited_email_is_not_invited(db):
    assert await invites.is_invited(db, "nobody@example.com") is False


async def test_invited_email_is_invited(db):
    db.add(InvitedEmail(email="yes@example.com"))
    await db.flush()
    assert await invites.is_invited(db, "yes@example.com") is True


async def test_is_invited_ignores_case_and_whitespace(db):
    db.add(InvitedEmail(email="mixed@example.com"))
    await db.flush()
    assert await invites.is_invited(db, "  Mixed@Example.COM ") is True


async def test_an_already_claimed_invite_still_counts_as_invited(db):
    """The claim is a record, not a lock.

    A user who is deleted and signs up again, or a second Google account on the
    same mailbox, must not be locked out by their own past claim — and the gate
    only consults this on the signup branch anyway.
    """
    user = await make_user(db)
    row = InvitedEmail(email="repeat@example.com")
    db.add(row)
    await db.flush()
    await invites.claim(db, "repeat@example.com", user.id)

    assert await invites.is_invited(db, "repeat@example.com") is True


async def test_claim_records_who_and_when(db):
    user = await make_user(db)
    db.add(InvitedEmail(email="claimme@example.com"))
    await db.flush()

    await invites.claim(db, "CLAIMME@example.com", user.id)

    row = await db.scalar(select(InvitedEmail).where(InvitedEmail.email == "claimme@example.com"))
    assert row.claimed_by_user_id == user.id
    assert row.claimed_at is not None


async def test_claim_twice_keeps_the_first_claimer(db):
    first = await make_user(db)
    second = await make_user(db)
    db.add(InvitedEmail(email="once@example.com"))
    await db.flush()

    await invites.claim(db, "once@example.com", first.id)
    await invites.claim(db, "once@example.com", second.id)

    row = await db.scalar(select(InvitedEmail).where(InvitedEmail.email == "once@example.com"))
    assert row.claimed_by_user_id == first.id


async def test_claiming_an_email_with_no_invite_is_a_no_op(db):
    """Belt and braces: `claim` runs after the gate said yes, but it must not
    explode if it is ever called for an address with no row."""
    user = await make_user(db)
    await invites.claim(db, "ghost@example.com", user.id)  # must not raise
