"""Add durable step execution and transition history.

Revision ID: 20260723_0002
Revises: 20260723_0001
Create Date: 2026-07-23
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260723_0002"
down_revision: str | None = "20260723_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RUN_STATUS_SQL = "'queued', 'running', 'succeeded', 'failed', 'cancelled'"
_QUEUE_CLASS_SQL = "'gpu', 'cpu', 'io'"


def upgrade() -> None:
    op.create_table(
        "step_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("queue", sa.String(length=20), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("attempt", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
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
        sa.CheckConstraint(f"queue IN ({_QUEUE_CLASS_SQL})", name="ck_step_runs_queue"),
        sa.CheckConstraint(f"status IN ({_RUN_STATUS_SQL})", name="ck_step_runs_status"),
        sa.CheckConstraint("ordinal >= 1", name="ck_step_runs_ordinal_positive"),
        sa.CheckConstraint("attempt >= 1", name="ck_step_runs_attempt_positive"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id",
            "idempotency_key",
            "attempt",
            name="uq_step_runs_run_key_attempt",
        ),
    )
    op.create_index(
        "ix_step_runs_run_status_ordinal",
        "step_runs",
        ["run_id", "status", "ordinal"],
    )
    op.create_table(
        "step_run_transitions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("step_run_id", sa.Integer(), nullable=False),
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
            name="ck_step_run_transitions_from_status",
        ),
        sa.CheckConstraint(
            f"to_status IN ({_RUN_STATUS_SQL})",
            name="ck_step_run_transitions_to_status",
        ),
        sa.ForeignKeyConstraint(
            ["step_run_id"],
            ["step_runs.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_step_run_transitions_step_occurred",
        "step_run_transitions",
        ["step_run_id", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_step_run_transitions_step_occurred",
        table_name="step_run_transitions",
    )
    op.drop_table("step_run_transitions")
    op.drop_index("ix_step_runs_run_status_ordinal", table_name="step_runs")
    op.drop_table("step_runs")
