"""Google OAuth grants and the Gmail sync cursor.

Replaces Composio's connected-account store: one row per user holding the single
grant that covers both Gmail and Calendar, plus the mailbox history cursor the
poller walks.

Purely additive — nothing reads this table yet.

Revision ID: d5b71e9a3c82
Revises: f7a3b9c1e2d4
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "d5b71e9a3c82"
down_revision = "f7a3b9c1e2d4"
branch_labels = None
depends_on = None


def timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "google_connections",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("google_sub", sa.String(255), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        # Fernet ciphertext; Text because token lengths are Google's to change.
        sa.Column("access_token", sa.Text()),
        sa.Column("refresh_token", sa.Text(), nullable=False),
        sa.Column("token_expiry", sa.DateTime(timezone=True)),
        sa.Column("scopes", sa.Text(), server_default="", nullable=False),
        sa.Column("history_id", sa.String(32)),
        sa.Column("last_polled_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.String(500)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        *timestamps(),
        sa.UniqueConstraint("user_id", name="uq_google_connections_user"),
    )
    op.create_index("ix_google_connections_user_id", "google_connections", ["user_id"])
    # The poller's every-60s fan-out is exactly "live rows", so the partial index
    # is the one that matters as revoked rows accumulate.
    op.create_index(
        "ix_google_connections_pollable",
        "google_connections",
        ["user_id"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_table("google_connections")
