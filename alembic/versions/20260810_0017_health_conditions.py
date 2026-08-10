"""Add durable health condition counters.

Revision ID: 20260810_0017
Revises: 20260810_0016
Create Date: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260810_0017"
down_revision: str | None = "20260810_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "health_conditions",
        sa.Column("check_name", sa.String(length=64), nullable=False),
        sa.Column("last_status", sa.String(length=30), nullable=False),
        sa.Column("consecutive_unhealthy", sa.Integer(), nullable=False),
        sa.Column("consecutive_healthy", sa.Integer(), nullable=False),
        sa.Column("first_unhealthy_at", sa.DateTime(), nullable=True),
        sa.Column("last_observed_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "last_status IN ('pass', 'degraded', 'fail', 'skip')",
            name="ck_health_conditions_status",
        ),
        sa.CheckConstraint(
            "consecutive_unhealthy >= 0 AND consecutive_healthy >= 0",
            name="ck_health_conditions_nonnegative_counts",
        ),
        sa.CheckConstraint(
            "consecutive_unhealthy = 0 OR consecutive_healthy = 0",
            name="ck_health_conditions_exclusive_counts",
        ),
        sa.CheckConstraint(
            "(consecutive_unhealthy = 0 AND first_unhealthy_at IS NULL) OR "
            "(consecutive_unhealthy > 0 AND first_unhealthy_at IS NOT NULL)",
            name="ck_health_conditions_unhealthy_timestamp",
        ),
        sa.PrimaryKeyConstraint("check_name"),
    )
    op.create_index(
        "ix_health_conditions_status_observed",
        "health_conditions",
        ["last_status", "last_observed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_health_conditions_status_observed",
        table_name="health_conditions",
    )
    op.drop_table("health_conditions")
