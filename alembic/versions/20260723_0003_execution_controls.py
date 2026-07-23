"""Add durable cancellation requests and automation kill switches.

Revision ID: 20260723_0003
Revises: 20260723_0002
Create Date: 2026-07-23
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260723_0003"
down_revision: str | None = "20260723_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONTROL_EVENT_TYPE_SQL = (
    "'kill_switch_enabled', 'kill_switch_disabled', 'cancellation_requested'"
)


def upgrade() -> None:
    op.add_column(
        "automations",
        sa.Column(
            "kill_switch_active",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    op.add_column(
        "automations",
        sa.Column("kill_switch_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "automations",
        sa.Column("kill_switch_changed_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "runs",
        sa.Column("cancellation_requested_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "runs",
        sa.Column("cancellation_reason", sa.Text(), nullable=True),
    )
    op.create_table(
        "control_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("automation_id", sa.Integer(), nullable=True),
        sa.Column("run_id", sa.Integer(), nullable=True),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"event_type IN ({_CONTROL_EVENT_TYPE_SQL})",
            name="ck_control_events_type",
        ),
        sa.CheckConstraint(
            "(automation_id IS NOT NULL AND run_id IS NULL AND "
            "event_type IN ('kill_switch_enabled', 'kill_switch_disabled')) OR "
            "(automation_id IS NULL AND run_id IS NOT NULL AND "
            "event_type = 'cancellation_requested')",
            name="ck_control_events_target",
        ),
        sa.ForeignKeyConstraint(
            ["automation_id"],
            ["automations.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_control_events_automation_occurred",
        "control_events",
        ["automation_id", "occurred_at"],
    )
    op.create_index(
        "ix_control_events_run_occurred",
        "control_events",
        ["run_id", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_control_events_run_occurred", table_name="control_events")
    op.drop_index("ix_control_events_automation_occurred", table_name="control_events")
    op.drop_table("control_events")
    op.drop_column("runs", "cancellation_reason")
    op.drop_column("runs", "cancellation_requested_at")
    op.drop_column("automations", "kill_switch_changed_at")
    op.drop_column("automations", "kill_switch_reason")
    op.drop_column("automations", "kill_switch_active")
