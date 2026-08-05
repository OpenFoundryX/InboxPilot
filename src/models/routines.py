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

from core.routine_slugs import (
    ROUTINE_BRIEFING as ROUTINE_BRIEFING,
)
from core.routine_slugs import (
    ROUTINE_CATCHUP as ROUTINE_CATCHUP,
)
from core.routine_slugs import (
    ROUTINE_CHASE_THREADS as ROUTINE_CHASE_THREADS,
)
from core.routine_slugs import (
    ROUTINE_DEADLINE_SCAN as ROUTINE_DEADLINE_SCAN,
)
from core.routine_slugs import (
    ROUTINE_DOUBLE_BOOKINGS as ROUTINE_DOUBLE_BOOKINGS,
)
from core.routine_slugs import (
    ROUTINE_INVOICES as ROUTINE_INVOICES,
)
from core.routine_slugs import (
    ROUTINE_NEWSLETTER_DIGEST as ROUTINE_NEWSLETTER_DIGEST,
)
from core.routine_slugs import (
    ROUTINE_RECONNECT as ROUTINE_RECONNECT,
)
from core.routine_slugs import (
    ROUTINE_SCHEDULE_TRUSTED as ROUTINE_SCHEDULE_TRUSTED,
)
from models.base import Base, TimestampMixin, UUIDMixin

# The slugs above are re-exported from `core.routine_slugs`, which holds them so
# that `core.plans` can name them without `core` importing `models` — see that
# module for why the cycle that created mattered.


class Routine(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "routines"
    __table_args__ = (UniqueConstraint("user_id", "type", name="uq_routine_user_type"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    run_time: Mapped[str] = mapped_column(String(5), default="08:00")  # "HH:MM" local

    weekday: Mapped[int | None] = mapped_column(nullable=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    config: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)
