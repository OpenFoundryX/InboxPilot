"""Recurring routines — scheduled jobs InboxPilot runs for a user.

One generic table drives every scheduled "routine" card (briefing, newsletter
digest, chase open threads, reconnect nudges, …). A routine has a type, a local
run time, an optional weekday (for weekly routines), and a free-form config. A
single beat job dispatches due routines to per-type handlers.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin, UUIDMixin

# Routine types (also the dispatcher keys).
ROUTINE_BRIEFING = "briefing"  # daily "what needs your attention" digest
ROUTINE_NEWSLETTER_DIGEST = "newsletter_digest"  # roundup of marketing/newsletters
ROUTINE_CHASE_THREADS = "chase_threads"  # threads you're awaiting a reply on
ROUTINE_RECONNECT = "reconnect"  # nudge to reach out before threads go cold
ROUTINE_DEADLINE_SCAN = "deadline_scan"  # extract deadlines into reminders
ROUTINE_CATCHUP = "catchup"  # digest of important unread
ROUTINE_INVOICES = "invoices"  # summarize recent invoices/receipts
ROUTINE_DOUBLE_BOOKINGS = "double_bookings"  # heads-up when meetings collide


class Routine(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "routines"
    __table_args__ = (
        UniqueConstraint("user_id", "type", name="uq_routine_user_type"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    run_time: Mapped[str] = mapped_column(String(5), default="08:00")  # "HH:MM" local
    # 0=Mon .. 6=Sun for weekly routines; NULL = every day.
    weekday: Mapped[int | None] = mapped_column(nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    config: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
