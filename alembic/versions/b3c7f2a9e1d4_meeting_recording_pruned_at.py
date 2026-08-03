"""meeting recording_pruned_at

Revision ID: b3c7f2a9e1d4
Revises: f1a2b3c4d5e6
"""

import sqlalchemy as sa
from alembic import op

revision: str = "b3c7f2a9e1d4"
down_revision: str | None = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A marker, not a cache: set once by retention pruning and never cleared,
    # so a re-resolve can tell "deliberately removed" apart from "never had a
    # recording" — both look like `recording_id IS NULL` otherwise.
    op.add_column(
        "meetings",
        sa.Column("recording_pruned_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("meetings", "recording_pruned_at")
