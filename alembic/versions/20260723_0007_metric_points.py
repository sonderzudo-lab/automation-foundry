"""Add immutable, attributable metric observations.

Revision ID: 20260723_0007
Revises: 20260723_0006
Create Date: 2026-07-23
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260723_0007"
down_revision: str | None = "20260723_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_METRIC_KIND_SQL = "'counter', 'gauge', 'duration', 'ratio', 'currency'"


def upgrade() -> None:
    op.create_table(
        "metric_points",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("automation_id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=True),
        sa.Column("step_run_id", sa.Integer(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column(
            "value",
            sa.Numeric(precision=30, scale=10).with_variant(
                sa.String(length=32),
                "sqlite",
            ),
            nullable=False,
        ),
        sa.Column("unit", sa.String(length=50), nullable=False),
        sa.Column("source", sa.String(length=200), nullable=False),
        sa.Column(
            "confidence",
            sa.Numeric(precision=5, scale=4).with_variant(
                sa.String(length=7),
                "sqlite",
            ),
            nullable=True,
        ),
        sa.Column("dimensions", sa.JSON(), nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"kind IN ({_METRIC_KIND_SQL})",
            name="ck_metric_points_kind",
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR "
            "(CAST(confidence AS NUMERIC) >= 0 AND "
            "CAST(confidence AS NUMERIC) <= 1)",
            name="ck_metric_points_confidence_range",
        ),
        sa.CheckConstraint(
            "step_run_id IS NULL OR run_id IS NOT NULL",
            name="ck_metric_points_step_requires_run",
        ),
        sa.CheckConstraint(
            "kind NOT IN ('counter', 'duration') OR "
            "CAST(value AS NUMERIC) >= 0",
            name="ck_metric_points_nonnegative_kind",
        ),
        sa.CheckConstraint(
            "kind != 'ratio' OR (CAST(value AS NUMERIC) >= 0 AND "
            "CAST(value AS NUMERIC) <= 1)",
            name="ck_metric_points_ratio_range",
        ),
        sa.ForeignKeyConstraint(
            ["automation_id"],
            ["automations.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["step_run_id"],
            ["step_runs.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "automation_id",
            "idempotency_key",
            name="uq_metric_points_automation_idempotency_key",
        ),
    )
    op.create_index(
        "ix_metric_points_automation_name_observed",
        "metric_points",
        ["automation_id", "name", "observed_at"],
    )
    op.create_index(
        "ix_metric_points_run_observed",
        "metric_points",
        ["run_id", "observed_at"],
    )
    op.create_index(
        "ix_metric_points_step_observed",
        "metric_points",
        ["step_run_id", "observed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_metric_points_step_observed", table_name="metric_points")
    op.drop_index("ix_metric_points_run_observed", table_name="metric_points")
    op.drop_index(
        "ix_metric_points_automation_name_observed",
        table_name="metric_points",
    )
    op.drop_table("metric_points")
