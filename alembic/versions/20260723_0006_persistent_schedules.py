"""Add persistent, operator-controlled automation schedules.

Revision ID: 20260723_0006
Revises: 20260723_0005
Create Date: 2026-07-23
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260723_0006"
down_revision: str | None = "20260723_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEDULE_STATUS_SQL = "'disabled', 'enabled'"
_SCHEDULE_EVENT_TYPE_SQL = "'created', 'enabled', 'disabled'"


def upgrade() -> None:
    op.create_table(
        "schedules",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("automation_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("cron_expression", sa.String(length=100), nullable=False),
        sa.Column("timezone", sa.String(length=100), nullable=False),
        sa.Column("input_payload", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=30),
            server_default=sa.text("'disabled'"),
            nullable=False,
        ),
        sa.Column(
            "allow_overlap",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.Column(
            "misfire_grace_seconds",
            sa.Integer(),
            server_default=sa.text("300"),
            nullable=False,
        ),
        sa.Column("next_run_at", sa.DateTime(), nullable=True),
        sa.Column("last_enqueued_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"status IN ({_SCHEDULE_STATUS_SQL})",
            name="ck_schedules_status",
        ),
        sa.CheckConstraint(
            "misfire_grace_seconds >= 0",
            name="ck_schedules_misfire_grace_nonnegative",
        ),
        sa.CheckConstraint(
            "(status = 'disabled' AND next_run_at IS NULL) OR "
            "(status = 'enabled' AND next_run_at IS NOT NULL)",
            name="ck_schedules_status_next_run",
        ),
        sa.ForeignKeyConstraint(
            ["automation_id"],
            ["automations.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "automation_id",
            "name",
            name="uq_schedules_automation_name",
        ),
    )
    op.create_index(
        "ix_schedules_status_next_run",
        "schedules",
        ["status", "next_run_at"],
    )
    op.create_table(
        "schedule_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("schedule_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=30), nullable=False),
        sa.Column("from_status", sa.String(length=30), nullable=True),
        sa.Column("to_status", sa.String(length=30), nullable=False),
        sa.Column("actor", sa.String(length=200), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("previous_next_run_at", sa.DateTime(), nullable=True),
        sa.Column("next_run_at", sa.DateTime(), nullable=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"event_type IN ({_SCHEDULE_EVENT_TYPE_SQL})",
            name="ck_schedule_events_type",
        ),
        sa.CheckConstraint(
            f"from_status IS NULL OR from_status IN ({_SCHEDULE_STATUS_SQL})",
            name="ck_schedule_events_from_status",
        ),
        sa.CheckConstraint(
            f"to_status IN ({_SCHEDULE_STATUS_SQL})",
            name="ck_schedule_events_to_status",
        ),
        sa.CheckConstraint(
            "(event_type = 'created' AND from_status IS NULL AND "
            "to_status = 'disabled') OR "
            "(event_type = 'enabled' AND from_status = 'disabled' AND "
            "to_status = 'enabled') OR "
            "(event_type = 'disabled' AND from_status = 'enabled' AND "
            "to_status = 'disabled')",
            name="ck_schedule_events_transition",
        ),
        sa.ForeignKeyConstraint(
            ["schedule_id"],
            ["schedules.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_schedule_events_schedule_occurred",
        "schedule_events",
        ["schedule_id", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_schedule_events_schedule_occurred",
        table_name="schedule_events",
    )
    op.drop_table("schedule_events")
    op.drop_index("ix_schedules_status_next_run", table_name="schedules")
    op.drop_table("schedules")
