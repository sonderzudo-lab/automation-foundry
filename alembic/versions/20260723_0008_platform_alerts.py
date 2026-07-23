"""Add deduplicated, audited platform alerts.

Revision ID: 20260723_0008
Revises: 20260723_0007
Create Date: 2026-07-23
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260723_0008"
down_revision: str | None = "20260723_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ALERT_SEVERITY_SQL = "'info', 'warning', 'error', 'critical'"
_ALERT_STATUS_SQL = "'open', 'acknowledged', 'resolved'"
_ALERT_EVENT_TYPE_SQL = (
    "'opened', 'occurred', 'reopened', 'acknowledged', 'resolved'"
)


def upgrade() -> None:
    op.create_table(
        "platform_alerts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("automation_id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=True),
        sa.Column("step_run_id", sa.Integer(), nullable=True),
        sa.Column("metric_point_id", sa.Integer(), nullable=True),
        sa.Column("deduplication_key", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("severity", sa.String(length=30), nullable=False),
        sa.Column(
            "status",
            sa.String(length=30),
            server_default=sa.text("'open'"),
            nullable=False,
        ),
        sa.Column("source", sa.String(length=200), nullable=False),
        sa.Column(
            "occurrence_count",
            sa.Integer(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(), nullable=True),
        sa.Column("acknowledged_by", sa.String(length=200), nullable=True),
        sa.Column("acknowledgement_reason", sa.Text(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("resolved_by", sa.String(length=200), nullable=True),
        sa.Column("resolution_reason", sa.Text(), nullable=True),
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
            f"severity IN ({_ALERT_SEVERITY_SQL})",
            name="ck_platform_alerts_severity",
        ),
        sa.CheckConstraint(
            f"status IN ({_ALERT_STATUS_SQL})",
            name="ck_platform_alerts_status",
        ),
        sa.CheckConstraint(
            "occurrence_count >= 1",
            name="ck_platform_alerts_occurrence_count_positive",
        ),
        sa.CheckConstraint(
            "last_seen_at >= first_seen_at",
            name="ck_platform_alerts_seen_order",
        ),
        sa.CheckConstraint(
            "step_run_id IS NULL OR run_id IS NOT NULL",
            name="ck_platform_alerts_step_requires_run",
        ),
        sa.CheckConstraint(
            "((acknowledged_at IS NULL AND acknowledged_by IS NULL AND "
            "acknowledgement_reason IS NULL) OR "
            "(acknowledged_at IS NOT NULL AND acknowledged_by IS NOT NULL AND "
            "acknowledgement_reason IS NOT NULL))",
            name="ck_platform_alerts_acknowledgement_complete",
        ),
        sa.CheckConstraint(
            "((resolved_at IS NULL AND resolved_by IS NULL AND "
            "resolution_reason IS NULL) OR "
            "(resolved_at IS NOT NULL AND resolved_by IS NOT NULL AND "
            "resolution_reason IS NOT NULL))",
            name="ck_platform_alerts_resolution_complete",
        ),
        sa.CheckConstraint(
            "(status = 'open' AND acknowledged_at IS NULL AND resolved_at IS NULL) "
            "OR (status = 'acknowledged' AND acknowledged_at IS NOT NULL AND "
            "resolved_at IS NULL) OR "
            "(status = 'resolved' AND resolved_at IS NOT NULL)",
            name="ck_platform_alerts_status_metadata",
        ),
        sa.ForeignKeyConstraint(
            ["automation_id"],
            ["automations.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["step_run_id"],
            ["step_runs.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["metric_point_id"],
            ["metric_points.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "automation_id",
            "deduplication_key",
            name="uq_platform_alerts_automation_deduplication",
        ),
    )
    op.create_index(
        "ix_platform_alerts_status_severity_seen",
        "platform_alerts",
        ["status", "severity", "last_seen_at"],
    )
    op.create_index(
        "ix_platform_alerts_automation_status",
        "platform_alerts",
        ["automation_id", "status"],
    )
    op.create_index(
        "ix_platform_alerts_run_status",
        "platform_alerts",
        ["run_id", "status"],
    )
    op.create_table(
        "platform_alert_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("alert_id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("event_type", sa.String(length=30), nullable=False),
        sa.Column("from_status", sa.String(length=30), nullable=True),
        sa.Column("to_status", sa.String(length=30), nullable=False),
        sa.Column("severity", sa.String(length=30), nullable=False),
        sa.Column("actor", sa.String(length=200), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"event_type IN ({_ALERT_EVENT_TYPE_SQL})",
            name="ck_platform_alert_events_type",
        ),
        sa.CheckConstraint(
            f"from_status IS NULL OR from_status IN ({_ALERT_STATUS_SQL})",
            name="ck_platform_alert_events_from_status",
        ),
        sa.CheckConstraint(
            f"to_status IN ({_ALERT_STATUS_SQL})",
            name="ck_platform_alert_events_to_status",
        ),
        sa.CheckConstraint(
            f"severity IN ({_ALERT_SEVERITY_SQL})",
            name="ck_platform_alert_events_severity",
        ),
        sa.CheckConstraint(
            "(event_type IN ('opened', 'occurred', 'reopened') AND "
            "idempotency_key IS NOT NULL) OR "
            "(event_type IN ('acknowledged', 'resolved') AND "
            "idempotency_key IS NULL)",
            name="ck_platform_alert_events_idempotency_scope",
        ),
        sa.CheckConstraint(
            "(event_type = 'opened' AND from_status IS NULL AND "
            "to_status = 'open') OR "
            "(event_type = 'occurred' AND from_status = 'open' AND "
            "to_status = 'open') OR "
            "(event_type = 'reopened' AND "
            "from_status IN ('acknowledged', 'resolved') AND "
            "to_status = 'open') OR "
            "(event_type = 'acknowledged' AND from_status = 'open' AND "
            "to_status = 'acknowledged') OR "
            "(event_type = 'resolved' AND "
            "from_status IN ('open', 'acknowledged') AND "
            "to_status = 'resolved')",
            name="ck_platform_alert_events_transition",
        ),
        sa.ForeignKeyConstraint(
            ["alert_id"],
            ["platform_alerts.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "alert_id",
            "idempotency_key",
            name="uq_platform_alert_events_alert_idempotency",
        ),
    )
    op.create_index(
        "ix_platform_alert_events_alert_occurred",
        "platform_alert_events",
        ["alert_id", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_platform_alert_events_alert_occurred",
        table_name="platform_alert_events",
    )
    op.drop_table("platform_alert_events")
    op.drop_index("ix_platform_alerts_run_status", table_name="platform_alerts")
    op.drop_index(
        "ix_platform_alerts_automation_status",
        table_name="platform_alerts",
    )
    op.drop_index(
        "ix_platform_alerts_status_severity_seen",
        table_name="platform_alerts",
    )
    op.drop_table("platform_alerts")
