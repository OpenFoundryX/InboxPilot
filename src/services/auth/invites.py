"""Is this mailbox allowed to become a user?

Signups are closed while the first ~100 customers are onboarded by hand. Because
signup and login are the same Google OAuth callback, this module is what tells
them apart: `is_invited` is consulted only when the callback is about to create
a new user, and existing users are never asked.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models.invites import InvitedEmail


def normalize_email(raw: str) -> str:
    """The one spelling of an address that `invited_emails.email` stores.

    Every write and every lookup goes through here. Google returns the address
    as the user's provider spells it, so an invite added as `Nilesh@X.com` must
    still match a login that arrives as `nilesh@x.com`.
    """
    return raw.strip().lower()


async def is_invited(db: AsyncSession, email: str) -> bool:
    """Whether this mailbox has a slot.

    Claimed invites still return True. The claim is a record of a slot being
    used, not a lock — and this is only ever asked on the signup branch, so a
    claimed row answering True cannot let an extra person in past the one the
    row was for.
    """
    row = await db.scalar(
        select(InvitedEmail.id).where(InvitedEmail.email == normalize_email(email))
    )
    return row is not None


async def claim(db: AsyncSession, email: str, user_id: uuid.UUID) -> None:
    """Record that `user_id` used this invite. Idempotent.

    Only the first claim is kept: re-running must not rewrite who took a slot,
    which is the one fact this row exists to preserve. An address with no row is
    a silent no-op — the gate has already run by the time we get here, so
    raising would turn a bookkeeping miss into a failed login.

    Uses an atomic UPDATE with a WHERE guard on claimed_at IS NULL to ensure
    concurrent claims are safe: only one can succeed, and it keeps the first.
    """
    await db.execute(
        update(InvitedEmail)
        .where(
            InvitedEmail.email == normalize_email(email),
            InvitedEmail.claimed_at.is_(None),
        )
        .values(claimed_at=datetime.now(timezone.utc), claimed_by_user_id=user_id)
    )
    await db.flush()
