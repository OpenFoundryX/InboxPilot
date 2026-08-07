"""Daily notes — one scratchpad per calendar day.

Everything else in the product holds a thought attached to something: an email,
a draft, a meeting. This is where the rest goes.

Keyed on `(user_id, note_date)`, so a day is a square on a calendar rather than
a span of time. That distinction is the whole reason `note_date` is a `Date` and
not a `DateTime`: an instant would immediately raise the question of whose
midnight bounds it, and the answer would have to be recomputed against a
timezone on every read. The client names the day it is writing on, and the
server stores exactly that.

A day with nothing written has no row. Empty bodies are deleted rather than
stored, because the page is scrolled through far more often than it is typed
into — persisting a blank per day visited would fill the table with rows that
mean nothing.
"""

import uuid
from datetime import date

from sqlalchemy import Date, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin, UUIDMixin


class DailyNote(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "daily_notes"
    __table_args__ = (
        UniqueConstraint("user_id", "note_date", name="uq_daily_note_user_date"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    #: The calendar day this page belongs to, as the user's own browser named it.
    note_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    #: Plain text. Never empty — a note emptied out is deleted instead.
    body: Mapped[str] = mapped_column(Text, nullable=False)
