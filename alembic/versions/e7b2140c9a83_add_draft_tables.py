"""add draft settings and files tables

Revision ID: e7b2140c9a83
Revises: d5a7f0c31e94
Create Date: 2026-07-30 16:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'e7b2140c9a83'
down_revision: Union[str, None] = 'd5a7f0c31e94'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('draft_settings',
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('is_enabled', sa.Boolean(), nullable=False),
    sa.Column('category_keys', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('selectivity', sa.String(length=32), nullable=False),
    sa.Column('tone', sa.String(length=32), nullable=False),
    sa.Column('length', sa.String(length=16), nullable=False),
    sa.Column('custom_instructions_enabled', sa.Boolean(), nullable=False),
    sa.Column('custom_instructions', sa.Text(), nullable=True),
    sa.Column('signature_enabled', sa.Boolean(), nullable=False),
    sa.Column('signature', sa.Text(), nullable=True),
    sa.Column('follow_up_enabled', sa.Boolean(), nullable=False),
    sa.Column('follow_up_days', sa.Integer(), nullable=False),
    sa.Column('model', sa.String(length=64), nullable=True),
    sa.Column('last_sweep_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_follow_up_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_draft_settings_user_id'), 'draft_settings', ['user_id'], unique=True)

    op.create_table('draft_files',
    sa.Column('user_id', sa.UUID(), nullable=False),
    sa.Column('purpose', sa.String(length=16), nullable=False),
    sa.Column('filename', sa.String(length=255), nullable=False),
    sa.Column('content_type', sa.String(length=128), nullable=False),
    sa.Column('size_bytes', sa.Integer(), nullable=False),
    sa.Column('extracted_text', sa.Text(), nullable=False),
    sa.Column('char_count', sa.Integer(), nullable=False),
    sa.Column('is_enabled', sa.Boolean(), nullable=False),
    sa.Column('id', sa.UUID(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_draft_files_user_id'), 'draft_files', ['user_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_draft_files_user_id'), table_name='draft_files')
    op.drop_table('draft_files')
    op.drop_index(op.f('ix_draft_settings_user_id'), table_name='draft_settings')
    op.drop_table('draft_settings')
