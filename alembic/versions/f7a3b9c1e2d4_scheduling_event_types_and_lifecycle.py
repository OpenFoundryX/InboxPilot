"""Event types, availability overrides, and the booking lifecycle.

Revision ID: f7a3b9c1e2d4
Revises: e4c8a21f7d10

Splits the single booking link introduced in e4c8a21f7d10 into a profile plus
one or more event types, and gives bookings the columns cancel/reschedule need.

This is a second revision rather than an edit to e4c8a21f7d10 because that one
has already been applied. Rewriting an applied migration leaves every database
that ran the old version stamped with a revision id whose file no longer
describes what is actually in the schema — alembic then skips it forever and
the tables silently disagree with the models.

Existing rows are carried forward, not dropped:
  * each profile gets an event type built from the per-profile duration and
    notice settings it used to carry, so its link keeps working;
  * existing bookings are attached to that event type and issued the
    management token the new cancel/reschedule flow authenticates with.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f7a3b9c1e2d4"
down_revision = "e4c8a21f7d10"
branch_labels = None
depends_on = None


def timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def upgrade() -> None:
    # The overlap constraint at the end excludes on (uuid =, tstzrange &&) in a
    # single GiST index. Without btree_gist there is no operator class for the
    # scalar equality half of that.
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")

    # ---------------------------------------------------------------- new tables
    op.create_table(
        "scheduling_event_types",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("slug", sa.String(80), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("position", sa.Integer(), server_default="0", nullable=False),
        sa.Column("duration_minutes", sa.Integer(), server_default="30", nullable=False),
        sa.Column("slot_interval_minutes", sa.Integer(), server_default="15", nullable=False),
        sa.Column("minimum_notice_minutes", sa.Integer(), server_default="120", nullable=False),
        sa.Column("booking_horizon_days", sa.Integer(), server_default="60", nullable=False),
        sa.Column("buffer_before_minutes", sa.Integer(), server_default="0", nullable=False),
        sa.Column("buffer_after_minutes", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_bookings_per_day", sa.Integer(), nullable=True),
        sa.Column("questions", postgresql.JSONB(), server_default="[]", nullable=False),
        *timestamps(),
        sa.UniqueConstraint("user_id", "slug", name="uq_event_type_slug_per_user"),
    )
    op.create_index("ix_scheduling_event_types_user_id", "scheduling_event_types", ["user_id"])

    op.create_table(
        "scheduling_date_overrides",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("windows", postgresql.JSONB(), server_default="[]", nullable=False),
        sa.Column("note", sa.String(200), nullable=True),
        *timestamps(),
        sa.UniqueConstraint("user_id", "day", name="uq_date_override_per_user"),
    )
    op.create_index("ix_scheduling_date_overrides_user_id", "scheduling_date_overrides", ["user_id"])

    # ------------------------------------------ carry each profile's link forward
    # One event type per existing profile, built from the settings that used to
    # live on the profile itself. Without this an upgraded account would open
    # the dashboard to no bookable meeting types and a link that 404s.
    op.execute(
        """
        INSERT INTO scheduling_event_types (
            id, user_id, slug, name, duration_minutes, slot_interval_minutes,
            minimum_notice_minutes, booking_horizon_days, questions
        )
        SELECT
            gen_random_uuid(),
            s.user_id,
            'meeting',
            s.duration_minutes || ' Minute Meeting',
            s.duration_minutes,
            s.slot_interval_minutes,
            s.minimum_notice_minutes,
            s.booking_horizon_days,
            '[]'::jsonb
        FROM scheduling_settings s
        """
    )

    # --------------------------------------------------------------- bookings
    op.add_column(
        "scheduling_bookings",
        sa.Column("event_type_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_scheduling_bookings_event_type",
        "scheduling_bookings",
        "scheduling_event_types",
        ["event_type_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_scheduling_bookings_event_type_id", "scheduling_bookings", ["event_type_id"])

    op.add_column("scheduling_bookings", sa.Column("answers", postgresql.JSONB(), server_default="{}", nullable=False))
    op.add_column("scheduling_bookings", sa.Column("management_token", sa.String(64), nullable=True))
    op.add_column("scheduling_bookings", sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("scheduling_bookings", sa.Column("cancelled_by", sa.String(10), nullable=True))
    op.add_column("scheduling_bookings", sa.Column("cancel_reason", sa.String(500), nullable=True))
    op.add_column("scheduling_bookings", sa.Column("rescheduled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("scheduling_bookings", sa.Column("reminder_sent_at", sa.DateTime(timezone=True), nullable=True))

    # Point existing bookings at their host's migrated event type.
    op.execute(
        """
        UPDATE scheduling_bookings b
           SET event_type_id = e.id
          FROM scheduling_event_types e
         WHERE e.user_id = b.user_id AND e.slug = 'meeting'
        """
    )

    # Issue a token to every existing booking, so links mailed out after this
    # deploy work for meetings booked before it. Two UUIDs give 64 hex chars,
    # which is the column width and well past guessable.
    op.execute(
        """
        UPDATE scheduling_bookings
           SET management_token = replace(gen_random_uuid()::text, '-', '')
                               || replace(gen_random_uuid()::text, '-', '')
         WHERE management_token IS NULL
        """
    )
    op.alter_column("scheduling_bookings", "management_token", nullable=False)
    op.create_unique_constraint(
        "uq_scheduling_booking_token", "scheduling_bookings", ["management_token"]
    )

    # `notes` outgrew a bounded VARCHAR the moment it started carrying anything
    # a guest types freely.
    op.alter_column(
        "scheduling_bookings",
        "notes",
        type_=sa.Text(),
        existing_type=sa.String(2000),
        existing_nullable=True,
    )

    # The old guard only caught two bookings sharing a start time, so 09:00 and
    # 09:15 of a 30-minute meeting both passed. It also spanned every status,
    # which meant a cancelled slot could never be rebooked. Replaced by a range
    # exclusion scoped to live bookings.
    op.drop_constraint("uq_scheduling_booking_start", "scheduling_bookings", type_="unique")
    op.execute(
        """
        ALTER TABLE scheduling_bookings
        ADD CONSTRAINT scheduling_bookings_no_overlap
        EXCLUDE USING gist (
            user_id WITH =,
            tstzrange(starts_at, ends_at) WITH &&
        ) WHERE (status IN ('pending', 'confirmed'))
        """
    )

    # --------------------------------------------------------------- profile
    # These moved to the event type above; the profile keeps only what is true
    # of the person rather than of a particular meeting.
    for column in (
        "duration_minutes",
        "slot_interval_minutes",
        "minimum_notice_minutes",
        "booking_horizon_days",
        "draft_for_proposed_times",
    ):
        op.drop_column("scheduling_settings", column)

    op.alter_column(
        "scheduling_settings", "weekly_hours", server_default="[]", existing_type=postgresql.JSONB()
    )
    # Redundant with the UNIQUE constraints already on these columns.
    op.drop_index("ix_scheduling_settings_user_id", table_name="scheduling_settings")
    op.drop_index("ix_scheduling_settings_slug", table_name="scheduling_settings")


def downgrade() -> None:
    op.create_index("ix_scheduling_settings_slug", "scheduling_settings", ["slug"])
    op.create_index("ix_scheduling_settings_user_id", "scheduling_settings", ["user_id"])
    op.add_column("scheduling_settings", sa.Column("draft_for_proposed_times", sa.Boolean(), server_default=sa.true(), nullable=False))
    op.add_column("scheduling_settings", sa.Column("booking_horizon_days", sa.Integer(), server_default="60", nullable=False))
    op.add_column("scheduling_settings", sa.Column("minimum_notice_minutes", sa.Integer(), server_default="120", nullable=False))
    op.add_column("scheduling_settings", sa.Column("slot_interval_minutes", sa.Integer(), server_default="15", nullable=False))
    op.add_column("scheduling_settings", sa.Column("duration_minutes", sa.Integer(), server_default="30", nullable=False))

    op.execute("ALTER TABLE scheduling_bookings DROP CONSTRAINT scheduling_bookings_no_overlap")
    # Deduplicate first: the old unique constraint is stricter than what the
    # exclusion constraint allowed, so back-to-back bookings that were legal
    # under it would block this from being recreated.
    op.execute(
        """
        DELETE FROM scheduling_bookings a
         USING scheduling_bookings b
         WHERE a.user_id = b.user_id AND a.starts_at = b.starts_at AND a.ctid > b.ctid
        """
    )
    op.create_unique_constraint(
        "uq_scheduling_booking_start", "scheduling_bookings", ["user_id", "starts_at"]
    )
    op.alter_column(
        "scheduling_bookings",
        "notes",
        type_=sa.String(2000),
        existing_type=sa.Text(),
        existing_nullable=True,
    )
    op.drop_constraint("uq_scheduling_booking_token", "scheduling_bookings", type_="unique")
    for column in (
        "reminder_sent_at",
        "rescheduled_at",
        "cancel_reason",
        "cancelled_by",
        "cancelled_at",
        "management_token",
        "answers",
    ):
        op.drop_column("scheduling_bookings", column)
    op.drop_index("ix_scheduling_bookings_event_type_id", table_name="scheduling_bookings")
    op.drop_constraint("fk_scheduling_bookings_event_type", "scheduling_bookings", type_="foreignkey")
    op.drop_column("scheduling_bookings", "event_type_id")

    op.drop_table("scheduling_date_overrides")
    op.drop_table("scheduling_event_types")
