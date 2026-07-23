"""Add verified metadata registry for local artifacts.

Revision ID: 20260723_0005
Revises: 20260723_0004
Create Date: 2026-07-23
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260723_0005"
down_revision: str | None = "20260723_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ARTIFACT_SENSITIVITY_SQL = "'public', 'internal', 'confidential', 'restricted'"


def upgrade() -> None:
    op.create_table(
        "artifacts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("step_run_id", sa.Integer(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("artifact_type", sa.String(length=100), nullable=False),
        sa.Column("relative_path", sa.String(length=1000), nullable=False),
        sa.Column("media_type", sa.String(length=255), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("origin", sa.String(length=200), nullable=False),
        sa.Column(
            "sensitivity",
            sa.String(length=30),
            server_default=sa.text("'internal'"),
            nullable=False,
        ),
        sa.Column("retention_days", sa.Integer(), nullable=True),
        sa.Column("retention_until", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "verified_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "size_bytes >= 0",
            name="ck_artifacts_size_nonnegative",
        ),
        sa.CheckConstraint(
            "length(sha256) = 64",
            name="ck_artifacts_sha256_length",
        ),
        sa.CheckConstraint(
            "retention_days IS NULL OR retention_days >= 1",
            name="ck_artifacts_retention_days_positive",
        ),
        sa.CheckConstraint(
            f"sensitivity IN ({_ARTIFACT_SENSITIVITY_SQL})",
            name="ck_artifacts_sensitivity",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["step_run_id"],
            ["step_runs.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id",
            "idempotency_key",
            name="uq_artifacts_run_idempotency_key",
        ),
    )
    op.create_index(
        "ix_artifacts_run_created",
        "artifacts",
        ["run_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_artifacts_run_created", table_name="artifacts")
    op.drop_table("artifacts")
