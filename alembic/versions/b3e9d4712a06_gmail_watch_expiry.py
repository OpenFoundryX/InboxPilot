"""Track when each mailbox's Gmail push watch expires.

Gmail caps `users.watch` at 7 days and offers no auto-renewal, so the renewal
sweep needs somewhere to read the deadline from. NULL means no active watch;
that mailbox falls back to the reconciliation poll.

Revision ID: b3e9d4712a06
Revises: d5b71e9a3c82
"""

import sqlalchemy as sa
from alembic import op

revision = "b3e9d4712a06"
down_revision = "d5b71e9a3c82"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "google_connections",
        sa.Column("watch_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    # The renewal sweep asks exactly one question — "which live watches are
    # about to lapse" — so that is the shape of the index.
    op.create_index(
        "ix_google_connections_watch_expiry",
        "google_connections",
        ["watch_expires_at"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_google_connections_watch_expiry", table_name="google_connections")
    op.drop_column("google_connections", "watch_expires_at")
