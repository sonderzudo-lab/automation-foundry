"""Create the initial shared Automation and Run kernel.

Revision ID: 20260723_0001
Revises: None
Create Date: 2026-07-23

This migration intentionally manages only shared platform tables. The legacy
Content Engine tables remain outside Alembic until a later explicit baseline.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260723_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RUN_STATUS_SQL = "'queued', 'running', 'succeeded', 'failed', 'cancelled'"


def upgrade() -> None:
    op.create_table(
        "automations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("owner", sa.String(length=200), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_automations_slug"),
    )
    op.create_table(
        "runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("automation_id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("trigger", sa.String(length=50), nullable=False),
        sa.Column("input_payload", sa.JSON(), nullable=False),
        sa.Column("output_payload", sa.JSON(), nullable=True),
        sa.Column(
            "status",
            sa.String(length=30),
            server_default=sa.text("'queued'"),
            nullable=False,
        ),
        sa.Column("error", sa.JSON(), nullable=True),
        sa.Column(
            "queued_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(f"status IN ({_RUN_STATUS_SQL})", name="ck_runs_status"),
        sa.ForeignKeyConstraint(
            ["automation_id"],
            ["automations.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "automation_id",
            "idempotency_key",
            name="uq_runs_automation_idempotency_key",
        ),
    )
    op.create_index("ix_runs_status_queued_at", "runs", ["status", "queued_at"])
    op.create_table(
        "run_transitions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("from_status", sa.String(length=30), nullable=True),
        sa.Column("to_status", sa.String(length=30), nullable=False),
        sa.Column("error", sa.JSON(), nullable=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.CheckConstraint(
            f"from_status IS NULL OR from_status IN ({_RUN_STATUS_SQL})",
            name="ck_run_transitions_from_status",
        ),
        sa.CheckConstraint(
            f"to_status IN ({_RUN_STATUS_SQL})",
            name="ck_run_transitions_to_status",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_run_transitions_run_occurred",
        "run_transitions",
        ["run_id", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_run_transitions_run_occurred", table_name="run_transitions")
    op.drop_table("run_transitions")
    op.drop_index("ix_runs_status_queued_at", table_name="runs")
    op.drop_table("runs")
    op.drop_table("automations")
