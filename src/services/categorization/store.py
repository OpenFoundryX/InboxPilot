"""Async DB access for the categorization taxonomy, rules, and settings.

Get-or-create throughout, matching `services.mailman.store`: the first read of a
user's taxonomy seeds the six built-ins, so nothing has to happen at signup.
"""

import re
import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models.categorization import (
    BUILTIN_CATEGORIES,
    CategorizationRule,
    CategorizationSettings,
    EmailCategory,
    default_actions,
)


async def _select_categories(db: AsyncSession, user_id: uuid.UUID) -> list[EmailCategory]:
    return list(
        await db.scalars(
            select(EmailCategory)
            .where(EmailCategory.user_id == user_id)
            .order_by(EmailCategory.sort_order, EmailCategory.key)
        )
    )


async def get_or_create_categories(db: AsyncSession, user_id: uuid.UUID) -> list[EmailCategory]:
    """Return the user's taxonomy in display order, seeding built-ins on first call."""
    rows = await _select_categories(db, user_id)
    if rows:
        return rows

    try:
        # Concurrent first-time callers — e.g. several PATCHes fired at once by a
        # freshly connected client — all see the empty SELECT above and all try to
        # seed. `uq_email_categories_user_key` lets exactly one win. Same conflict
        # `create_category` guards at its flush, but here the caller asked for
        # get-or-create, so the answer is the winner's rows rather than a 409.
        # The SAVEPOINT scopes the rollback to this seed attempt, leaving the
        # request's own transaction usable.
        async with db.begin_nested():
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
    except IntegrityError:
        # The winner has committed by the time our INSERT is told it conflicts
        # (that is what we blocked on), so the re-read below sees its rows.
        pass

    return await _select_categories(db, user_id)


async def get_category(
    db: AsyncSession, user_id: uuid.UUID, key: str
) -> EmailCategory | None:
    return await db.scalar(
        select(EmailCategory).where(
            EmailCategory.user_id == user_id, EmailCategory.key == key
        )
    )


async def list_rules(db: AsyncSession, user_id: uuid.UUID) -> list[CategorizationRule]:
    return list(
        await db.scalars(
            select(CategorizationRule)
            .where(CategorizationRule.user_id == user_id)
            .order_by(CategorizationRule.priority, CategorizationRule.created_at)
        )
    )


async def get_rule(
    db: AsyncSession, user_id: uuid.UUID, rule_id: uuid.UUID
) -> CategorizationRule | None:
    return await db.scalar(
        select(CategorizationRule).where(
            CategorizationRule.user_id == user_id, CategorizationRule.id == rule_id
        )
    )


async def next_rule_priority(db: AsyncSession, user_id: uuid.UUID) -> int:
    """One past the current maximum, so new rules land at the end."""
    highest = await db.scalar(
        select(func.max(CategorizationRule.priority)).where(
            CategorizationRule.user_id == user_id
        )
    )
    return 0 if highest is None else highest + 1


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


def slugify(display_name: str) -> str:
    """Derive a stable key from a display name: 'Client work' -> 'client_work'."""
    return re.sub(r"[^a-z0-9]+", "_", display_name.strip().casefold()).strip("_")


async def delete_category(
    db: AsyncSession, user_id: uuid.UUID, category: EmailCategory
) -> None:
    """Remove a custom category and everything that pointed at it.

    The Gmail label is deliberately left in place: nothing is stripped from the
    user's mail, so this is non-destructive and undoable by hand.
    """
    await db.execute(
        delete(CategorizationRule).where(
            CategorizationRule.user_id == user_id,
            CategorizationRule.category_key == category.key,
        )
    )
    settings_row = await get_or_create_settings(db, user_id)
    if settings_row.fallback_category_key == category.key:
        settings_row.fallback_category_key = None

    await db.delete(category)
    await db.flush()
