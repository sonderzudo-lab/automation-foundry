"""Allow durable dispatch wake-ups after approved continuation work.

Revision ID: 20260811_0019
Revises: 20260810_0018
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260811_0019"
down_revision: str | None = "20260810_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_EVENT_TYPES = (
    "event_type IN ('prepared', 'publish_succeeded', 'publish_failed', "
    "'claimed', 'lease_reclaimed', 'completed', 'failed')"
)
_NEW_EVENT_TYPES = (
    "event_type IN ('prepared', 'publish_succeeded', 'publish_failed', "
    "'claimed', 'lease_reclaimed', 'requeued', 'completed', 'failed')"
)


def upgrade() -> None:
    _replace_constraint(_NEW_EVENT_TYPES)


def downgrade() -> None:
    bind = op.get_bind()
    requeued_count = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM run_dispatch_events "
            "WHERE event_type = 'requeued'"
        )
    ).scalar_one()
    if requeued_count:
        raise RuntimeError("cannot downgrade while requeued dispatch events exist")
    _replace_constraint(_OLD_EVENT_TYPES)


def _replace_constraint(expression: str) -> None:
    with op.batch_alter_table("run_dispatch_events") as batch_op:
        batch_op.drop_constraint("ck_run_dispatch_events_event_type", type_="check")
        batch_op.create_check_constraint(
            "ck_run_dispatch_events_event_type",
            expression,
        )
