"""Async DB helpers for Mailman settings, VIP rules, and held emails."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings as app_settings
from models.mailman import MailmanSettings, VipRule


async def get_or_create_settings(db: AsyncSession, user_id: uuid.UUID) -> MailmanSettings:
    row = await db.scalar(select(MailmanSettings).where(MailmanSettings.user_id == user_id))
    if row is None:
        row = MailmanSettings(user_id=user_id, timezone=app_settings.MAILMAN_DEFAULT_TZ)
        db.add(row)
        await db.flush()
    return row


async def get_or_create_vip(db: AsyncSession, user_id: uuid.UUID) -> VipRule:
    row = await db.scalar(select(VipRule).where(VipRule.user_id == user_id))
    if row is None:
        row = VipRule(user_id=user_id, domains=[], addresses=[], keywords=[])
        db.add(row)
        await db.flush()
    return row


async def list_active_settings(db: AsyncSession) -> list[MailmanSettings]:
    result = await db.scalars(select(MailmanSettings).where(MailmanSettings.is_active.is_(True)))
    return list(result)
