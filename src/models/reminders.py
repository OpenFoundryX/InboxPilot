"""Reminders — things to resurface to the user at a future time.

Created explicitly ("remind me about X tomorrow at 3pm") or derived from a
deadline the assistant spotted in a message. A beat job delivers due reminders
(threaded into the source conversation when there is one, else a fresh email).
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin, UUIDMixin

ORIGIN_MANUAL = "manual"  # user asked for it
ORIGIN_DEADLINE = "deadline"  # auto-extracted from a message
ORIGIN_MEETING = "meeting"  # a commitment the notetaker heard in a meeting


class Reminder(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "reminders"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    remind_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    thread_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    origin: Mapped[str] = mapped_column(String(16), default=ORIGIN_MANUAL, nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
