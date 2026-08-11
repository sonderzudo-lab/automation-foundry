"""Audit administrative automation enable and disable controls.

Revision ID: 20260810_0018
Revises: 20260810_0017
Create Date: 2026-08-10
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260810_0018"
down_revision: str | None = "20260810_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_EVENT_TYPES = (
    "event_type IN ('kill_switch_enabled', 'kill_switch_disabled', "
    "'cancellation_requested')"
)
_NEW_EVENT_TYPES = (
    "event_type IN ('kill_switch_enabled', 'kill_switch_disabled', "
    "'automation_enabled', 'automation_disabled', 'cancellation_requested')"
)
_OLD_TARGETS = (
    "(automation_id IS NOT NULL AND run_id IS NULL AND "
    "event_type IN ('kill_switch_enabled', 'kill_switch_disabled')) OR "
    "(automation_id IS NULL AND run_id IS NOT NULL AND "
    "event_type = 'cancellation_requested')"
)
_NEW_TARGETS = (
    "(automation_id IS NOT NULL AND run_id IS NULL AND "
    "event_type IN ('kill_switch_enabled', 'kill_switch_disabled', "
    "'automation_enabled', 'automation_disabled')) OR "
    "(automation_id IS NULL AND run_id IS NOT NULL AND "
    "event_type = 'cancellation_requested')"
)


def upgrade() -> None:
    _replace_constraints(event_types=_NEW_EVENT_TYPES, targets=_NEW_TARGETS)


def downgrade() -> None:
    bind = op.get_bind()
    unsupported_count = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM control_events "
            "WHERE event_type IN ('automation_enabled', 'automation_disabled')"
        )
    ).scalar_one()
    if unsupported_count:
        raise RuntimeError(
            "cannot downgrade while automation enabled/disabled audit events exist"
        )
    _replace_constraints(event_types=_OLD_EVENT_TYPES, targets=_OLD_TARGETS)


def _replace_constraints(*, event_types: str, targets: str) -> None:
    with op.batch_alter_table("control_events") as batch_op:
        batch_op.drop_constraint("ck_control_events_target", type_="check")
        batch_op.drop_constraint("ck_control_events_type", type_="check")
        batch_op.create_check_constraint("ck_control_events_type", event_types)
        batch_op.create_check_constraint("ck_control_events_target", targets)
