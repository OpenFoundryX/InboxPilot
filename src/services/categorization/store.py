"""Async DB access for the categorization taxonomy, rules, and settings.

Get-or-create throughout, matching `services.mailman.store`: the first read of a
user's taxonomy seeds the six built-ins, so nothing has to happen at signup.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.categorization import (
    BUILTIN_CATEGORIES,
    CategorizationSettings,
    EmailCategory,
    default_actions,
)


async def get_or_create_categories(db: AsyncSession, user_id: uuid.UUID) -> list[EmailCategory]:
    """Return the user's taxonomy in display order, seeding built-ins on first call."""
    rows = list(
        await db.scalars(
            select(EmailCategory)
            .where(EmailCategory.user_id == user_id)
            .order_by(EmailCategory.sort_order, EmailCategory.key)
        )
    )
    if rows:
        return rows

    for index, builtin in enumerate(BUILTIN_CATEGORIES):
        db.add(
            EmailCategory(
                user_id=user_id,
                key=builtin.key,
                gmail_label=builtin.gmail_label,
                display_name=builtin.display_name,
                description=builtin.description,
                color_bg=builtin.color_bg,
                color_text=builtin.color_text,
                is_builtin=True,
                is_enabled=True,
                sort_order=index,
                actions=default_actions(),
            )
        )
    await db.flush()

    return list(
        await db.scalars(
            select(EmailCategory)
            .where(EmailCategory.user_id == user_id)
            .order_by(EmailCategory.sort_order, EmailCategory.key)
        )
    )


async def get_category(
    db: AsyncSession, user_id: uuid.UUID, key: str
) -> EmailCategory | None:
    return await db.scalar(
        select(EmailCategory).where(
            EmailCategory.user_id == user_id, EmailCategory.key == key
        )
    )


async def get_or_create_settings(
    db: AsyncSession, user_id: uuid.UUID
) -> CategorizationSettings:
    row = await db.scalar(
        select(CategorizationSettings).where(CategorizationSettings.user_id == user_id)
    )
    if row is None:
        row = CategorizationSettings(user_id=user_id)
        db.add(row)
        await db.flush()
    return row
