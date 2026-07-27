"""meetings and meeting_settings tables

Revision ID: a3f1c2d84e17
Revises: 87b738ba57db
Create Date: 2026-07-26 16:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'a3f1c2d84e17'
down_revision: Union[str, None] = '87b738ba57db'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'meetings',
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('source', sa.String(length=16), nullable=False),
        sa.Column('calendar_event_id', sa.String(length=256), nullable=True),
        sa.Column('title', sa.String(length=300), nullable=True),
        sa.Column('meeting_url', sa.Text(), nullable=False),
        sa.Column('platform', sa.String(length=16), nullable=True),
        sa.Column('starts_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('ends_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('attendees', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('status_detail', sa.String(length=200), nullable=True),
        sa.Column('bot_id', sa.String(length=128), nullable=True),
        sa.Column('recording_id', sa.String(length=128), nullable=True),
        sa.Column('joined_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('transcript', sa.Text(), nullable=True),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('decisions', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('action_items', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('recap_sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        # One bot per calendar event. NULL event ids (ad-hoc joins) are distinct
        # in Postgres, so a user can have many of those.
        sa.UniqueConstraint('user_id', 'calendar_event_id', name='uq_meeting_user_event'),
    )
    op.create_index(op.f('ix_meetings_bot_id'), 'meetings', ['bot_id'], unique=False)
    op.create_index(op.f('ix_meetings_starts_at'), 'meetings', ['starts_at'], unique=False)
    op.create_index(op.f('ix_meetings_status'), 'meetings', ['status'], unique=False)
    op.create_index(op.f('ix_meetings_user_id'), 'meetings', ['user_id'], unique=False)

    op.create_table(
        'meeting_settings',
        sa.Column('user_id', sa.UUID(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('auto_join', sa.Boolean(), nullable=False),
        sa.Column('bot_name', sa.String(length=64), nullable=False),
        sa.Column('min_attendees', sa.Integer(), nullable=False),
        sa.Column('skip_titles', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column('lookahead_minutes', sa.Integer(), nullable=False),
        sa.Column('email_recap', sa.Boolean(), nullable=False),
        sa.Column('create_reminders', sa.Boolean(), nullable=False),
        sa.Column('include_in_digest', sa.Boolean(), nullable=False),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_meeting_settings_user_id'), 'meeting_settings', ['user_id'], unique=True
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_meeting_settings_user_id'), table_name='meeting_settings')
    op.drop_table('meeting_settings')
    op.drop_index(op.f('ix_meetings_user_id'), table_name='meetings')
    op.drop_index(op.f('ix_meetings_status'), table_name='meetings')
    op.drop_index(op.f('ix_meetings_starts_at'), table_name='meetings')
    op.drop_index(op.f('ix_meetings_bot_id'), table_name='meetings')
    op.drop_table('meetings')
