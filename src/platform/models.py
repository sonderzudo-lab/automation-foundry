"""Durable shared models for the Automation Foundry run kernel."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator, TypeEngine

from src.core.database import Base


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class _ExactNumeric(TypeDecorator[Decimal]):
    """Use exact decimal text on SQLite and native NUMERIC elsewhere."""

    impl = Numeric
    cache_ok = True

    def __init__(self, precision: int, scale: int) -> None:
        self._precision = precision
        self._scale = scale
        super().__init__(precision=precision, scale=scale)

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "sqlite":
            return dialect.type_descriptor(String(self._precision + 2))
        return dialect.type_descriptor(Numeric(self._precision, self._scale))

    def process_bind_param(
        self,
        value: Decimal | None,
        dialect: Dialect,
    ) -> Decimal | str | None:
        if value is None or dialect.name != "sqlite":
            return value
        return format(value, "f")

    def process_result_value(
        self,
        value: Decimal | str | None,
        _dialect: Dialect,
    ) -> Decimal | None:
        if value is None or isinstance(value, Decimal):
            return value
        return Decimal(value)


class RunStatus(StrEnum):
    """States supported by the first durable run lifecycle slice."""

    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
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


class ApprovalStatus(StrEnum):
    """Immutable decision states for a human approval gate."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class ArtifactSensitivity(StrEnum):
    """Operator-visible sensitivity labels for durable local artifacts."""

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class ScheduleStatus(StrEnum):
    """Operator-controlled states for a persistent schedule."""

    DISABLED = "disabled"
    ENABLED = "enabled"


class ScheduleEventType(StrEnum):
    """Append-only schedule lifecycle events."""

    CREATED = "created"
    ENABLED = "enabled"
    DISABLED = "disabled"


class MetricKind(StrEnum):
    """Semantic value types supported by durable metric observations."""

    COUNTER = "counter"
    GAUGE = "gauge"
    DURATION = "duration"
    RATIO = "ratio"
    CURRENCY = "currency"


class LedgerEntryType(StrEnum):
    """Financial observation types supported by the shared ledger."""

    COST = "cost"
    REVENUE = "revenue"
    ATTRIBUTED_VALUE = "attributed_value"


class AlertSeverity(StrEnum):
    """Operator-visible impact levels for platform alerts."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class AlertStatus(StrEnum):
    """Lifecycle states for a deduplicated platform alert."""

    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"


class AlertEventType(StrEnum):
    """Append-only events supported by the alert lifecycle."""

    OPENED = "opened"
    OCCURRED = "occurred"
    REOPENED = "reopened"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"


_RUN_STATUS_SQL = (
    "'queued', 'running', 'awaiting_approval', 'succeeded', 'failed', 'cancelled'"
)
_STEP_RUN_STATUS_SQL = "'queued', 'running', 'succeeded', 'failed', 'cancelled'"
_QUEUE_CLASS_SQL = "'gpu', 'cpu', 'io'"
_APPROVAL_STATUS_SQL = "'pending', 'approved', 'rejected', 'cancelled'"
_ARTIFACT_SENSITIVITY_SQL = "'public', 'internal', 'confidential', 'restricted'"
_SCHEDULE_STATUS_SQL = "'disabled', 'enabled'"
_SCHEDULE_EVENT_TYPE_SQL = "'created', 'enabled', 'disabled'"
_METRIC_KIND_SQL = "'counter', 'gauge', 'duration', 'ratio', 'currency'"
_LEDGER_ENTRY_TYPE_SQL = "'cost', 'revenue', 'attributed_value'"
_ALERT_SEVERITY_SQL = "'info', 'warning', 'error', 'critical'"
_ALERT_STATUS_SQL = "'open', 'acknowledged', 'resolved'"
_ALERT_EVENT_TYPE_SQL = (
    "'opened', 'occurred', 'reopened', 'acknowledged', 'resolved'"
)
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
    schedules: Mapped[list[Schedule]] = relationship(
        "Schedule",
        back_populates="automation",
        order_by="Schedule.id",
    )
    metric_points: Mapped[list[MetricPoint]] = relationship(
        "MetricPoint",
        back_populates="automation",
        order_by="MetricPoint.id",
    )
    ledger_entries: Mapped[list[LedgerEntry]] = relationship(
        "LedgerEntry",
        back_populates="automation",
        order_by="LedgerEntry.id",
    )
    alerts: Mapped[list[PlatformAlert]] = relationship(
        "PlatformAlert",
        back_populates="automation",
        order_by="PlatformAlert.id",
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
    approvals: Mapped[list[Approval]] = relationship(
        "Approval",
        back_populates="run",
        order_by="Approval.id",
    )
    artifacts: Mapped[list[Artifact]] = relationship(
        "Artifact",
        back_populates="run",
        order_by="Artifact.id",
    )
    metric_points: Mapped[list[MetricPoint]] = relationship(
        "MetricPoint",
        back_populates="run",
        order_by="MetricPoint.id",
    )
    ledger_entries: Mapped[list[LedgerEntry]] = relationship(
        "LedgerEntry",
        back_populates="run",
        order_by="LedgerEntry.id",
    )
    alerts: Mapped[list[PlatformAlert]] = relationship(
        "PlatformAlert",
        back_populates="run",
        order_by="PlatformAlert.id",
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
        CheckConstraint(
            f"status IN ({_STEP_RUN_STATUS_SQL})",
            name="ck_step_runs_status",
        ),
        CheckConstraint("ordinal >= 1", name="ck_step_runs_ordinal_positive"),
        CheckConstraint("attempt >= 1", name="ck_step_runs_attempt_positive"),
        Index("ix_step_runs_run_status_ordinal", "run_id", "status", "ordinal"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    approval_id: Mapped[int | None] = mapped_column(
        ForeignKey("approvals.id", ondelete="RESTRICT"),
        nullable=True,
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
    approval: Mapped[Approval | None] = relationship(
        "Approval",
        back_populates="step_runs",
    )
    transitions: Mapped[list[StepRunTransition]] = relationship(
        "StepRunTransition",
        back_populates="step_run",
        order_by="StepRunTransition.id",
    )
    artifacts: Mapped[list[Artifact]] = relationship(
        "Artifact",
        back_populates="step_run",
        order_by="Artifact.id",
    )
    metric_points: Mapped[list[MetricPoint]] = relationship(
        "MetricPoint",
        back_populates="step_run",
        order_by="MetricPoint.id",
    )
    ledger_entries: Mapped[list[LedgerEntry]] = relationship(
        "LedgerEntry",
        back_populates="step_run",
        order_by="LedgerEntry.id",
    )
    alerts: Mapped[list[PlatformAlert]] = relationship(
        "PlatformAlert",
        back_populates="step_run",
        order_by="PlatformAlert.id",
    )


class StepRunTransition(Base):
    """Append-only evidence of one persisted step state transition."""

    __tablename__ = "step_run_transitions"
    __table_args__ = (
        CheckConstraint(
            f"from_status IS NULL OR from_status IN ({_STEP_RUN_STATUS_SQL})",
            name="ck_step_run_transitions_from_status",
        ),
        CheckConstraint(
            f"to_status IN ({_STEP_RUN_STATUS_SQL})",
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


class Artifact(Base):
    """Verified metadata for one file stored below the configured local root."""

    __tablename__ = "artifacts"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "idempotency_key",
            name="uq_artifacts_run_idempotency_key",
        ),
        CheckConstraint("size_bytes >= 0", name="ck_artifacts_size_nonnegative"),
        CheckConstraint("length(sha256) = 64", name="ck_artifacts_sha256_length"),
        CheckConstraint(
            "retention_days IS NULL OR retention_days >= 1",
            name="ck_artifacts_retention_days_positive",
        ),
        CheckConstraint(
            f"sensitivity IN ({_ARTIFACT_SENSITIVITY_SQL})",
            name="ck_artifacts_sensitivity",
        ),
        Index("ix_artifacts_run_created", "run_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    step_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("step_runs.id", ondelete="RESTRICT"),
        nullable=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    artifact_type: Mapped[str] = mapped_column(String(100), nullable=False)
    relative_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    origin: Mapped[str] = mapped_column(String(200), nullable=False)
    sensitivity: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default=ArtifactSensitivity.INTERNAL.value,
    )
    retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retention_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)
    verified_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)

    run: Mapped[Run] = relationship("Run", back_populates="artifacts")
    step_run: Mapped[StepRun | None] = relationship(
        "StepRun",
        back_populates="artifacts",
    )


class Schedule(Base):
    """A validated cron definition whose activation is controlled by an operator."""

    __tablename__ = "schedules"
    __table_args__ = (
        UniqueConstraint(
            "automation_id",
            "name",
            name="uq_schedules_automation_name",
        ),
        CheckConstraint(
            f"status IN ({_SCHEDULE_STATUS_SQL})",
            name="ck_schedules_status",
        ),
        CheckConstraint(
            "misfire_grace_seconds >= 0",
            name="ck_schedules_misfire_grace_nonnegative",
        ),
        CheckConstraint(
            "(status = 'disabled' AND next_run_at IS NULL) OR "
            "(status = 'enabled' AND next_run_at IS NOT NULL)",
            name="ck_schedules_status_next_run",
        ),
        Index("ix_schedules_status_next_run", "status", "next_run_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    automation_id: Mapped[int] = mapped_column(
        ForeignKey("automations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    cron_expression: Mapped[str] = mapped_column(String(100), nullable=False)
    timezone: Mapped[str] = mapped_column(String(100), nullable=False)
    input_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default=ScheduleStatus.DISABLED.value,
    )
    allow_overlap: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    misfire_grace_seconds: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=300,
    )
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_enqueued_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)

    automation: Mapped[Automation] = relationship(
        "Automation",
        back_populates="schedules",
    )
    events: Mapped[list[ScheduleEvent]] = relationship(
        "ScheduleEvent",
        back_populates="schedule",
        order_by="ScheduleEvent.id",
    )


class ScheduleEvent(Base):
    """Append-only evidence for schedule creation and operator state changes."""

    __tablename__ = "schedule_events"
    __table_args__ = (
        CheckConstraint(
            f"event_type IN ({_SCHEDULE_EVENT_TYPE_SQL})",
            name="ck_schedule_events_type",
        ),
        CheckConstraint(
            f"from_status IS NULL OR from_status IN ({_SCHEDULE_STATUS_SQL})",
            name="ck_schedule_events_from_status",
        ),
        CheckConstraint(
            f"to_status IN ({_SCHEDULE_STATUS_SQL})",
            name="ck_schedule_events_to_status",
        ),
        CheckConstraint(
            "(event_type = 'created' AND from_status IS NULL AND "
            "to_status = 'disabled') OR "
            "(event_type = 'enabled' AND from_status = 'disabled' AND "
            "to_status = 'enabled') OR "
            "(event_type = 'disabled' AND from_status = 'enabled' AND "
            "to_status = 'disabled')",
            name="ck_schedule_events_transition",
        ),
        Index(
            "ix_schedule_events_schedule_occurred",
            "schedule_id",
            "occurred_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    schedule_id: Mapped[int] = mapped_column(
        ForeignKey("schedules.id", ondelete="RESTRICT"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    to_status: Mapped[str] = mapped_column(String(30), nullable=False)
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    previous_next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)

    schedule: Mapped[Schedule] = relationship("Schedule", back_populates="events")


class MetricPoint(Base):
    """One immutable, attributable observation recorded by a local automation."""

    __tablename__ = "metric_points"
    __table_args__ = (
        UniqueConstraint(
            "automation_id",
            "idempotency_key",
            name="uq_metric_points_automation_idempotency_key",
        ),
        CheckConstraint(
            f"kind IN ({_METRIC_KIND_SQL})",
            name="ck_metric_points_kind",
        ),
        CheckConstraint(
            "confidence IS NULL OR "
            "(CAST(confidence AS NUMERIC) >= 0 AND "
            "CAST(confidence AS NUMERIC) <= 1)",
            name="ck_metric_points_confidence_range",
        ),
        CheckConstraint(
            "step_run_id IS NULL OR run_id IS NOT NULL",
            name="ck_metric_points_step_requires_run",
        ),
        CheckConstraint(
            "kind NOT IN ('counter', 'duration') OR "
            "CAST(value AS NUMERIC) >= 0",
            name="ck_metric_points_nonnegative_kind",
        ),
        CheckConstraint(
            "kind != 'ratio' OR (CAST(value AS NUMERIC) >= 0 AND "
            "CAST(value AS NUMERIC) <= 1)",
            name="ck_metric_points_ratio_range",
        ),
        Index(
            "ix_metric_points_automation_name_observed",
            "automation_id",
            "name",
            "observed_at",
        ),
        Index("ix_metric_points_run_observed", "run_id", "observed_at"),
        Index("ix_metric_points_step_observed", "step_run_id", "observed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    automation_id: Mapped[int] = mapped_column(
        ForeignKey("automations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("runs.id", ondelete="RESTRICT"),
        nullable=True,
    )
    step_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("step_runs.id", ondelete="RESTRICT"),
        nullable=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    value: Mapped[Decimal] = mapped_column(_ExactNumeric(30, 10), nullable=False)
    unit: Mapped[str] = mapped_column(String(50), nullable=False)
    source: Mapped[str] = mapped_column(String(200), nullable=False)
    confidence: Mapped[Decimal | None] = mapped_column(
        _ExactNumeric(5, 4),
        nullable=True,
    )
    dimensions: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    observed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)

    automation: Mapped[Automation] = relationship(
        "Automation",
        back_populates="metric_points",
    )
    run: Mapped[Run | None] = relationship("Run", back_populates="metric_points")
    step_run: Mapped[StepRun | None] = relationship(
        "StepRun",
        back_populates="metric_points",
    )
    alerts: Mapped[list[PlatformAlert]] = relationship(
        "PlatformAlert",
        back_populates="metric_point",
        order_by="PlatformAlert.id",
    )
    ledger_entries: Mapped[list[LedgerEntry]] = relationship(
        "LedgerEntry",
        back_populates="metric_point",
        order_by="LedgerEntry.id",
    )


class LedgerEntry(Base):
    """One immutable financial observation with explicit attribution."""

    __tablename__ = "ledger_entries"
    __table_args__ = (
        UniqueConstraint(
            "automation_id",
            "idempotency_key",
            name="uq_ledger_entries_automation_idempotency_key",
        ),
        CheckConstraint(
            f"entry_type IN ({_LEDGER_ENTRY_TYPE_SQL})",
            name="ck_ledger_entries_type",
        ),
        CheckConstraint(
            "confidence IS NULL OR "
            "(CAST(confidence AS NUMERIC) >= 0 AND "
            "CAST(confidence AS NUMERIC) <= 1)",
            name="ck_ledger_entries_confidence_range",
        ),
        CheckConstraint(
            "step_run_id IS NULL OR run_id IS NOT NULL",
            name="ck_ledger_entries_step_requires_run",
        ),
        CheckConstraint(
            "LENGTH(currency) = 3 AND currency = UPPER(currency)",
            name="ck_ledger_entries_currency",
        ),
        Index(
            "ix_ledger_entries_automation_type_observed",
            "automation_id",
            "entry_type",
            "observed_at",
        ),
        Index("ix_ledger_entries_run_observed", "run_id", "observed_at"),
        Index("ix_ledger_entries_step_observed", "step_run_id", "observed_at"),
        Index("ix_ledger_entries_metric", "metric_point_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    automation_id: Mapped[int] = mapped_column(
        ForeignKey("automations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("runs.id", ondelete="RESTRICT"),
        nullable=True,
    )
    step_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("step_runs.id", ondelete="RESTRICT"),
        nullable=True,
    )
    metric_point_id: Mapped[int | None] = mapped_column(
        ForeignKey("metric_points.id", ondelete="RESTRICT"),
        nullable=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    entry_type: Mapped[str] = mapped_column(String(30), nullable=False)
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    amount: Mapped[Decimal] = mapped_column(_ExactNumeric(30, 10), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    source: Mapped[str] = mapped_column(String(200), nullable=False)
    confidence: Mapped[Decimal | None] = mapped_column(
        _ExactNumeric(5, 4),
        nullable=True,
    )
    observed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)

    automation: Mapped[Automation] = relationship(
        "Automation",
        back_populates="ledger_entries",
    )
    run: Mapped[Run | None] = relationship("Run", back_populates="ledger_entries")
    step_run: Mapped[StepRun | None] = relationship(
        "StepRun",
        back_populates="ledger_entries",
    )
    metric_point: Mapped[MetricPoint | None] = relationship(
        "MetricPoint",
        back_populates="ledger_entries",
    )


class PlatformAlert(Base):
    """One deduplicated operational condition in the shared platform kernel."""

    __tablename__ = "platform_alerts"
    __table_args__ = (
        UniqueConstraint(
            "automation_id",
            "deduplication_key",
            name="uq_platform_alerts_automation_deduplication",
        ),
        CheckConstraint(
            f"severity IN ({_ALERT_SEVERITY_SQL})",
            name="ck_platform_alerts_severity",
        ),
        CheckConstraint(
            f"status IN ({_ALERT_STATUS_SQL})",
            name="ck_platform_alerts_status",
        ),
        CheckConstraint(
            "occurrence_count >= 1",
            name="ck_platform_alerts_occurrence_count_positive",
        ),
        CheckConstraint(
            "last_seen_at >= first_seen_at",
            name="ck_platform_alerts_seen_order",
        ),
        CheckConstraint(
            "step_run_id IS NULL OR run_id IS NOT NULL",
            name="ck_platform_alerts_step_requires_run",
        ),
        CheckConstraint(
            "((acknowledged_at IS NULL AND acknowledged_by IS NULL AND "
            "acknowledgement_reason IS NULL) OR "
            "(acknowledged_at IS NOT NULL AND acknowledged_by IS NOT NULL AND "
            "acknowledgement_reason IS NOT NULL))",
            name="ck_platform_alerts_acknowledgement_complete",
        ),
        CheckConstraint(
            "((resolved_at IS NULL AND resolved_by IS NULL AND "
            "resolution_reason IS NULL) OR "
            "(resolved_at IS NOT NULL AND resolved_by IS NOT NULL AND "
            "resolution_reason IS NOT NULL))",
            name="ck_platform_alerts_resolution_complete",
        ),
        CheckConstraint(
            "(status = 'open' AND acknowledged_at IS NULL AND resolved_at IS NULL) "
            "OR (status = 'acknowledged' AND acknowledged_at IS NOT NULL AND "
            "resolved_at IS NULL) OR "
            "(status = 'resolved' AND resolved_at IS NOT NULL)",
            name="ck_platform_alerts_status_metadata",
        ),
        Index(
            "ix_platform_alerts_status_severity_seen",
            "status",
            "severity",
            "last_seen_at",
        ),
        Index(
            "ix_platform_alerts_automation_status",
            "automation_id",
            "status",
        ),
        Index("ix_platform_alerts_run_status", "run_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    automation_id: Mapped[int] = mapped_column(
        ForeignKey("automations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    run_id: Mapped[int | None] = mapped_column(
        ForeignKey("runs.id", ondelete="RESTRICT"),
        nullable=True,
    )
    step_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("step_runs.id", ondelete="RESTRICT"),
        nullable=True,
    )
    metric_point_id: Mapped[int | None] = mapped_column(
        ForeignKey("metric_points.id", ondelete="RESTRICT"),
        nullable=True,
    )
    deduplication_key: Mapped[str] = mapped_column(String(255), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default=AlertStatus.OPEN.value,
    )
    source: Mapped[str] = mapped_column(String(200), nullable=False)
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    acknowledged_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    acknowledgement_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    resolution_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)

    automation: Mapped[Automation] = relationship(
        "Automation",
        back_populates="alerts",
    )
    run: Mapped[Run | None] = relationship("Run", back_populates="alerts")
    step_run: Mapped[StepRun | None] = relationship(
        "StepRun",
        back_populates="alerts",
    )
    metric_point: Mapped[MetricPoint | None] = relationship(
        "MetricPoint",
        back_populates="alerts",
    )
    events: Mapped[list[AlertEvent]] = relationship(
        "AlertEvent",
        back_populates="alert",
        order_by="AlertEvent.id",
    )


class AlertEvent(Base):
    """Append-only evidence for alert occurrences and operator transitions."""

    __tablename__ = "platform_alert_events"
    __table_args__ = (
        UniqueConstraint(
            "alert_id",
            "idempotency_key",
            name="uq_platform_alert_events_alert_idempotency",
        ),
        CheckConstraint(
            f"event_type IN ({_ALERT_EVENT_TYPE_SQL})",
            name="ck_platform_alert_events_type",
        ),
        CheckConstraint(
            f"from_status IS NULL OR from_status IN ({_ALERT_STATUS_SQL})",
            name="ck_platform_alert_events_from_status",
        ),
        CheckConstraint(
            f"to_status IN ({_ALERT_STATUS_SQL})",
            name="ck_platform_alert_events_to_status",
        ),
        CheckConstraint(
            f"severity IN ({_ALERT_SEVERITY_SQL})",
            name="ck_platform_alert_events_severity",
        ),
        CheckConstraint(
            "(event_type IN ('opened', 'occurred', 'reopened') AND "
            "idempotency_key IS NOT NULL) OR "
            "(event_type IN ('acknowledged', 'resolved') AND "
            "idempotency_key IS NULL)",
            name="ck_platform_alert_events_idempotency_scope",
        ),
        CheckConstraint(
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
        Index(
            "ix_platform_alert_events_alert_occurred",
            "alert_id",
            "occurred_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    alert_id: Mapped[int] = mapped_column(
        ForeignKey("platform_alerts.id", ondelete="RESTRICT"),
        nullable=False,
    )
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    to_status: Mapped[str] = mapped_column(String(30), nullable=False)
    severity: Mapped[str] = mapped_column(String(30), nullable=False)
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)

    alert: Mapped[PlatformAlert] = relationship(
        "PlatformAlert",
        back_populates="events",
    )


Alert = PlatformAlert


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


class Approval(Base):
    """One immutable human decision gate for a protected run action."""

    __tablename__ = "approvals"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "idempotency_key",
            name="uq_approvals_run_idempotency_key",
        ),
        CheckConstraint(
            f"status IN ({_APPROVAL_STATUS_SQL})",
            name="ck_approvals_status",
        ),
        Index("ix_approvals_status_requested", "status", "requested_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default=ApprovalStatus.PENDING.value,
    )
    requested_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    run: Mapped[Run] = relationship("Run", back_populates="approvals")
    events: Mapped[list[ApprovalEvent]] = relationship(
        "ApprovalEvent",
        back_populates="approval",
        order_by="ApprovalEvent.id",
    )
    step_runs: Mapped[list[StepRun]] = relationship(
        "StepRun",
        back_populates="approval",
        order_by="StepRun.id",
    )


class ApprovalEvent(Base):
    """Append-only evidence for approval creation and final decision."""

    __tablename__ = "approval_events"
    __table_args__ = (
        CheckConstraint(
            f"from_status IS NULL OR from_status IN ({_APPROVAL_STATUS_SQL})",
            name="ck_approval_events_from_status",
        ),
        CheckConstraint(
            f"to_status IN ({_APPROVAL_STATUS_SQL})",
            name="ck_approval_events_to_status",
        ),
        Index(
            "ix_approval_events_approval_occurred",
            "approval_id",
            "occurred_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    approval_id: Mapped[int] = mapped_column(
        ForeignKey("approvals.id", ondelete="RESTRICT"),
        nullable=False,
    )
    from_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    to_status: Mapped[str] = mapped_column(String(30), nullable=False)
    actor: Mapped[str | None] = mapped_column(String(200), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)

    approval: Mapped[Approval] = relationship("Approval", back_populates="events")
