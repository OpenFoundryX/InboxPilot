"""meeting recording url cache

Revision ID: d5a7f0c31e94
Revises: 4e0a91cc72d5
Create Date: 2026-07-30 11:20:04.118392

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd5a7f0c31e94'
down_revision: Union[str, None] = '4e0a91cc72d5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Both columns are one value: a provider link and the moment it stops
    # working. Backfilling is pointless — every link old enough to be in this
    # table has already expired, so existing rows re-resolve on first read.
    op.add_column("meetings", sa.Column("recording_url", sa.Text(), nullable=True))
    op.add_column(
        "meetings",
        sa.Column("recording_url_expires_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("meetings", "recording_url_expires_at")
    op.drop_column("meetings", "recording_url")
