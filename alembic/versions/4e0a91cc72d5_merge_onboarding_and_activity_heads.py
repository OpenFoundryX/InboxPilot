"""merge onboarding and activity heads

Revision ID: 4e0a91cc72d5
Revises: 7c001fcd5e99, 2fccf6c64455
Create Date: 2026-07-29 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4e0a91cc72d5'
down_revision: Union[str, None] = ('7c001fcd5e99', '2fccf6c64455')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
