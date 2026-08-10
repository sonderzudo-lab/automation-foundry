"""Record the local actor for operator control changes.

Revision ID: 20260810_0011
Revises: 20260810_0010
Create Date: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260810_0011"
down_revision: str | None = "20260810_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("control_events") as batch_op:
        batch_op.add_column(sa.Column("actor", sa.String(length=200), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("control_events") as batch_op:
        batch_op.drop_column("actor")
