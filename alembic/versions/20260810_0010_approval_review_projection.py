"""Store an explicit safe projection for operator approval review.

Revision ID: 20260810_0010
Revises: 20260723_0009
Create Date: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260810_0010"
down_revision: str | None = "20260723_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("approvals") as batch_op:
        batch_op.add_column(sa.Column("review_payload", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("approvals") as batch_op:
        batch_op.drop_column("review_payload")
