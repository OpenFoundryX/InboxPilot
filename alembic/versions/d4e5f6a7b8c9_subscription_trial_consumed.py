"""subscriptions.trial_consumed marker

Revision ID: d4e5f6a7b8c9
Revises: c9a1d4e7b2f6
"""

import sqlalchemy as sa
from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "c9a1d4e7b2f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "subscriptions",
        sa.Column("trial_consumed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # Every row that already exists represents a trial already granted, either
    # by the original billing backfill migration or by a completed checkout —
    # so mark every one of them consumed. Without this, the very next
    # checkout each of these users runs would read `trial_consumed = false`
    # (the column's own default) and be handed a second, full-length trial —
    # the exact bug this column exists to close, reintroduced for every
    # account that predates it.
    op.execute(sa.text("UPDATE subscriptions SET trial_consumed = true"))


def downgrade() -> None:
    op.drop_column("subscriptions", "trial_consumed")
