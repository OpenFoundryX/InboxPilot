"""invited_emails

Revision ID: a7c3e1d90f42
Revises: b3e9d4712a06
Create Date: 2026-08-24

The signup allowlist. Schema only — the backfill of existing accounts is a
separate revision (c8e2f4a10b57) so that this one is a pure structural change
and can be reasoned about on its own.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "a7c3e1d90f42"
down_revision: str | None = "b3e9d4712a06"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "invited_emails",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("note", sa.String(255), nullable=True),
        sa.Column(
            "invited_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "claimed_by_user_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    # Unique, not just indexed: the gate asks "is this mailbox invited" and two
    # rows for one mailbox would mean two answers.
    op.create_index("ix_invited_emails_email", "invited_emails", ["email"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_invited_emails_email", table_name="invited_emails")
    op.drop_table("invited_emails")
