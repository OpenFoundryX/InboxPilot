"""Mailman (batched delivery) models.

Mailman holds incoming Gmail out of the inbox and releases it in scheduled
batches. Three tables back it: per-user settings (the delivery schedule + DND),
per-user VIP rules (senders/keywords that bypass the hold), and a log of held
messages so the scheduler knows what to release.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin, UUIDMixin


MODE_INTERVAL = "interval"
MODE_TIMES = "times"
MODE_CUSTOM_DAILY = "custom_daily"


class MailmanSettings(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "mailman_settings"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)

    delivery_mode: Mapped[str] = mapped_column(String(32), default=MODE_TIMES, nullable=False)
    interval_hours: Mapped[int | None] = mapped_column(nullable=True)
    interval_minutes: Mapped[int | None] = mapped_column(nullable=True)

    times_per_day: Mapped[int | None] = mapped_column(nullable=True, default=3)
    custom_times: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    active_window_start: Mapped[str] = mapped_column(String(5), default="09:00")
    active_window_end: Mapped[str] = mapped_column(String(5), default="21:00")

    dnd_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    dnd_start: Mapped[str | None] = mapped_column(String(5), nullable=True)
    dnd_end: Mapped[str | None] = mapped_column(String(5), nullable=True)

    last_delivery_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    gmail_filter_id: Mapped[str | None] = mapped_column(String(128), nullable=True)


class VipRule(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "mailman_vip_rules"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True)
    domains: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    addresses: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    keywords: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
