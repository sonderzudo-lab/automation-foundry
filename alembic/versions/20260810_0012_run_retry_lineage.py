"""Persist explicit run retry lineage and operator evidence.

Revision ID: 20260810_0012
Revises: 20260810_0011
Create Date: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260810_0012"
down_revision: str | None = "20260810_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("runs") as batch_op:
        batch_op.add_column(sa.Column("retry_of_run_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("retry_requested_by", sa.String(length=200), nullable=True))
        batch_op.add_column(sa.Column("retry_reason", sa.Text(), nullable=True))
        batch_op.create_foreign_key(
            "fk_runs_retry_of_run_id_runs",
            "runs",
            ["retry_of_run_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_unique_constraint("uq_runs_retry_of_run_id", ["retry_of_run_id"])


def downgrade() -> None:
    with op.batch_alter_table("runs") as batch_op:
        batch_op.drop_constraint("uq_runs_retry_of_run_id", type_="unique")
        batch_op.drop_constraint("fk_runs_retry_of_run_id_runs", type_="foreignkey")
        batch_op.drop_column("retry_reason")
        batch_op.drop_column("retry_requested_by")
        batch_op.drop_column("retry_of_run_id")
