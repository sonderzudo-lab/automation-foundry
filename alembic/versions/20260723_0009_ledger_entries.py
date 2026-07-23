"""Add immutable, attributable financial observations.

Revision ID: 20260723_0009
Revises: 20260723_0008
Create Date: 2026-07-23
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260723_0009"
down_revision: str | None = "20260723_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEDGER_ENTRY_TYPE_SQL = "'cost', 'revenue', 'attributed_value'"


def upgrade() -> None:
    op.create_table(
        "ledger_entries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("automation_id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=True),
        sa.Column("step_run_id", sa.Integer(), nullable=True),
        sa.Column("metric_point_id", sa.Integer(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("entry_type", sa.String(length=30), nullable=False),
        sa.Column("category", sa.String(length=100), nullable=False),
        sa.Column(
            "amount",
            sa.Numeric(precision=30, scale=10).with_variant(
                sa.String(length=32),
                "sqlite",
            ),
            nullable=False,
        ),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("source", sa.String(length=200), nullable=False),
        sa.Column(
            "confidence",
            sa.Numeric(precision=5, scale=4).with_variant(
                sa.String(length=7),
                "sqlite",
            ),
            nullable=True,
        ),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column(
            "recorded_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"entry_type IN ({_LEDGER_ENTRY_TYPE_SQL})",
            name="ck_ledger_entries_type",
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR "
            "(CAST(confidence AS NUMERIC) >= 0 AND "
            "CAST(confidence AS NUMERIC) <= 1)",
            name="ck_ledger_entries_confidence_range",
        ),
        sa.CheckConstraint(
            "step_run_id IS NULL OR run_id IS NOT NULL",
            name="ck_ledger_entries_step_requires_run",
        ),
        sa.CheckConstraint(
            "LENGTH(currency) = 3 AND currency = UPPER(currency)",
            name="ck_ledger_entries_currency",
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
        sa.ForeignKeyConstraint(
            ["metric_point_id"],
            ["metric_points.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "automation_id",
            "idempotency_key",
            name="uq_ledger_entries_automation_idempotency_key",
        ),
    )
    op.create_index(
        "ix_ledger_entries_automation_type_observed",
        "ledger_entries",
        ["automation_id", "entry_type", "observed_at"],
    )
    op.create_index(
        "ix_ledger_entries_run_observed",
        "ledger_entries",
        ["run_id", "observed_at"],
    )
    op.create_index(
        "ix_ledger_entries_step_observed",
        "ledger_entries",
        ["step_run_id", "observed_at"],
    )
    op.create_index(
        "ix_ledger_entries_metric",
        "ledger_entries",
        ["metric_point_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_ledger_entries_metric", table_name="ledger_entries")
    op.drop_index(
        "ix_ledger_entries_step_observed",
        table_name="ledger_entries",
    )
    op.drop_index("ix_ledger_entries_run_observed", table_name="ledger_entries")
    op.drop_index(
        "ix_ledger_entries_automation_type_observed",
        table_name="ledger_entries",
    )
    op.drop_table("ledger_entries")
