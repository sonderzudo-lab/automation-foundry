"""Read-only projections for the local server-rendered dashboard."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.platform.executor_registry import automation_supports_retry
from src.platform.models import (
    AlertStatus,
    Approval,
    ApprovalStatus,
    Artifact,
    Automation,
    Experiment,
    LedgerEntry,
    LedgerEntryType,
    MetricPoint,
    PlatformAlert,
    Run,
    RunStatus,
    Schedule,
    ScheduleOccurrence,
    ScheduleStatus,
    StepRun,
)

_SAFE_FAILURE_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,79}")
_SAFE_EXCEPTION_TYPE = re.compile(r"[A-Za-z][A-Za-z0-9_.]{0,99}")


@dataclass(frozen=True, slots=True)
class AutomationSummary:
    """Safe automation fields rendered by the initial dashboard."""

    id: int
    slug: str
    name: str
    enabled: bool
    kill_switch_active: bool


@dataclass(frozen=True, slots=True)
class RunSummary:
    """Redacted recent-run projection without inputs, outputs, or errors."""

    id: int
    automation_slug: str
    status: str
    trigger: str
    queued_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class AutomationExecutionSummary:
    """Recent completed-run indicators for one automation."""

    automation_slug: str
    sample_size: int
    succeeded_count: int
    success_rate_percent: Decimal | None
    duration_sample_size: int
    average_duration_seconds: Decimal | None
    sample_limit: int


@dataclass(frozen=True, slots=True)
class ApprovalSummary:
    """Redacted pending-approval projection without protected summaries."""

    id: int
    automation_slug: str
    run_id: int
    action: str
    requested_at: datetime
    pending_for_seconds: int
    pending_for_label: str


@dataclass(frozen=True, slots=True)
class AlertSummary:
    """Redacted active-alert projection without summaries or sources."""

    id: int
    automation_slug: str
    title: str
    status: str
    severity: str
    occurrence_count: int
    last_seen_at: datetime


@dataclass(frozen=True, slots=True)
class LedgerCurrencySummary:
    """Exact observed ledger totals for one currency, without conversion."""

    currency: str
    cost: Decimal
    revenue: Decimal
    attributed_value: Decimal
    net_revenue: Decimal


@dataclass(frozen=True, slots=True)
class AutomationLedgerSummary:
    """Exact ledger totals attributable to one automation and currency."""

    automation_slug: str
    currency: str
    cost: Decimal
    revenue: Decimal
    attributed_value: Decimal
    net_revenue: Decimal


@dataclass(frozen=True, slots=True)
class ExperimentSummary:
    """Operator-visible experiment definition without hypothesis or audit reasons."""

    id: int
    automation_slug: str
    key: str
    name: str
    status: str
    primary_metric: str
    primary_metric_unit: str
    control_variant: str
    candidate_variant: str
    started_at: datetime | None
    ended_at: datetime | None


@dataclass(frozen=True, slots=True)
class ScheduleSummary:
    """Redacted schedule state without trigger payloads or skip reasons."""

    id: int
    automation_slug: str
    name: str
    status: str
    cron_expression: str
    timezone: str
    next_run_at: datetime | None
    latest_occurrence_status: str | None
    latest_scheduled_for: datetime | None


@dataclass(frozen=True, slots=True)
class DashboardSnapshot:
    """Complete read-only state rendered on the dashboard home page."""

    automations: tuple[AutomationSummary, ...]
    recent_runs: tuple[RunSummary, ...]
    execution_summaries: tuple[AutomationExecutionSummary, ...]
    pending_approvals: tuple[ApprovalSummary, ...]
    active_alerts: tuple[AlertSummary, ...]
    ledger_totals: tuple[LedgerCurrencySummary, ...]
    automation_ledger_totals: tuple[AutomationLedgerSummary, ...]
    experiments: tuple[ExperimentSummary, ...]
    schedules: tuple[ScheduleSummary, ...]


@dataclass(frozen=True, slots=True)
class FailureSummary:
    """Allowlisted failure metadata without messages or payloads."""

    code: str
    exception_type: str | None


@dataclass(frozen=True, slots=True)
class StepRunSummary:
    """Safe attempt-level execution fields for one run."""

    id: int
    name: str
    queue: str
    ordinal: int
    attempt: int
    status: str
    started_at: datetime | None
    finished_at: datetime | None
    duration_seconds: Decimal | None
    failure: FailureSummary | None


@dataclass(frozen=True, slots=True)
class RunApprovalSummary:
    """Approval lifecycle fields safe for a run detail page."""

    id: int
    action: str
    status: str
    requested_at: datetime
    decided_at: datetime | None
    review_payload: dict[str, str] | None


@dataclass(frozen=True, slots=True)
class RunArtifactSummary:
    """Artifact metadata without filesystem paths, hashes, or origins."""

    id: int
    step_run_id: int | None
    artifact_type: str
    media_type: str
    size_bytes: int
    sensitivity: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RunMetricSummary:
    """Metric observation without source or free-form dimensions."""

    id: int
    step_run_id: int | None
    name: str
    kind: str
    value: Decimal
    unit: str
    confidence: Decimal | None
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class RunLedgerSummary:
    """Financial observation without category, source, or idempotency data."""

    id: int
    step_run_id: int | None
    metric_point_id: int | None
    entry_type: str
    amount: Decimal
    currency: str
    confidence: Decimal | None
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class RunDetail:
    """Redacted, read-only execution evidence for one persisted run."""

    id: int
    retry_of_run_id: int | None
    can_retry: bool
    experiment_id: int | None
    experiment_key: str | None
    experiment_name: str | None
    automation_slug: str
    automation_name: str
    status: str
    trigger: str
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    duration_seconds: Decimal | None
    cancellation_requested_at: datetime | None
    failure: FailureSummary | None
    steps: tuple[StepRunSummary, ...]
    approvals: tuple[RunApprovalSummary, ...]
    artifacts: tuple[RunArtifactSummary, ...]
    metrics: tuple[RunMetricSummary, ...]
    ledger_entries: tuple[RunLedgerSummary, ...]

    @property
    def can_request_cancellation(self) -> bool:
        """Return whether an operator may request cancellation now."""
        return self.cancellation_requested_at is None and RunStatus(self.status) not in {
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }

    @property
    def is_terminal(self) -> bool:
        """Return whether live dashboard polling can stop for this run."""
        return RunStatus(self.status) in {
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }


async def load_dashboard_snapshot(
    session: AsyncSession,
    *,
    recent_run_limit: int = 10,
    pending_limit: int = 20,
    alert_limit: int = 20,
    experiment_limit: int = 50,
    schedule_limit: int = 50,
    execution_sample_limit: int = 50,
    now: datetime | None = None,
) -> DashboardSnapshot:
    """Load a bounded, redacted dashboard snapshot from durable state."""
    if (
        recent_run_limit < 1
        or pending_limit < 1
        or alert_limit < 1
        or experiment_limit < 1
        or schedule_limit < 1
        or execution_sample_limit < 1
    ):
        raise ValueError("dashboard query limits must be positive")
    observed_at = _as_utc_naive(now or datetime.now(UTC))

    automations = tuple(
        AutomationSummary(
            id=automation.id,
            slug=automation.slug,
            name=automation.name,
            enabled=automation.enabled,
            kill_switch_active=automation.kill_switch_active,
        )
        for automation in await session.scalars(
            select(Automation).order_by(Automation.slug)
        )
    )

    run_rows = (
        await session.execute(
            select(Run, Automation.slug)
            .join(Automation, Automation.id == Run.automation_id)
            .order_by(Run.queued_at.desc(), Run.id.desc())
            .limit(recent_run_limit)
        )
    ).all()
    recent_runs = tuple(
        RunSummary(
            id=run.id,
            automation_slug=automation_slug,
            status=run.status,
            trigger=run.trigger,
            queued_at=run.queued_at,
            finished_at=run.finished_at,
        )
        for run, automation_slug in run_rows
    )

    ranked_runs = (
        select(
            Run.automation_id.label("automation_id"),
            Run.status.label("status"),
            Run.started_at.label("started_at"),
            Run.finished_at.label("finished_at"),
            func.row_number()
            .over(
                partition_by=Run.automation_id,
                order_by=(Run.finished_at.desc(), Run.id.desc()),
            )
            .label("sample_rank"),
        )
        .where(
            Run.status.in_(
                (
                    RunStatus.SUCCEEDED.value,
                    RunStatus.FAILED.value,
                )
            ),
            Run.finished_at.is_not(None),
        )
        .subquery()
    )
    execution_rows = (
        await session.execute(
            select(
                Automation.slug,
                ranked_runs.c.status,
                ranked_runs.c.started_at,
                ranked_runs.c.finished_at,
            )
            .join(
                ranked_runs,
                ranked_runs.c.automation_id == Automation.id,
            )
            .where(ranked_runs.c.sample_rank <= execution_sample_limit)
            .order_by(Automation.slug, ranked_runs.c.sample_rank)
        )
    ).all()
    execution_samples: dict[
        str,
        list[tuple[str, datetime | None, datetime | None]],
    ] = {}
    for automation_slug, status, started_at, finished_at in execution_rows:
        execution_samples.setdefault(automation_slug, []).append(
            (status, started_at, finished_at)
        )
    execution_summaries = tuple(
        _execution_summary(
            automation_slug=automation.slug,
            samples=execution_samples.get(automation.slug, []),
            sample_limit=execution_sample_limit,
        )
        for automation in automations
    )

    approval_rows = (
        await session.execute(
            select(Approval, Automation.slug)
            .join(Run, Run.id == Approval.run_id)
            .join(Automation, Automation.id == Run.automation_id)
            .where(Approval.status == ApprovalStatus.PENDING.value)
            .order_by(Approval.requested_at, Approval.id)
            .limit(pending_limit)
        )
    ).all()
    pending_approvals = tuple(
        _approval_summary(
            approval=approval,
            automation_slug=automation_slug,
            observed_at=observed_at,
        )
        for approval, automation_slug in approval_rows
    )

    alert_rows = (
        await session.execute(
            select(PlatformAlert, Automation.slug)
            .join(Automation, Automation.id == PlatformAlert.automation_id)
            .where(PlatformAlert.status != AlertStatus.RESOLVED.value)
            .order_by(PlatformAlert.last_seen_at.desc(), PlatformAlert.id.desc())
            .limit(alert_limit)
        )
    ).all()
    active_alerts = tuple(
        AlertSummary(
            id=alert.id,
            automation_slug=automation_slug,
            title=alert.title,
            status=alert.status,
            severity=alert.severity,
            occurrence_count=alert.occurrence_count,
            last_seen_at=alert.last_seen_at,
        )
        for alert, automation_slug in alert_rows
    )

    totals: dict[str, dict[LedgerEntryType, Decimal]] = {}
    automation_totals: dict[
        tuple[str, str],
        dict[LedgerEntryType, Decimal],
    ] = {}
    ledger_rows = (
        await session.execute(
            select(LedgerEntry, Automation.slug)
            .join(Automation, Automation.id == LedgerEntry.automation_id)
            .order_by(Automation.slug, LedgerEntry.currency, LedgerEntry.id)
        )
    ).all()
    for entry, automation_slug in ledger_rows:
        currency_totals = totals.setdefault(
            entry.currency,
            {entry_type: Decimal(0) for entry_type in LedgerEntryType},
        )
        currency_totals[LedgerEntryType(entry.entry_type)] += entry.amount
        attributed_totals = automation_totals.setdefault(
            (automation_slug, entry.currency),
            {entry_type: Decimal(0) for entry_type in LedgerEntryType},
        )
        attributed_totals[LedgerEntryType(entry.entry_type)] += entry.amount
    ledger_totals = tuple(
        LedgerCurrencySummary(
            currency=currency,
            cost=currency_totals[LedgerEntryType.COST],
            revenue=currency_totals[LedgerEntryType.REVENUE],
            attributed_value=currency_totals[LedgerEntryType.ATTRIBUTED_VALUE],
            net_revenue=(
                currency_totals[LedgerEntryType.REVENUE]
                - currency_totals[LedgerEntryType.COST]
            ),
        )
        for currency, currency_totals in sorted(totals.items())
    )
    automation_ledger_totals = tuple(
        AutomationLedgerSummary(
            automation_slug=automation_slug,
            currency=currency,
            cost=currency_totals[LedgerEntryType.COST],
            revenue=currency_totals[LedgerEntryType.REVENUE],
            attributed_value=currency_totals[LedgerEntryType.ATTRIBUTED_VALUE],
            net_revenue=(
                currency_totals[LedgerEntryType.REVENUE]
                - currency_totals[LedgerEntryType.COST]
            ),
        )
        for (
            automation_slug,
            currency,
        ), currency_totals in sorted(automation_totals.items())
    )

    experiment_rows = (
        await session.execute(
            select(Experiment, Automation.slug)
            .join(Automation, Automation.id == Experiment.automation_id)
            .order_by(Experiment.created_at.desc(), Experiment.id.desc())
            .limit(experiment_limit)
        )
    ).all()
    experiments = tuple(
        ExperimentSummary(
            id=experiment.id,
            automation_slug=automation_slug,
            key=experiment.key,
            name=experiment.name,
            status=experiment.status,
            primary_metric=experiment.primary_metric,
            primary_metric_unit=experiment.primary_metric_unit,
            control_variant=experiment.control_variant,
            candidate_variant=experiment.candidate_variant,
            started_at=experiment.started_at,
            ended_at=experiment.ended_at,
        )
        for experiment, automation_slug in experiment_rows
    )

    latest_occurrence_status = (
        select(ScheduleOccurrence.status)
        .where(ScheduleOccurrence.schedule_id == Schedule.id)
        .order_by(
            ScheduleOccurrence.scheduled_for.desc(),
            ScheduleOccurrence.id.desc(),
        )
        .limit(1)
        .correlate(Schedule)
        .scalar_subquery()
    )
    latest_scheduled_for = (
        select(ScheduleOccurrence.scheduled_for)
        .where(ScheduleOccurrence.schedule_id == Schedule.id)
        .order_by(
            ScheduleOccurrence.scheduled_for.desc(),
            ScheduleOccurrence.id.desc(),
        )
        .limit(1)
        .correlate(Schedule)
        .scalar_subquery()
    )
    schedule_rows = (
        await session.execute(
            select(
                Schedule,
                Automation.slug,
                latest_occurrence_status.label("latest_occurrence_status"),
                latest_scheduled_for.label("latest_scheduled_for"),
            )
            .join(Automation, Automation.id == Schedule.automation_id)
            .order_by(
                case(
                    (Schedule.status == ScheduleStatus.ENABLED.value, 0),
                    else_=1,
                ),
                case((Schedule.next_run_at.is_(None), 1), else_=0),
                Schedule.next_run_at,
                Automation.slug,
                Schedule.name,
                Schedule.id,
            )
            .limit(schedule_limit)
        )
    ).all()
    schedules = tuple(
        ScheduleSummary(
            id=schedule.id,
            automation_slug=automation_slug,
            name=schedule.name,
            status=schedule.status,
            cron_expression=schedule.cron_expression,
            timezone=schedule.timezone,
            next_run_at=schedule.next_run_at,
            latest_occurrence_status=occurrence_status,
            latest_scheduled_for=scheduled_for,
        )
        for schedule, automation_slug, occurrence_status, scheduled_for in schedule_rows
    )

    return DashboardSnapshot(
        automations=automations,
        recent_runs=recent_runs,
        execution_summaries=execution_summaries,
        pending_approvals=pending_approvals,
        active_alerts=active_alerts,
        ledger_totals=ledger_totals,
        automation_ledger_totals=automation_ledger_totals,
        experiments=experiments,
        schedules=schedules,
    )


async def load_run_detail(session: AsyncSession, *, run_id: int) -> RunDetail | None:
    """Load one redacted run and its directly attributable durable evidence."""
    if run_id < 1:
        return None

    run_row = (
        await session.execute(
            select(
                Run,
                Automation.slug,
                Automation.name,
                Experiment.key,
                Experiment.name,
            )
            .join(Automation, Automation.id == Run.automation_id)
            .outerjoin(Experiment, Experiment.id == Run.experiment_id)
            .where(Run.id == run_id)
        )
    ).first()
    if run_row is None:
        return None
    run, automation_slug, automation_name, experiment_key, experiment_name = run_row

    steps = tuple(
        StepRunSummary(
            id=step.id,
            name=step.name,
            queue=step.queue,
            ordinal=step.ordinal,
            attempt=step.attempt,
            status=step.status,
            started_at=step.started_at,
            finished_at=step.finished_at,
            duration_seconds=_duration_seconds(step.started_at, step.finished_at),
            failure=_redacted_failure(step.error),
        )
        for step in await session.scalars(
            select(StepRun)
            .where(StepRun.run_id == run_id)
            .order_by(StepRun.ordinal, StepRun.attempt, StepRun.id)
        )
    )
    approvals = tuple(
        RunApprovalSummary(
            id=approval.id,
            action=approval.action,
            status=approval.status,
            requested_at=approval.requested_at,
            decided_at=approval.decided_at,
            review_payload=approval.review_payload,
        )
        for approval in await session.scalars(
            select(Approval)
            .where(Approval.run_id == run_id)
            .order_by(Approval.requested_at, Approval.id)
        )
    )
    artifacts = tuple(
        RunArtifactSummary(
            id=artifact.id,
            step_run_id=artifact.step_run_id,
            artifact_type=artifact.artifact_type,
            media_type=artifact.media_type,
            size_bytes=artifact.size_bytes,
            sensitivity=artifact.sensitivity,
            created_at=artifact.created_at,
        )
        for artifact in await session.scalars(
            select(Artifact)
            .where(Artifact.run_id == run_id)
            .order_by(Artifact.created_at, Artifact.id)
        )
    )
    metrics = tuple(
        RunMetricSummary(
            id=metric.id,
            step_run_id=metric.step_run_id,
            name=metric.name,
            kind=metric.kind,
            value=metric.value,
            unit=metric.unit,
            confidence=metric.confidence,
            observed_at=metric.observed_at,
        )
        for metric in await session.scalars(
            select(MetricPoint)
            .where(MetricPoint.run_id == run_id)
            .order_by(MetricPoint.observed_at, MetricPoint.id)
        )
    )
    ledger_entries = tuple(
        RunLedgerSummary(
            id=entry.id,
            step_run_id=entry.step_run_id,
            metric_point_id=entry.metric_point_id,
            entry_type=entry.entry_type,
            amount=entry.amount,
            currency=entry.currency,
            confidence=entry.confidence,
            observed_at=entry.observed_at,
        )
        for entry in await session.scalars(
            select(LedgerEntry)
            .where(LedgerEntry.run_id == run_id)
            .order_by(LedgerEntry.observed_at, LedgerEntry.id)
        )
    )

    return RunDetail(
        id=run.id,
        retry_of_run_id=run.retry_of_run_id,
        can_retry=(
            RunStatus(run.status) is RunStatus.FAILED
            and isinstance(run.error, dict)
            and run.error.get("retryable") is True
            and automation_supports_retry(automation_slug)
        ),
        experiment_id=run.experiment_id,
        experiment_key=experiment_key,
        experiment_name=experiment_name,
        automation_slug=automation_slug,
        automation_name=automation_name,
        status=run.status,
        trigger=run.trigger,
        queued_at=run.queued_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        duration_seconds=_duration_seconds(run.started_at, run.finished_at),
        cancellation_requested_at=run.cancellation_requested_at,
        failure=_redacted_failure(run.error),
        steps=steps,
        approvals=approvals,
        artifacts=artifacts,
        metrics=metrics,
        ledger_entries=ledger_entries,
    )


def _duration_seconds(
    started_at: datetime | None,
    finished_at: datetime | None,
) -> Decimal | None:
    if started_at is None or finished_at is None or finished_at < started_at:
        return None
    duration = finished_at - started_at
    microseconds = (
        (duration.days * 86_400 + duration.seconds) * 1_000_000
        + duration.microseconds
    )
    return Decimal(microseconds) / Decimal(1_000_000)


def _execution_summary(
    *,
    automation_slug: str,
    samples: list[tuple[str, datetime | None, datetime | None]],
    sample_limit: int,
) -> AutomationExecutionSummary:
    sample_size = len(samples)
    succeeded_count = sum(
        status == RunStatus.SUCCEEDED.value for status, _, _ in samples
    )
    durations = [
        duration
        for _, started_at, finished_at in samples
        if (duration := _duration_seconds(started_at, finished_at)) is not None
    ]
    success_rate_percent = (
        (
            Decimal(succeeded_count)
            * Decimal(100)
            / Decimal(sample_size)
        ).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
        if sample_size
        else None
    )
    average_duration_seconds = (
        (sum(durations, Decimal(0)) / Decimal(len(durations))).quantize(
            Decimal("0.001"),
            rounding=ROUND_HALF_UP,
        )
        if durations
        else None
    )
    return AutomationExecutionSummary(
        automation_slug=automation_slug,
        sample_size=sample_size,
        succeeded_count=succeeded_count,
        success_rate_percent=success_rate_percent,
        duration_sample_size=len(durations),
        average_duration_seconds=average_duration_seconds,
        sample_limit=sample_limit,
    )


def _approval_summary(
    *,
    approval: Approval,
    automation_slug: str,
    observed_at: datetime,
) -> ApprovalSummary:
    requested_at = _as_utc_naive(approval.requested_at)
    pending_for_seconds = max(
        0,
        int((observed_at - requested_at).total_seconds()),
    )
    return ApprovalSummary(
        id=approval.id,
        automation_slug=automation_slug,
        run_id=approval.run_id,
        action=approval.action,
        requested_at=requested_at,
        pending_for_seconds=pending_for_seconds,
        pending_for_label=_format_pending_age(pending_for_seconds),
    )


def _as_utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _format_pending_age(total_seconds: int) -> str:
    if total_seconds < 60:
        return "menos de 1 min"
    if total_seconds < 3_600:
        return f"{total_seconds // 60} min"
    if total_seconds < 86_400:
        hours, remainder = divmod(total_seconds, 3_600)
        minutes = remainder // 60
        return f"{hours} h {minutes} min" if minutes else f"{hours} h"
    days, remainder = divmod(total_seconds, 86_400)
    hours = remainder // 3_600
    return f"{days} d {hours} h" if hours else f"{days} d"


def _redacted_failure(error: dict[str, object] | None) -> FailureSummary | None:
    if error is None:
        return None
    code = error.get("code")
    exception_type = error.get("exception_type")
    safe_code = (
        code
        if isinstance(code, str) and _SAFE_FAILURE_CODE.fullmatch(code)
        else "REDACTED_FAILURE"
    )
    safe_exception_type = (
        exception_type
        if isinstance(exception_type, str)
        and _SAFE_EXCEPTION_TYPE.fullmatch(exception_type)
        else None
    )
    return FailureSummary(code=safe_code, exception_type=safe_exception_type)
