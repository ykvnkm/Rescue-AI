"""Create operational Postgres tables for mission state."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "202603140001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "missions",
        sa.Column("mission_id", sa.Text(), nullable=False),
        sa.Column("source_name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("total_frames", sa.Integer(), nullable=False),
        sa.Column("fps", sa.Float(), nullable=False),
        sa.Column("completed_frame_id", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("mission_id"),
    )
    op.create_index("ix_missions_status", "missions", ["status"], unique=False)

    op.create_table(
        "frame_events",
        sa.Column("mission_id", sa.Text(), nullable=False),
        sa.Column("frame_id", sa.Integer(), nullable=False),
        sa.Column("ts_sec", sa.Float(), nullable=False),
        sa.Column("image_uri", sa.Text(), nullable=False),
        sa.Column("gt_person_present", sa.Boolean(), nullable=False),
        sa.Column("gt_episode_id", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["mission_id"],
            ["missions.mission_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("mission_id", "frame_id"),
    )
    op.create_index(
        "ix_frame_events_mission_ts",
        "frame_events",
        ["mission_id", "ts_sec"],
        unique=False,
    )

    op.create_table(
        "alerts",
        sa.Column("alert_id", sa.Text(), nullable=False),
        sa.Column("mission_id", sa.Text(), nullable=False),
        sa.Column("frame_id", sa.Integer(), nullable=False),
        sa.Column("ts_sec", sa.Float(), nullable=False),
        sa.Column("image_uri", sa.Text(), nullable=False),
        sa.Column("people_detected", sa.Integer(), nullable=False),
        sa.Column(
            "primary_bbox", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("primary_score", sa.Float(), nullable=False),
        sa.Column("primary_label", sa.Text(), nullable=False),
        sa.Column("primary_model_name", sa.Text(), nullable=False),
        sa.Column("primary_explanation", sa.Text(), nullable=True),
        sa.Column(
            "detections", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reviewed_by", sa.Text(), nullable=True),
        sa.Column("reviewed_at_sec", sa.Float(), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["mission_id"],
            ["missions.mission_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["mission_id", "frame_id"],
            ["frame_events.mission_id", "frame_events.frame_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("alert_id"),
    )
    op.create_index(
        "ix_alerts_mission_ts",
        "alerts",
        ["mission_id", "ts_sec"],
        unique=False,
    )
    op.create_index(
        "ix_alerts_mission_status",
        "alerts",
        ["mission_id", "status"],
        unique=False,
    )

    op.create_table(
        "episodes",
        sa.Column("mission_id", sa.Text(), nullable=False),
        sa.Column("episode_index", sa.Integer(), nullable=False),
        sa.Column("start_sec", sa.Float(), nullable=False),
        sa.Column("end_sec", sa.Float(), nullable=False),
        sa.Column(
            "found_by_alert",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.ForeignKeyConstraint(
            ["mission_id"],
            ["missions.mission_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("mission_id", "episode_index"),
    )
    op.create_index(
        "ix_episodes_mission_found",
        "episodes",
        ["mission_id", "found_by_alert"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_episodes_mission_found", table_name="episodes")
    op.drop_table("episodes")

    op.drop_index("ix_alerts_mission_status", table_name="alerts")
    op.drop_index("ix_alerts_mission_ts", table_name="alerts")
    op.drop_table("alerts")

    op.drop_index("ix_frame_events_mission_ts", table_name="frame_events")
    op.drop_table("frame_events")

    op.drop_index("ix_missions_status", table_name="missions")
    op.drop_table("missions")
