"""
Append-only record of what InboxPilot did for a user.

One row per thing worth counting on the dashboard: a message labelled, a reply
drafted. Append-only on purpose — the dashboard reads aggregates today, but a
log leaves the door open to a per-day or per-category breakdown later without
needing a backfill to answer the question.

The unique constraint is what makes the counts trustworthy rather than
decorative. `classify.new_email` declares `max_retries=3`, and
`jobs.sync_last_7_days` is documented as the safe-to-re-run catch-up lever — so
without it, one retry storm or one manual catch-up permanently inflates the
number the user reads on the dashboard.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin, UUIDMixin

KIND_EMAIL_CATEGORIZED = "email_categorized"
KIND_DRAFT_CREATED = "draft_created"
KINDS = frozenset({KIND_EMAIL_CATEGORIZED, KIND_DRAFT_CREATED})


class ActivityEvent(UUIDMixin, TimestampMixin, Base):
    """One dashboard-countable thing that happened, at most once per `ref_id`."""

    __tablename__ = "activity_events"
    __table_args__ = (
        UniqueConstraint("user_id", "kind", "ref_id", name="uq_activity_events_user_kind_ref"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), index=True, nullable=False)

    # The Gmail id that makes this event unique: the categorized message for
    # `email_categorized`, and the *source* message for `draft_created` — one
    # email replied to counts once, even if a re-run leaves a second draft
    # object behind in the mailbox.
    ref_id: Mapped[str] = mapped_column(String(128), nullable=False)

    # References EmailCategory.key. Not an FK, for the same reason
    # CategorizationRule.category_key is not one: `key` is unique only per user,
    # so this would need a composite (user_id, key) target. Unlike the rules
    # table it is also never cleaned up when a category is deleted — these rows
    # record what happened, and rewriting history to match a renamed taxonomy
    # would be a lie.
    category_key: Mapped[str | None] = mapped_column(String(64), nullable=True)

    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True, nullable=False
    )

    def __repr__(self) -> str:
        return f"<ActivityEvent {self.kind} {self.ref_id}>"
