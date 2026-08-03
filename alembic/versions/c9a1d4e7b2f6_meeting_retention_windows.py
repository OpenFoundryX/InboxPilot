"""meeting retention windows (grandfathering)

Revision ID: c9a1d4e7b2f6
Revises: b3c7f2a9e1d4
"""

import sqlalchemy as sa
from alembic import op

revision: str = "c9a1d4e7b2f6"
down_revision: str | None = "b3c7f2a9e1d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The resolved day-counts in force when a meeting was captured, not a plan
    # id — so a later edit to what a plan *means* in code can't retroactively
    # move an already-fixed deadline. Null on every existing row; the prune
    # falls back to the current plan for those, since there is nothing to
    # grandfather them against.
    op.add_column("meetings", sa.Column("retention_video_days", sa.Integer(), nullable=True))
    op.add_column(
        "meetings", sa.Column("retention_transcript_days", sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("meetings", "retention_transcript_days")
    op.drop_column("meetings", "retention_video_days")
