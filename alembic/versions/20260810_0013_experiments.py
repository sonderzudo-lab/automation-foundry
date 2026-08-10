"""Add measurable experiments with audited lifecycle and run attribution.

Revision ID: 20260810_0013
Revises: 20260810_0012
Create Date: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260810_0013"
down_revision: str | None = "20260810_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUS_SQL = "'draft', 'running', 'paused', 'completed', 'cancelled'"
_EVENT_SQL = "'created', 'started', 'paused', 'resumed', 'completed', 'cancelled'"


def upgrade() -> None:
    op.create_table(
        "experiments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("automation_id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("hypothesis", sa.Text(), nullable=False),
        sa.Column("primary_metric", sa.String(length=200), nullable=False),
        sa.Column("primary_metric_unit", sa.String(length=30), nullable=False),
        sa.Column("control_variant", sa.String(length=100), nullable=False),
        sa.Column("candidate_variant", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(f"status IN ({_STATUS_SQL})", name="ck_experiments_status"),
        sa.CheckConstraint(
            "control_variant <> candidate_variant",
            name="ck_experiments_distinct_variants",
        ),
        sa.ForeignKeyConstraint(
            ["automation_id"],
            ["automations.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "automation_id",
            "key",
            name="uq_experiments_automation_key",
        ),
    )
    op.create_index(
        "ix_experiments_status_created",
        "experiments",
        ["status", "created_at"],
    )
    op.create_table(
        "experiment_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("experiment_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=30), nullable=False),
        sa.Column("from_status", sa.String(length=30), nullable=True),
        sa.Column("to_status", sa.String(length=30), nullable=False),
        sa.Column("actor", sa.String(length=200), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            f"event_type IN ({_EVENT_SQL})",
            name="ck_experiment_events_type",
        ),
        sa.CheckConstraint(
            f"from_status IS NULL OR from_status IN ({_STATUS_SQL})",
            name="ck_experiment_events_from_status",
        ),
        sa.CheckConstraint(
            f"to_status IN ({_STATUS_SQL})",
            name="ck_experiment_events_to_status",
        ),
        sa.CheckConstraint(
            "(event_type = 'created' AND from_status IS NULL AND to_status = 'draft') OR "
            "(event_type = 'started' AND from_status = 'draft' AND to_status = 'running') OR "
            "(event_type = 'paused' AND from_status = 'running' AND to_status = 'paused') OR "
            "(event_type = 'resumed' AND from_status = 'paused' AND to_status = 'running') OR "
            "(event_type = 'completed' AND from_status IN ('running', 'paused') AND to_status = 'completed') OR "
            "(event_type = 'cancelled' AND from_status IN ('draft', 'running', 'paused') AND to_status = 'cancelled')",
            name="ck_experiment_events_transition",
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"],
            ["experiments.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_experiment_events_experiment_occurred",
        "experiment_events",
        ["experiment_id", "occurred_at"],
    )
    with op.batch_alter_table("runs") as batch_op:
        batch_op.add_column(sa.Column("experiment_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_runs_experiment_id_experiments",
            "experiments",
            ["experiment_id"],
            ["id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch_op:
        batch_op.drop_constraint(
            "fk_runs_experiment_id_experiments",
            type_="foreignkey",
        )
        batch_op.drop_column("experiment_id")
    op.drop_index(
        "ix_experiment_events_experiment_occurred",
        table_name="experiment_events",
    )
    op.drop_table("experiment_events")
    op.drop_index("ix_experiments_status_created", table_name="experiments")
    op.drop_table("experiments")
