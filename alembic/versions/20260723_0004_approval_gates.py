"""Add durable human approval gates and awaiting-approval run state.

Revision ID: 20260723_0004
Revises: 20260723_0003
Create Date: 2026-07-23
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260723_0004"
down_revision: str | None = "20260723_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RUN_STATUS_SQL = (
    "'queued', 'running', 'awaiting_approval', 'succeeded', 'failed', 'cancelled'"
)
_OLD_RUN_STATUS_SQL = "'queued', 'running', 'succeeded', 'failed', 'cancelled'"
_APPROVAL_STATUS_SQL = "'pending', 'approved', 'rejected', 'cancelled'"


def _replace_run_checks(status_sql: str) -> None:
    with op.batch_alter_table("runs") as batch_op:
        batch_op.drop_constraint("ck_runs_status", type_="check")
        batch_op.create_check_constraint("ck_runs_status", f"status IN ({status_sql})")
    with op.batch_alter_table("run_transitions") as batch_op:
        batch_op.drop_constraint(
            "ck_run_transitions_from_status",
            type_="check",
        )
        batch_op.drop_constraint("ck_run_transitions_to_status", type_="check")
        batch_op.create_check_constraint(
            "ck_run_transitions_from_status",
            f"from_status IS NULL OR from_status IN ({status_sql})",
        )
        batch_op.create_check_constraint(
            "ck_run_transitions_to_status",
            f"to_status IN ({status_sql})",
        )


def upgrade() -> None:
    _replace_run_checks(_RUN_STATUS_SQL)
    op.create_table(
        "approvals",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("payload_digest", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=30),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column(
            "requested_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("decided_by", sa.String(length=200), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.CheckConstraint(
            f"status IN ({_APPROVAL_STATUS_SQL})",
            name="ck_approvals_status",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id",
            "idempotency_key",
            name="uq_approvals_run_idempotency_key",
        ),
    )
    op.create_index(
        "ix_approvals_status_requested",
        "approvals",
        ["status", "requested_at"],
    )
    op.create_table(
        "approval_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("approval_id", sa.Integer(), nullable=False),
        sa.Column("from_status", sa.String(length=30), nullable=True),
        sa.Column("to_status", sa.String(length=30), nullable=False),
        sa.Column("actor", sa.String(length=200), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"from_status IS NULL OR from_status IN ({_APPROVAL_STATUS_SQL})",
            name="ck_approval_events_from_status",
        ),
        sa.CheckConstraint(
            f"to_status IN ({_APPROVAL_STATUS_SQL})",
            name="ck_approval_events_to_status",
        ),
        sa.ForeignKeyConstraint(
            ["approval_id"],
            ["approvals.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_approval_events_approval_occurred",
        "approval_events",
        ["approval_id", "occurred_at"],
    )
    with op.batch_alter_table("step_runs") as batch_op:
        batch_op.add_column(sa.Column("approval_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_step_runs_approval_id_approvals",
            "approvals",
            ["approval_id"],
            ["id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    with op.batch_alter_table("step_runs") as batch_op:
        batch_op.drop_constraint(
            "fk_step_runs_approval_id_approvals",
            type_="foreignkey",
        )
        batch_op.drop_column("approval_id")
    op.drop_index(
        "ix_approval_events_approval_occurred",
        table_name="approval_events",
    )
    op.drop_table("approval_events")
    op.drop_index("ix_approvals_status_requested", table_name="approvals")
    op.drop_table("approvals")
    _replace_run_checks(_OLD_RUN_STATUS_SQL)
