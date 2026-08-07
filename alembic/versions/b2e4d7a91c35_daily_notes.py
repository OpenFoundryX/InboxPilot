"""Daily notes — one scratchpad per calendar day

Revision ID: b2e4d7a91c35
Revises: a8f3c91d5b02
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b2e4d7a91c35"
down_revision: str | None = "a8f3c91d5b02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "daily_notes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("note_date", sa.Date(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        # server_default matters: TimestampMixin declares these as DB-side
        # defaults, so SQLAlchemy leaves them out of the INSERT entirely. Without
        # the default here, every insert would fail the NOT NULL.
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("user_id", "note_date", name="uq_daily_note_user_date"),
    )
    op.create_index("ix_daily_notes_user_id", "daily_notes", ["user_id"])
    # The page always reads a contiguous window of days for one user, so the
    # range scan is what this table is asked for; a bare date index would still
    # cross every user's rows.
    op.create_index("ix_daily_notes_user_date", "daily_notes", ["user_id", "note_date"])


def downgrade() -> None:
    op.drop_index("ix_daily_notes_user_date", table_name="daily_notes")
    op.drop_index("ix_daily_notes_user_id", table_name="daily_notes")
    op.drop_table("daily_notes")
