"""The user's Google grant: OAuth tokens and the Gmail sync cursor.

One row per app user, holding the single grant that covers both Gmail and
Calendar. This is the table that replaced Composio's connected-account store,
and it is now the only record that a user's mailbox is reachable at all.

Tokens are Fernet ciphertext (`core.crypto`), never plaintext.

The *access* token is stored, not just the refresh token, and that is
deliberate: every Celery task runs in a fresh process, so without somewhere
shared to put it, N workers would each mint their own access token on every
task and burn through the refresh endpoint's quota for nothing.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from models.base import Base, TimestampMixin, UUIDMixin

# Scopes the app requests. Login already covers `openid email profile`; these are
# the ones the Connect step adds.
GMAIL_MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
GMAIL_SETTINGS_SCOPE = "https://www.googleapis.com/auth/gmail.settings.basic"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"

# What each half of the product needs, checked per row so one grant can serve
# both while `gmail.is_connected` and `calendar.is_connected` stay separate
# questions with separate answers.
GMAIL_REQUIRED_SCOPES = frozenset({GMAIL_MODIFY_SCOPE, GMAIL_SETTINGS_SCOPE})
CALENDAR_REQUIRED_SCOPES = frozenset({CALENDAR_SCOPE})

CONNECT_SCOPES = (
    "openid",
    "email",
    "profile",
    GMAIL_MODIFY_SCOPE,
    GMAIL_SETTINGS_SCOPE,
    CALENDAR_SCOPE,
)


class GoogleConnection(UUIDMixin, TimestampMixin, Base):
    """A user's Google OAuth grant plus the state needed to poll their mailbox."""

    __tablename__ = "google_connections"

    user_id: Mapped[str] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )

    # The Google account the grant belongs to. Checked against `users.google_sub`
    # at callback time: a user signed in as one account can otherwise complete
    # the consent screen as another, and every later Gmail call would silently
    # operate on the wrong mailbox.
    google_sub: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)

    # Fernet ciphertext. Text rather than String(n) because ciphertext length
    # tracks plaintext length, and Google has changed token sizes before.
    access_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    token_expiry: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Space-joined, exactly as Google returns it. Stored rather than assumed
    # because incremental auth means a user can hold a subset of what we asked
    # for, and a 403 at send time is a much worse way to discover that.
    scopes: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # Gmail's mailbox-wide change cursor, from users.getProfile / users.history.
    # NULL means "not yet seeded" — the poller skips those rows rather than
    # guessing, since a wrong cursor either misses mail or replays it.
    history_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # When Gmail stops pushing for this mailbox unless `users.watch` is called
    # again. Gmail caps a watch at 7 days and there is no auto-renewal, so this
    # column is what the renewal sweep works from. NULL means no watch — the
    # mailbox is then reachable only by the reconciliation poll.
    watch_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Why the grant stopped working, for the reconnect prompt in the UI.
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Set when Google rejects the refresh token (revoked, password change, six
    # months idle, or a still-in-Testing OAuth app). The flag matters more than
    # the exception it accompanies: several callers swallow broad exceptions, so
    # a durable marker is the only thing that reliably takes a dead account out
    # of the poll fan-out and flips its status to disconnected.
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # The poller's fan-out query, run every 60 seconds, is exactly
        # "live rows with a cursor" — so that is what gets the index.
        Index(
            "ix_google_connections_pollable",
            "user_id",
            postgresql_where=revoked_at.is_(None),
        ),
    )

    @property
    def granted_scopes(self) -> frozenset[str]:
        return frozenset(self.scopes.split())

    @property
    def is_live(self) -> bool:
        return self.revoked_at is None

    def has_scopes(self, required: frozenset[str]) -> bool:
        return self.is_live and required <= self.granted_scopes

    def __repr__(self) -> str:
        return f"<GoogleConnection {self.email}{' revoked' if self.revoked_at else ''}>"
