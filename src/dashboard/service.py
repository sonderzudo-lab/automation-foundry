"""Read-only projections for the local server-rendered dashboard."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.platform.executor_registry import (
    automation_has_executor,
    automation_supports_retry,
)
from src.platform.models import (
    AlertStatus,
    Approval,
    ApprovalStatus,
    Artifact,
    Automation,
    ConnectorObservation,
    ConnectorStatus,
    Experiment,
    LedgerEntry,
    LedgerEntryType,
    MetricKind,
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
_SCHEDULE_SKIP_NOTES = {
    "AUTOMATION_DISABLED": "módulo desabilitado no horário previsto",
    "EXECUTOR_NOT_REGISTERED": "executor local não registrado",
    "KILL_SWITCH_ACTIVE": "kill switch ativo no horário previsto",
    "MISFIRE_GRACE_EXCEEDED": "janela de tolerância excedida",
    "OVERLAP_BLOCKED": "execução anterior ainda ativa",
}
_CONTENT_OUTCOME_METRICS = {
    "content_script_narration_characters": (
        "Caracteres da narração",
        MetricKind.COUNTER.value,
        "characters",
    ),
    "content_tts_audio_duration_seconds": (
        "Duração do áudio",
        MetricKind.DURATION.value,
        "seconds",
    ),
    "content_visual_asset_count": (
        "Imagens produzidas",
        MetricKind.COUNTER.value,
        "images",
    ),
    "content_visual_seconds_per_asset": (
        "Segundos por imagem",
        MetricKind.GAUGE.value,
        "seconds_per_image",
    ),
    "content_caption_timed_word_count": (
        "Palavras temporizadas",
        MetricKind.COUNTER.value,
        "words",
    ),
    "content_caption_transcript_match_ratio": (
        "Correspondência da legenda",
        MetricKind.GAUGE.value,
        "ratio",
    ),
    "content_caption_alignment_speech_coverage_ratio": (
        "Fala coberta pela legenda",
        MetricKind.RATIO.value,
        "ratio",
    ),
    "content_caption_alignment_outside_speech_ratio": (
        "Legenda sobre silêncio",
        MetricKind.RATIO.value,
        "ratio",
    ),
    "content_caption_alignment_onset_offset_seconds": (
        "Desvio no início da legenda",
        MetricKind.GAUGE.value,
        "seconds",
    ),
    "content_caption_alignment_end_offset_seconds": (
        "Desvio no fim da legenda",
        MetricKind.GAUGE.value,
        "seconds",
    ),
    "content_caption_alignment_speech_segment_count": (
        "Trechos de fala detectados",
        MetricKind.COUNTER.value,
        "segments",
    ),
    "content_assembly_video_duration_seconds": (
        "Duração do vídeo",
        MetricKind.DURATION.value,
        "seconds",
    ),
    "content_assembly_render_duration_seconds": (
        "Tempo de render",
        MetricKind.DURATION.value,
        "seconds",
    ),
    "content_assembly_output_bytes": (
        "Tamanho do vídeo",
        MetricKind.GAUGE.value,
        "bytes",
    ),
    "content_originality_max_similarity": (
        "Similaridade máxima",
        MetricKind.RATIO.value,
        "ratio",
    ),
    "content_originality_mean_similarity": (
        "Similaridade média",
        MetricKind.RATIO.value,
        "ratio",
    ),
    "content_originality_reference_count": (
        "Referências comparadas",
        MetricKind.COUNTER.value,
        "scripts",
    ),
    "content_originality_evaluation_duration_seconds": (
        "Tempo da avaliação de originalidade",
        MetricKind.DURATION.value,
        "seconds",
    ),
}
_CONTENT_ENERGY_METRICS = frozenset(
    {
        "content_tts_energy_estimate_kwh",
        "content_visual_energy_estimate_kwh",
        "content_caption_energy_estimate_kwh",
        "content_assembly_energy_estimate_kwh",
        "content_originality_energy_estimate_kwh",
    }
)


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
    """Redacted schedule state with allowlisted operational diagnostics."""

    id: int
    automation_slug: str
    name: str
    status: str
    effective_status: str
    status_note: str | None
    enable_block_reason: str | None
    cron_expression: str
    timezone: str
    next_run_at: datetime | None
    latest_occurrence_status: str | None
    latest_scheduled_for: datetime | None
    latest_occurrence_note: str | None


@dataclass(frozen=True, slots=True)
class ConnectorSummary:
    """Latest redacted connector, freshness, and data-quality state."""

    automation_slug: str
    connector_key: str
    reported_status: str
    effective_status: str
    observation_age_seconds: int
    observation_age_label: str
    status_slo_seconds: int
    status_slo_label: str
    last_success_at: datetime | None
    data_age_seconds: int | None
    data_age_label: str
    freshness_status: str
    freshness_slo_seconds: int
    freshness_slo_label: str
    quality_status: str
    quality_score: Decimal | None
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class ConnectorSummaryPage:
    """Bounded connector projection with explicit truncation evidence."""

    items: tuple[ConnectorSummary, ...]
    truncated: bool


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
    connectors: tuple[ConnectorSummary, ...]
    connectors_truncated: bool


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
class ContentOutcomeIndicator:
    """Allowlisted local Content Engine result without metric dimensions."""

    step_run_id: int | None
    name: str
    label: str
    value: Decimal
    unit: str
    confidence: Decimal | None
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class ContentOutcomeSummary:
    """Run-attributed local content outcome; never external platform analytics."""

    published: bool | None
    observed_through: datetime | None
    indicators: tuple[ContentOutcomeIndicator, ...]
    estimated_energy_kwh: Decimal | None
    ledger_totals: tuple[LedgerCurrencySummary, ...]


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
    has_local_export: bool
    failure: FailureSummary | None
    steps: tuple[StepRunSummary, ...]
    approvals: tuple[RunApprovalSummary, ...]
    artifacts: tuple[RunArtifactSummary, ...]
    metrics: tuple[RunMetricSummary, ...]
    ledger_entries: tuple[RunLedgerSummary, ...]
    content_outcome: ContentOutcomeSummary | None

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


async def load_connector_summaries(
    session: AsyncSession,
    *,
    connector_limit: int = 50,
    now: datetime | None = None,
) -> ConnectorSummaryPage:
    """Load only the latest observation for each automation connector."""
    if connector_limit < 1:
        raise ValueError("connector query limit must be positive")
    observed_at = _as_utc_naive(now or datetime.now(UTC))
    ranked_observations = (
        select(
            ConnectorObservation.id.label("observation_id"),
            func.row_number()
            .over(
                partition_by=(
                    ConnectorObservation.automation_id,
                    ConnectorObservation.connector_key,
                ),
                order_by=(
                    ConnectorObservation.observed_at.desc(),
                    ConnectorObservation.id.desc(),
                ),
            )
            .label("observation_rank"),
        )
        .subquery()
    )
    rows = (
        await session.execute(
            select(ConnectorObservation, Automation.slug)
            .join(
                ranked_observations,
                ranked_observations.c.observation_id == ConnectorObservation.id,
            )
            .join(Automation, Automation.id == ConnectorObservation.automation_id)
            .where(ranked_observations.c.observation_rank == 1)
            .order_by(
                case(
                    (ConnectorObservation.status == ConnectorStatus.UNAVAILABLE.value, 0),
                    (ConnectorObservation.status == ConnectorStatus.DEGRADED.value, 1),
                    (ConnectorObservation.status == ConnectorStatus.HEALTHY.value, 2),
                    else_=3,
                ),
                ConnectorObservation.observed_at,
                Automation.slug,
                ConnectorObservation.connector_key,
            )
            .limit(connector_limit + 1)
        )
    ).all()
    truncated = len(rows) > connector_limit
    items = tuple(
        _connector_summary(
            observation=observation,
            automation_slug=automation_slug,
            now=observed_at,
        )
        for observation, automation_slug in rows[:connector_limit]
    )
    return ConnectorSummaryPage(items=items, truncated=truncated)


async def load_dashboard_snapshot(
    session: AsyncSession,
    *,
    recent_run_limit: int = 10,
    pending_limit: int = 20,
    alert_limit: int = 20,
    experiment_limit: int = 50,
    schedule_limit: int = 50,
    connector_limit: int = 50,
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
        or connector_limit < 1
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
    connector_page = await load_connector_summaries(
        session,
        connector_limit=connector_limit,
        now=observed_at.replace(tzinfo=UTC),
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
    latest_occurrence_reason = (
        select(ScheduleOccurrence.reason_code)
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
                Automation.enabled,
                Automation.kill_switch_active,
                latest_occurrence_status.label("latest_occurrence_status"),
                latest_occurrence_reason.label("latest_occurrence_reason"),
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
        _schedule_summary(
            schedule=schedule,
            automation_slug=automation_slug,
            automation_enabled=automation_enabled,
            kill_switch_active=kill_switch_active,
            occurrence_status=occurrence_status,
            occurrence_reason=occurrence_reason,
            scheduled_for=scheduled_for,
        )
        for (
            schedule,
            automation_slug,
            automation_enabled,
            kill_switch_active,
            occurrence_status,
            occurrence_reason,
            scheduled_for,
        ) in schedule_rows
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
        connectors=connector_page.items,
        connectors_truncated=connector_page.truncated,
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
        has_local_export=(
            automation_slug == "content-engine"
            and RunStatus(run.status) is RunStatus.SUCCEEDED
            and isinstance(run.output_payload, dict)
            and run.output_payload.get("published") is False
            and isinstance(run.output_payload.get("final_review_approval_id"), int)
            and not isinstance(
                run.output_payload.get("final_review_approval_id"),
                bool,
            )
        ),
        failure=_redacted_failure(run.error),
        steps=steps,
        approvals=approvals,
        artifacts=artifacts,
        metrics=metrics,
        ledger_entries=ledger_entries,
        content_outcome=_content_outcome_summary(
            automation_slug=automation_slug,
            output_payload=run.output_payload,
            metrics=metrics,
            ledger_entries=ledger_entries,
        ),
    )


def _content_outcome_summary(
    *,
    automation_slug: str,
    output_payload: dict[str, object] | None,
    metrics: tuple[RunMetricSummary, ...],
    ledger_entries: tuple[RunLedgerSummary, ...],
) -> ContentOutcomeSummary | None:
    if automation_slug != "content-engine":
        return None

    indicator_order = {
        name: position for position, name in enumerate(_CONTENT_OUTCOME_METRICS)
    }
    indicators = tuple(
        sorted(
            (
                ContentOutcomeIndicator(
                    step_run_id=metric.step_run_id,
                    name=metric.name,
                    label=metric_contract[0],
                    value=metric.value,
                    unit=metric.unit,
                    confidence=metric.confidence,
                    observed_at=metric.observed_at,
                )
                for metric in metrics
                if (metric_contract := _CONTENT_OUTCOME_METRICS.get(metric.name))
                is not None
                and metric.kind == metric_contract[1]
                and metric.unit == metric_contract[2]
            ),
            key=lambda item: (
                indicator_order[item.name],
                item.observed_at,
                item.step_run_id or 0,
            ),
        )
    )
    energy_observations = tuple(
        metric
        for metric in metrics
        if metric.name in _CONTENT_ENERGY_METRICS
        and metric.kind == MetricKind.GAUGE.value
        and metric.unit == "kWh"
    )
    evidence_times = tuple(item.observed_at for item in indicators) + tuple(
        metric.observed_at for metric in energy_observations
    ) + tuple(entry.observed_at for entry in ledger_entries)
    published_value = (
        output_payload.get("published") if isinstance(output_payload, dict) else None
    )

    return ContentOutcomeSummary(
        published=published_value if type(published_value) is bool else None,
        observed_through=max(evidence_times) if evidence_times else None,
        indicators=indicators,
        estimated_energy_kwh=(
            sum(
                (metric.value for metric in energy_observations),
                start=Decimal(0),
            )
            if energy_observations
            else None
        ),
        ledger_totals=_run_ledger_totals(ledger_entries),
    )


def _run_ledger_totals(
    ledger_entries: tuple[RunLedgerSummary, ...],
) -> tuple[LedgerCurrencySummary, ...]:
    totals: dict[str, dict[LedgerEntryType, Decimal]] = {}
    for entry in ledger_entries:
        currency_totals = totals.setdefault(
            entry.currency,
            {entry_type: Decimal(0) for entry_type in LedgerEntryType},
        )
        currency_totals[LedgerEntryType(entry.entry_type)] += entry.amount
    return tuple(
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


def _schedule_summary(
    *,
    schedule: Schedule,
    automation_slug: str,
    automation_enabled: bool,
    kill_switch_active: bool,
    occurrence_status: str | None,
    occurrence_reason: str | None,
    scheduled_for: datetime | None,
) -> ScheduleSummary:
    executor_registered = automation_has_executor(automation_slug)
    enable_block_reason: str | None = None
    if not automation_enabled:
        enable_block_reason = "módulo desabilitado"
    elif kill_switch_active:
        enable_block_reason = "kill switch ativo"
    elif not executor_registered:
        enable_block_reason = "executor local não registrado"

    effective_status = schedule.status
    status_note: str | None = None
    if schedule.status == ScheduleStatus.ENABLED.value and enable_block_reason is not None:
        effective_status = "blocked"
        status_note = enable_block_reason

    return ScheduleSummary(
        id=schedule.id,
        automation_slug=automation_slug,
        name=schedule.name,
        status=schedule.status,
        effective_status=effective_status,
        status_note=status_note,
        enable_block_reason=enable_block_reason,
        cron_expression=schedule.cron_expression,
        timezone=schedule.timezone,
        next_run_at=schedule.next_run_at,
        latest_occurrence_status=occurrence_status,
        latest_scheduled_for=scheduled_for,
        latest_occurrence_note=_SCHEDULE_SKIP_NOTES.get(occurrence_reason or ""),
    )


def _connector_summary(
    *,
    observation: ConnectorObservation,
    automation_slug: str,
    now: datetime,
) -> ConnectorSummary:
    observation_is_future = observation.observed_at > now
    observation_age_seconds = max(
        0,
        int((now - observation.observed_at).total_seconds()),
    )
    effective_status = observation.status
    if observation.status != ConnectorStatus.DISABLED.value and (
        observation_is_future
        or observation_age_seconds > observation.status_slo_seconds
    ):
        effective_status = "stale"

    data_age_seconds: int | None = None
    if observation.status == ConnectorStatus.DISABLED.value:
        freshness_status = "not_applicable"
        data_age_label = "não aplicável"
    elif observation.last_success_at is None:
        freshness_status = "unknown"
        data_age_label = "sem sucesso observado"
    else:
        data_age_seconds = max(
            0,
            int((now - observation.last_success_at).total_seconds()),
        )
        freshness_status = (
            "fresh"
            if observation.last_success_at <= now
            and data_age_seconds <= observation.freshness_slo_seconds
            else "stale"
        )
        data_age_label = _format_pending_age(data_age_seconds)

    return ConnectorSummary(
        automation_slug=automation_slug,
        connector_key=observation.connector_key,
        reported_status=observation.status,
        effective_status=effective_status,
        observation_age_seconds=observation_age_seconds,
        observation_age_label=_format_pending_age(observation_age_seconds),
        status_slo_seconds=observation.status_slo_seconds,
        status_slo_label=_format_duration_threshold(observation.status_slo_seconds),
        last_success_at=observation.last_success_at,
        data_age_seconds=data_age_seconds,
        data_age_label=data_age_label,
        freshness_status=freshness_status,
        freshness_slo_seconds=observation.freshness_slo_seconds,
        freshness_slo_label=_format_duration_threshold(
            observation.freshness_slo_seconds
        ),
        quality_status=observation.quality_status,
        quality_score=observation.quality_score,
        observed_at=observation.observed_at,
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


def _format_duration_threshold(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3_600:
        minutes, remainder = divmod(seconds, 60)
        return f"{minutes} min" if remainder == 0 else f"{minutes} min {remainder} s"
    if seconds < 86_400:
        hours, remainder = divmod(seconds, 3_600)
        minutes = remainder // 60
        return f"{hours} h" if minutes == 0 else f"{hours} h {minutes} min"
    days, remainder = divmod(seconds, 86_400)
    hours = remainder // 3_600
    return f"{days} d" if hours == 0 else f"{days} d {hours} h"


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
