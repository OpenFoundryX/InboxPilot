"""The signup allowlist.

Signups are closed while the first ~100 customers are onboarded by hand, and
signup is not a separate code path from login — both are the Google OAuth
callback — so the allowlist is what tells the two apart. A row here is
permission for one mailbox to become a user, once.

`email` is stored lowercased. Google returns the address as the user's provider
spells it, so `Nilesh@X.com` and `nilesh@x.com` are the same mailbox and must
not be two rows; `services.auth.invites.normalize_email` is the only thing that
should ever write this column.

`invited_at` is deliberately not `created_at`. `created_at` is an audit
timestamp that moves if a row is ever recreated or corrected; "when did we
decide to invite this person" is a product fact we want in a funnel query.

`claimed_at` being null is the whole definition of "unclaimed" — `revoke` reads
it to decide whether deleting the row is safe. It is a *record*, not a lock:
existing users always pass the gate, so clearing it would not remove anyone's
access.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin, UUIDMixin


class InvitedEmail(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "invited_emails"

    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)

    invited_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # SET NULL, not CASCADE: deleting a user must not delete the record that
    # this slot was used. CASCADE would silently free the slot and lose the
    # history of how that person got in.
    claimed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    def __repr__(self) -> str:
        state = "claimed" if self.claimed_at else "open"
        return f"<InvitedEmail {self.email} {state}>"
