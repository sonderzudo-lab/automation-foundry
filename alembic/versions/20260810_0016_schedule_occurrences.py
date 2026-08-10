"""Add durable schedule occurrences.

Revision ID: 20260810_0016
Revises: 20260810_0015
Create Date: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260810_0016"
down_revision: str | None = "20260810_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "schedule_occurrences",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("schedule_id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=True),
        sa.Column("dispatch_id", sa.Integer(), nullable=True),
        sa.Column("scheduled_for", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("reason_code", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'published', 'skipped')",
            name="ck_schedule_occurrences_status",
        ),
        sa.CheckConstraint(
            "(status = 'skipped' AND run_id IS NULL AND dispatch_id IS NULL "
            "AND reason_code IS NOT NULL) OR "
            "(status IN ('pending', 'published') AND run_id IS NOT NULL "
            "AND dispatch_id IS NOT NULL AND reason_code IS NULL)",
            name="ck_schedule_occurrences_outcome",
        ),
        sa.ForeignKeyConstraint(["dispatch_id"], ["run_dispatches.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["schedule_id"], ["schedules.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dispatch_id", name="uq_schedule_occurrences_dispatch_id"),
        sa.UniqueConstraint("run_id", name="uq_schedule_occurrences_run_id"),
        sa.UniqueConstraint(
            "schedule_id",
            "scheduled_for",
            name="uq_schedule_occurrences_schedule_time",
        ),
    )
    op.create_index(
        "ix_schedule_occurrences_status_created",
        "schedule_occurrences",
        ["status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_schedule_occurrences_status_created",
        table_name="schedule_occurrences",
    )
    op.drop_table("schedule_occurrences")
