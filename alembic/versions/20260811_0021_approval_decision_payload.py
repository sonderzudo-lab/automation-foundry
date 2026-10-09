"""Persist bounded structured evidence selected during an approval decision.

Revision ID: 20260811_0021
Revises: 20260811_0020
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260811_0021"
down_revision: str | None = "20260811_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("approvals") as batch_op:
        batch_op.add_column(sa.Column("decision_payload", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("approvals") as batch_op:
        batch_op.drop_column("decision_payload")
