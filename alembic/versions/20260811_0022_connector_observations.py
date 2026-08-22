"""Add append-only connector freshness and quality observations.

Revision ID: 20260811_0022
Revises: 20260811_0021
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260811_0022"
down_revision: str | None = "20260811_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "connector_observations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("automation_id", sa.Integer(), nullable=False),
        sa.Column("connector_key", sa.String(length=100), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("status_slo_seconds", sa.Integer(), nullable=False),
        sa.Column("last_success_at", sa.DateTime(), nullable=True),
        sa.Column("freshness_slo_seconds", sa.Integer(), nullable=False),
        sa.Column("quality_status", sa.String(length=30), nullable=False),
        sa.Column(
            "quality_score",
            sa.Numeric(precision=5, scale=4).with_variant(
                sa.String(length=7),
                "sqlite",
            ),
            nullable=True,
        ),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('healthy', 'degraded', 'unavailable', 'disabled')",
            name="ck_connector_observations_status",
        ),
        sa.CheckConstraint(
            "quality_status IN ('pass', 'warning', 'fail', 'unknown')",
            name="ck_connector_observations_quality_status",
        ),
        sa.CheckConstraint(
            "status_slo_seconds > 0 AND freshness_slo_seconds > 0",
            name="ck_connector_observations_positive_slos",
        ),
        sa.CheckConstraint(
            "quality_score IS NULL OR "
            "(CAST(quality_score AS NUMERIC) >= 0 AND "
            "CAST(quality_score AS NUMERIC) <= 1)",
            name="ck_connector_observations_quality_score_range",
        ),
        sa.CheckConstraint(
            "quality_status != 'unknown' OR quality_score IS NULL",
            name="ck_connector_observations_unknown_quality_score",
        ),
        sa.CheckConstraint(
            "last_success_at IS NULL OR last_success_at <= observed_at",
            name="ck_connector_observations_success_not_future",
        ),
        sa.CheckConstraint(
            "observed_at <= recorded_at",
            name="ck_connector_observations_observed_not_future",
        ),
        sa.ForeignKeyConstraint(
            ["automation_id"],
            ["automations.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "automation_id",
            "connector_key",
            "idempotency_key",
            name="uq_connector_observations_idempotency",
        ),
    )
    op.create_index(
        "ix_connector_observations_latest",
        "connector_observations",
        ["automation_id", "connector_key", "observed_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_connector_observations_status_observed",
        "connector_observations",
        ["status", "observed_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_connector_observations_status_observed",
        table_name="connector_observations",
    )
    op.drop_index(
        "ix_connector_observations_latest",
        table_name="connector_observations",
    )
    op.drop_table("connector_observations")
