"""Record durable evidence for approved artifact purges.

Revision ID: 20260811_0020
Revises: 20260811_0019
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260811_0020"
down_revision: str | None = "20260811_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PURGE_EVIDENCE = (
    "(purged_at IS NULL AND purged_by_approval_id IS NULL) OR "
    "(purged_at IS NOT NULL AND purged_by_approval_id IS NOT NULL)"
)


def upgrade() -> None:
    with op.batch_alter_table("artifacts") as batch_op:
        batch_op.add_column(sa.Column("purged_at", sa.DateTime(), nullable=True))
        batch_op.add_column(
            sa.Column("purged_by_approval_id", sa.Integer(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_artifacts_purged_by_approval_id",
            "approvals",
            ["purged_by_approval_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            "ck_artifacts_purge_evidence",
            _PURGE_EVIDENCE,
        )
    op.create_index("ix_artifacts_purged_at", "artifacts", ["purged_at"])


def downgrade() -> None:
    bind = op.get_bind()
    purged_count = bind.execute(
        sa.text("SELECT COUNT(*) FROM artifacts WHERE purged_at IS NOT NULL")
    ).scalar_one()
    if purged_count:
        raise RuntimeError("cannot downgrade while purged artifact evidence exists")
    op.drop_index("ix_artifacts_purged_at", table_name="artifacts")
    with op.batch_alter_table("artifacts") as batch_op:
        batch_op.drop_constraint("ck_artifacts_purge_evidence", type_="check")
        batch_op.drop_constraint(
            "fk_artifacts_purged_by_approval_id",
            type_="foreignkey",
        )
        batch_op.drop_column("purged_by_approval_id")
        batch_op.drop_column("purged_at")
