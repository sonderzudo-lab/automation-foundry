"""Add the legacy Content Engine schema to the managed baseline.

Revision ID: 20260810_0014
Revises: 20260810_0013
Create Date: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260810_0014"
down_revision: str | None = "20260810_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "channels",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("niche", sa.Text(), nullable=False),
        sa.Column("platform", sa.String(length=20), nullable=False),
        sa.Column("language", sa.String(length=10), nullable=False),
        sa.Column("persona", sa.Text(), nullable=False),
        sa.Column("cloud_project_id", sa.Text(), nullable=True),
        sa.Column("oauth_refresh_token", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "videos",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("niche_angle", sa.Text(), nullable=True),
        sa.Column("topic_source", sa.String(length=50), nullable=True),
        sa.Column("format", sa.String(length=10), nullable=False),
        sa.Column("script_path", sa.Text(), nullable=True),
        sa.Column("audio_path", sa.Text(), nullable=True),
        sa.Column("video_path", sa.Text(), nullable=True),
        sa.Column("thumbnail_path", sa.Text(), nullable=True),
        sa.Column("platform_video_id", sa.Text(), nullable=True),
        sa.Column("ai_disclosure", sa.Boolean(), nullable=False),
        sa.Column("similarity_score", sa.Float(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["channel_id"], ["channels.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_videos_channel_status",
        "videos",
        ["channel_id", "status"],
    )
    op.create_table(
        "topics",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=True),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("raw_data", sa.JSON(), nullable=True),
        sa.Column("potential_score", sa.Float(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["channel_id"], ["channels.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_topics_channel_status",
        "topics",
        ["channel_id", "status"],
    )
    op.create_table(
        "alerts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column("type", sa.String(length=30), nullable=False),
        sa.Column("severity", sa.String(length=20), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("acknowledged", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["channel_id"], ["channels.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_alerts_channel_acknowledged",
        "alerts",
        ["channel_id", "acknowledged"],
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("video_id", sa.Integer(), nullable=False),
        sa.Column("stage", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("worker", sa.String(length=100), nullable=True),
        sa.Column("celery_task_id", sa.String(length=255), nullable=True),
        sa.Column("error_msg", sa.Text(), nullable=True),
        sa.Column("retries", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["video_id"], ["videos.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_jobs_video_stage", "jobs", ["video_id", "stage"])
    op.create_table(
        "metrics",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("video_id", sa.Integer(), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("views", sa.Integer(), nullable=True),
        sa.Column("watch_time_min", sa.Integer(), nullable=True),
        sa.Column("avg_view_duration", sa.Float(), nullable=True),
        sa.Column("ctr", sa.Float(), nullable=True),
        sa.Column("estimated_revenue", sa.Float(), nullable=True),
        sa.Column("cpm", sa.Float(), nullable=True),
        sa.Column("monetized_playbacks", sa.Integer(), nullable=True),
        sa.Column("subscribers_gained", sa.Integer(), nullable=True),
        sa.Column("collected_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["video_id"], ["videos.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_metrics_video_date", "metrics", ["video_id", "date"])
    op.create_table(
        "costs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("video_id", sa.Integer(), nullable=False),
        sa.Column("gpu_seconds", sa.Float(), nullable=True),
        sa.Column("kwh_estimated", sa.Float(), nullable=True),
        sa.Column("api_cost", sa.Float(), nullable=True),
        sa.Column("computed_cost", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["video_id"], ["videos.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "ab_variants",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("video_id", sa.Integer(), nullable=False),
        sa.Column("variant_type", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("impressions", sa.Integer(), nullable=False),
        sa.Column("clicks", sa.Integer(), nullable=False),
        sa.Column("ctr", sa.Float(), nullable=True),
        sa.Column("is_winner", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["video_id"], ["videos.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("ab_variants")
    op.drop_table("costs")
    op.drop_index("ix_metrics_video_date", table_name="metrics")
    op.drop_table("metrics")
    op.drop_index("ix_jobs_video_stage", table_name="jobs")
    op.drop_table("jobs")
    op.drop_index("ix_alerts_channel_acknowledged", table_name="alerts")
    op.drop_table("alerts")
    op.drop_index("ix_topics_channel_status", table_name="topics")
    op.drop_table("topics")
    op.drop_index("ix_videos_channel_status", table_name="videos")
    op.drop_table("videos")
    op.drop_table("channels")
