"""Add durable run dispatch records and append-only events.

Revision ID: 20260810_0015
Revises: 20260810_0014
Create Date: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260810_0015"
down_revision: str | None = "20260810_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "run_dispatches",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("queue", sa.String(length=20), nullable=False),
        sa.Column("task_name", sa.String(length=200), nullable=False),
        sa.Column("delivery_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("publish_attempts", sa.Integer(), nullable=False),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("claimed_by", sa.String(length=200), nullable=True),
        sa.Column("claim_token", sa.String(length=100), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("prepared_at", sa.DateTime(), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("queue IN ('gpu', 'cpu', 'io')", name="ck_run_dispatches_queue"),
        sa.CheckConstraint(
            "status IN ('pending', 'published', 'claimed', 'completed', 'failed')",
            name="ck_run_dispatches_status",
        ),
        sa.CheckConstraint(
            "publish_attempts >= 0",
            name="ck_run_dispatches_publish_attempts_nonnegative",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("delivery_id", name="uq_run_dispatches_delivery_id"),
        sa.UniqueConstraint("run_id", name="uq_run_dispatches_run_id"),
    )
    op.create_index(
        "ix_run_dispatches_status_lease",
        "run_dispatches",
        ["status", "lease_expires_at"],
    )
    op.create_table(
        "run_dispatch_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("dispatch_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("from_status", sa.String(length=30), nullable=True),
        sa.Column("to_status", sa.String(length=30), nullable=False),
        sa.Column("actor", sa.String(length=200), nullable=False),
        sa.Column("reason_code", sa.String(length=100), nullable=True),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "event_type IN ('prepared', 'publish_succeeded', 'publish_failed', "
            "'claimed', 'lease_reclaimed', 'completed', 'failed')",
            name="ck_run_dispatch_events_event_type",
        ),
        sa.CheckConstraint(
            "from_status IS NULL OR from_status IN "
            "('pending', 'published', 'claimed', 'completed', 'failed')",
            name="ck_run_dispatch_events_from_status",
        ),
        sa.CheckConstraint(
            "to_status IN ('pending', 'published', 'claimed', 'completed', 'failed')",
            name="ck_run_dispatch_events_to_status",
        ),
        sa.ForeignKeyConstraint(
            ["dispatch_id"], ["run_dispatches.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_run_dispatch_events_dispatch_occurred",
        "run_dispatch_events",
        ["dispatch_id", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_run_dispatch_events_dispatch_occurred",
        table_name="run_dispatch_events",
    )
    op.drop_table("run_dispatch_events")
    op.drop_index("ix_run_dispatches_status_lease", table_name="run_dispatches")
    op.drop_table("run_dispatches")
