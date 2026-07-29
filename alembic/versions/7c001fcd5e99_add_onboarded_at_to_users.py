"""add onboarded_at to users

Revision ID: 7c001fcd5e99
Revises: bb9e0a302824
Create Date: 2026-07-28 13:47:10.893006

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7c001fcd5e99'
down_revision: Union[str, None] = 'bb9e0a302824'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("onboarded_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Existing users already connected both accounts under the old flow. Treat
    # them as onboarded so nobody in the middle of using the product is pulled
    # into the new wizard; they configure these features from the dashboard.
    op.execute("UPDATE users SET onboarded_at = now()")


def downgrade() -> None:
    op.drop_column("users", "onboarded_at")
