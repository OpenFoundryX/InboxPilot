"""Scheduling links and bookings.

Revision ID: e4c8a21f7d10
Revises: b2e4d7a91c35
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "e4c8a21f7d10"
down_revision = "b2e4d7a91c35"
branch_labels = None
depends_on = None


def timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "scheduling_settings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("slug", sa.String(80), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("timezone", sa.String(64), server_default="UTC", nullable=False),
        sa.Column("duration_minutes", sa.Integer(), server_default="30", nullable=False),
        sa.Column("slot_interval_minutes", sa.Integer(), server_default="15", nullable=False),
        sa.Column("minimum_notice_minutes", sa.Integer(), server_default="120", nullable=False),
        sa.Column("booking_horizon_days", sa.Integer(), server_default="60", nullable=False),
        sa.Column("weekly_hours", postgresql.JSONB(), nullable=False),
        sa.Column("include_link_in_drafts", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("draft_for_proposed_times", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("confirmation_email", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("reschedule_reminders", sa.Boolean(), server_default=sa.true(), nullable=False),
        *timestamps(),
        sa.UniqueConstraint("user_id"),
        sa.UniqueConstraint("slug"),
    )
    op.create_index("ix_scheduling_settings_user_id", "scheduling_settings", ["user_id"])
    op.create_index("ix_scheduling_settings_slug", "scheduling_settings", ["slug"])
    op.create_table(
        "scheduling_bookings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("booker_name", sa.String(200), nullable=False),
        sa.Column("booker_email", sa.String(320), nullable=False),
        sa.Column("attendee_emails", postgresql.JSONB(), nullable=False),
        sa.Column("title", sa.String(300), server_default="Meeting", nullable=False),
        sa.Column("notes", sa.String(2000)),
        sa.Column("status", sa.String(20), server_default="pending", nullable=False),
        sa.Column("calendar_event_id", sa.String(256)),
        sa.Column("meeting_url", sa.String(1024)),
        sa.Column("failure_reason", sa.String(500)),
        *timestamps(),
        sa.UniqueConstraint("user_id", "starts_at", name="uq_scheduling_booking_start"),
    )
    op.create_index("ix_scheduling_bookings_user_id", "scheduling_bookings", ["user_id"])
    op.create_index("ix_scheduling_bookings_starts_at", "scheduling_bookings", ["starts_at"])
    op.create_index("ix_scheduling_bookings_status", "scheduling_bookings", ["status"])


def downgrade() -> None:
    op.drop_table("scheduling_bookings")
    op.drop_table("scheduling_settings")
