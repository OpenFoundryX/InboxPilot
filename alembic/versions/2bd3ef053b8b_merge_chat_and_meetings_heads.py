"""merge chat and meetings heads

Revision ID: 2bd3ef053b8b
Revises: a1c4e77b93d2, a3f1c2d84e17
Create Date: 2026-07-27 07:47:46.695707

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2bd3ef053b8b'
down_revision: Union[str, None] = ('a1c4e77b93d2', 'a3f1c2d84e17')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
