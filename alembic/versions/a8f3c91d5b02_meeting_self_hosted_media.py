"""Meeting media we host ourselves: uploads and browser recordings

Adds the two columns the self-hosted media path turns on, and drops the NOT NULL
on `meeting_url` — an uploaded file has no call to join.

Additive and backward compatible. Existing rows keep a non-null `meeting_url`
and a null `media_key`, so every meeting captured so far continues to take the
provider branch exactly as before.

Revision ID: a8f3c91d5b02
Revises: d4e5f6a7b8c9
"""

import sqlalchemy as sa
from alembic import op

revision: str = "a8f3c91d5b02"
down_revision: str | None = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("meetings", "meeting_url", existing_type=sa.Text(), nullable=True)
    op.add_column("meetings", sa.Column("media_key", sa.String(length=512), nullable=True))
    op.add_column(
        "meetings",
        sa.Column("media_confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("meetings", "media_confirmed_at")
    op.drop_column("meetings", "media_key")
    # Rows captured as uploads have no URL to restore, and NOT NULL would reject
    # them. Empty string is what `upsert_from_event` already writes for a
    # calendar event with no link, so it is the shape this column tolerates.
    op.execute("UPDATE meetings SET meeting_url = '' WHERE meeting_url IS NULL")
    op.alter_column("meetings", "meeting_url", existing_type=sa.Text(), nullable=False)
