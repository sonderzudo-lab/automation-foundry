"""Durable shared models for the Automation Foundry run kernel."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.database import Base


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class RunStatus(StrEnum):
    """States supported by the first durable run lifecycle slice."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class QueueClass(StrEnum):
    """Execution queues reserved by the local worker topology."""

    GPU = "gpu"
    CPU = "cpu"
    IO = "io"


class ControlEventType(StrEnum):
    """Audited operator controls supported by the local run kernel."""

    KILL_SWITCH_ENABLED = "kill_switch_enabled"
    KILL_SWITCH_DISABLED = "kill_switch_disabled"
    CANCELLATION_REQUESTED = "cancellation_requested"


_RUN_STATUS_SQL = "'queued', 'running', 'succeeded', 'failed', 'cancelled'"
_QUEUE_CLASS_SQL = "'gpu', 'cpu', 'io'"
_CONTROL_EVENT_TYPE_SQL = (
    "'kill_switch_enabled', 'kill_switch_disabled', 'cancellation_requested'"
)


class Automation(Base):
    """A registered automation module owned by a local operator."""

    __tablename__ = "automations"
    __table_args__ = (UniqueConstraint("slug", name="uq_automations_slug"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    slug: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    owner: Mapped[str] = mapped_column(String(200), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    kill_switch_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    kill_switch_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    kill_switch_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)

    runs: Mapped[list[Run]] = relationship("Run", back_populates="automation")
    control_events: Mapped[list[ControlEvent]] = relationship(
        "ControlEvent",
        back_populates="automation",
        order_by="ControlEvent.id",
    )


class Run(Base):
    """One durable execution request for an automation."""

    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint(
            "automation_id",
            "idempotency_key",
            name="uq_runs_automation_idempotency_key",
        ),
        CheckConstraint(f"status IN ({_RUN_STATUS_SQL})", name="ck_runs_status"),
        Index("ix_runs_status_queued_at", "status", "queued_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    automation_id: Mapped[int] = mapped_column(
        ForeignKey("automations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    trigger: Mapped[str] = mapped_column(String(50), nullable=False, default="manual")
    input_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    output_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default=RunStatus.QUEUED.value,
    )
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    cancellation_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    queued_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    automation: Mapped[Automation] = relationship("Automation", back_populates="runs")
    transitions: Mapped[list[RunTransition]] = relationship(
        "RunTransition",
        back_populates="run",
        order_by="RunTransition.id",
    )
    step_runs: Mapped[list[StepRun]] = relationship(
        "StepRun",
        back_populates="run",
        order_by="StepRun.ordinal",
    )
    control_events: Mapped[list[ControlEvent]] = relationship(
        "ControlEvent",
        back_populates="run",
        order_by="ControlEvent.id",
    )


class RunTransition(Base):
    """Append-only evidence of one persisted run state transition."""

    __tablename__ = "run_transitions"
    __table_args__ = (
        CheckConstraint(
            f"from_status IS NULL OR from_status IN ({_RUN_STATUS_SQL})",
            name="ck_run_transitions_from_status",
        ),
        CheckConstraint(
            f"to_status IN ({_RUN_STATUS_SQL})",
            name="ck_run_transitions_to_status",
        ),
        Index("ix_run_transitions_run_occurred", "run_id", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    from_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    to_status: Mapped[str] = mapped_column(String(30), nullable=False)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    run: Mapped[Run] = relationship("Run", back_populates="transitions")


class StepRun(Base):
    """One durable, independently observable step inside a run."""

    __tablename__ = "step_runs"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "idempotency_key",
            "attempt",
            name="uq_step_runs_run_key_attempt",
        ),
        CheckConstraint(f"queue IN ({_QUEUE_CLASS_SQL})", name="ck_step_runs_queue"),
        CheckConstraint(f"status IN ({_RUN_STATUS_SQL})", name="ck_step_runs_status"),
        CheckConstraint("ordinal >= 1", name="ck_step_runs_ordinal_positive"),
        CheckConstraint("attempt >= 1", name="ck_step_runs_attempt_positive"),
        Index("ix_step_runs_run_status_ordinal", "run_id", "status", "ordinal"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    queue: Mapped[str] = mapped_column(String(20), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    input_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    output_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default=RunStatus.QUEUED.value,
    )
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    queued_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    run: Mapped[Run] = relationship("Run", back_populates="step_runs")
    transitions: Mapped[list[StepRunTransition]] = relationship(
        "StepRunTransition",
        back_populates="step_run",
        order_by="StepRunTransition.id",
    )


class StepRunTransition(Base):
    """Append-only evidence of one persisted step state transition."""

    __tablename__ = "step_run_transitions"
    __table_args__ = (
        CheckConstraint(
            f"from_status IS NULL OR from_status IN ({_RUN_STATUS_SQL})",
            name="ck_step_run_transitions_from_status",
        ),
        CheckConstraint(
            f"to_status IN ({_RUN_STATUS_SQL})",
            name="ck_step_run_transitions_to_status",
        ),
        Index(
            "ix_step_run_transitions_step_occurred",
            "step_run_id",
            "occurred_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    step_run_id: Mapped[int] = mapped_column(
        ForeignKey("step_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    from_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    to_status: Mapped[str] = mapped_column(String(30), nullable=False)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    step_run: Mapped[StepRun] = relationship("StepRun", back_populates="transitions")


class ControlEvent(Base):
    """Append-only audit evidence for operator safety controls."""

    __tablename__ = "control_events"
    __table_args__ = (
        CheckConstraint(
            f"event_type IN ({_CONTROL_EVENT_TYPE_SQL})",
            name="ck_control_events_type",
        ),
        CheckConstraint(
            "(automation_id IS NOT NULL AND run_id IS NULL AND "
            "event_type IN ('kill_switch_enabled', 'kill_switch_disabled')) OR "
            "(automation_id IS NULL AND run_id IS NOT NULL AND "
            "event_type = 'cancellation_requested')",
            name="ck_control_events_target",
        ),
        Index("ix_control_events_automation_occurred", "automation_id", "occurred_at"),
        Index("ix_control_events_run_occurred", "run_id", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    automation_id: Mapped[int | None] = mapped_column(
        ForeignKey("automations.id", ondelete="RESTRICT"),
        nullable=True,
    )
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("runs.id", ondelete="RESTRICT"),
        nullable=True,
    )
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)

    automation: Mapped[Automation | None] = relationship(
        "Automation",
        back_populates="control_events",
    )
    run: Mapped[Run | None] = relationship("Run", back_populates="control_events")
