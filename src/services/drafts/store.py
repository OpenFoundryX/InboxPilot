"""Async DB access for draft settings and uploaded files.

Get-or-create for the settings singleton, matching `services.categorization.store`:
the first read seeds defaults, so nothing has to happen at signup.

There is nothing here for the drafts themselves — none are stored. The guard
against drafting an email twice is the `gmail.DRAFTED_LABEL` marker in the user's
mailbox, not a row in this database.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.drafts import DraftFile, DraftSettings


async def get_or_create_settings(db: AsyncSession, user_id: uuid.UUID) -> DraftSettings:
    row = await db.scalar(select(DraftSettings).where(DraftSettings.user_id == user_id))
    if row is None:
        row = DraftSettings(user_id=user_id)
        db.add(row)
        try:
            # Concurrent first-time callers all see the empty SELECT above; the
            # unique index on user_id lets exactly one win. The caller asked for
            # get-or-create, so the answer is the winner's row, not a 409. The
            # SAVEPOINT scopes the rollback to this insert, leaving the request's
            # own transaction usable.
            async with db.begin_nested():
                await db.flush()
        except IntegrityError:
            row = await db.scalar(
                select(DraftSettings).where(DraftSettings.user_id == user_id)
            )
            # The winner has committed by the time our INSERT was told it
            # conflicted, so the re-read always finds it.
            assert row is not None
    return row


async def list_files(
    db: AsyncSession, user_id: uuid.UUID, purpose: str | None = None
) -> list[DraftFile]:
    """Newest first — the file a user just uploaded is the one they want to see."""
    stmt = select(DraftFile).where(DraftFile.user_id == user_id)
    if purpose is not None:
        stmt = stmt.where(DraftFile.purpose == purpose)
    return list(await db.scalars(stmt.order_by(DraftFile.created_at.desc())))


async def get_file(
    db: AsyncSession, user_id: uuid.UUID, file_id: uuid.UUID
) -> DraftFile | None:
    return await db.scalar(
        select(DraftFile).where(DraftFile.user_id == user_id, DraftFile.id == file_id)
    )


async def users_with_drafting_enabled(db: AsyncSession) -> list[DraftSettings]:
    """Every user who has opted into drafting. The sweeps decide who is due."""
    return list(
        await db.scalars(select(DraftSettings).where(DraftSettings.is_enabled.is_(True)))
    )
