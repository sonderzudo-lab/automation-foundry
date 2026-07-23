"""Read-only projections for the local server-rendered dashboard."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.platform.models import (
    AlertStatus,
    Approval,
    ApprovalStatus,
    Artifact,
    Automation,
    LedgerEntry,
    LedgerEntryType,
    MetricPoint,
    PlatformAlert,
    Run,
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
class ApprovalSummary:
    """Redacted pending-approval projection without protected summaries."""

    id: int
    automation_slug: str
    run_id: int
    action: str
    requested_at: datetime


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
class DashboardSnapshot:
    """Complete read-only state rendered on the dashboard home page."""

    automations: tuple[AutomationSummary, ...]
    recent_runs: tuple[RunSummary, ...]
    pending_approvals: tuple[ApprovalSummary, ...]
    active_alerts: tuple[AlertSummary, ...]
    ledger_totals: tuple[LedgerCurrencySummary, ...]


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
    automation_slug: str
    automation_name: str
    status: str
    trigger: str
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    duration_seconds: Decimal | None
    failure: FailureSummary | None
    steps: tuple[StepRunSummary, ...]
    approvals: tuple[RunApprovalSummary, ...]
    artifacts: tuple[RunArtifactSummary, ...]
    metrics: tuple[RunMetricSummary, ...]
    ledger_entries: tuple[RunLedgerSummary, ...]


async def load_dashboard_snapshot(
    session: AsyncSession,
    *,
    recent_run_limit: int = 10,
    pending_limit: int = 20,
    alert_limit: int = 20,
) -> DashboardSnapshot:
    """Load a bounded, redacted dashboard snapshot from durable state."""
    if recent_run_limit < 1 or pending_limit < 1 or alert_limit < 1:
        raise ValueError("dashboard query limits must be positive")

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
        ApprovalSummary(
            id=approval.id,
            automation_slug=automation_slug,
            run_id=approval.run_id,
            action=approval.action,
            requested_at=approval.requested_at,
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
    ledger_entries = await session.scalars(
        select(LedgerEntry).order_by(LedgerEntry.id)
    )
    for entry in ledger_entries:
        currency_totals = totals.setdefault(
            entry.currency,
            {entry_type: Decimal(0) for entry_type in LedgerEntryType},
        )
        currency_totals[LedgerEntryType(entry.entry_type)] += entry.amount
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

    return DashboardSnapshot(
        automations=automations,
        recent_runs=recent_runs,
        pending_approvals=pending_approvals,
        active_alerts=active_alerts,
        ledger_totals=ledger_totals,
    )


async def load_run_detail(session: AsyncSession, *, run_id: int) -> RunDetail | None:
    """Load one redacted run and its directly attributable durable evidence."""
    if run_id < 1:
        return None

    run_row = (
        await session.execute(
            select(Run, Automation.slug, Automation.name)
            .join(Automation, Automation.id == Run.automation_id)
            .where(Run.id == run_id)
        )
    ).first()
    if run_row is None:
        return None
    run, automation_slug, automation_name = run_row

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
        automation_slug=automation_slug,
        automation_name=automation_name,
        status=run.status,
        trigger=run.trigger,
        queued_at=run.queued_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        duration_seconds=_duration_seconds(run.started_at, run.finished_at),
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
