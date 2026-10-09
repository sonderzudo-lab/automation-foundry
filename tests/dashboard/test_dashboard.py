"""Read-only dashboard projections and HTTP smoke tests."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.briefs import executor as brief_executor
from src.core.config import Settings
from src.core.database import Base, get_session
from src.dashboard.main import create_app
from src.dashboard.service import (
    load_connector_summaries,
    load_dashboard_snapshot,
    load_run_detail,
)
from src.operations.health import HealthCheck, HealthReport, HealthStatus
from src.operations.purge import plan_retention_purge
from src.operations.retention import (
    RetentionAutomationSummary,
    RetentionClass,
    RetentionClassSummary,
    RetentionInventory,
    RetentionInventoryError,
    RetentionItem,
)
from src.pipeline import (
    a1_executor,
    caption_alignment,
    local_export,
    thumbnail_review,
)
from src.pipeline.a1_executor import execute_content_script_run
from src.pipeline.final_review import FINAL_REVIEW_APPROVAL_ACTION, FinalVideoReview
from src.pipeline.narration_review import NARRATION_REVIEW_APPROVAL_ACTION
from src.pipeline.script_gen import ScriptResult
from src.pipeline.thumbnail_review import THUMBNAIL_REVIEW_APPROVAL_ACTION
from src.platform.approval_service import decide_approval
from src.platform.artifact_service import register_local_artifact
from src.platform.connector_service import record_connector_observation
from src.platform.dispatch_service import (
    execute_claimed_dispatch,
    publish_registered_dispatch,
)
from src.platform.executor_registry import list_automation_executors
from src.platform.experiment_service import (
    get_or_create_experiment,
    transition_experiment,
)
from src.platform.models import (
    AlertSeverity,
    AlertStatus,
    Approval,
    ApprovalEvent,
    ApprovalStatus,
    Artifact,
    ArtifactSensitivity,
    Automation,
    ConnectorStatus,
    ControlEvent,
    ControlEventType,
    DataQualityStatus,
    Experiment,
    ExperimentStatus,
    LedgerEntry,
    LedgerEntryType,
    MetricKind,
    MetricPoint,
    PlatformAlert,
    QueueClass,
    Run,
    RunDispatch,
    RunStatus,
    Schedule,
    ScheduleEvent,
    ScheduleEventType,
    ScheduleOccurrence,
    ScheduleOccurrenceStatus,
    ScheduleStatus,
    StepRun,
)
from src.platform.run_service import (
    get_or_create_automation,
    get_or_create_run,
    transition_run,
)
from tests.pipeline.test_assembly import _assembly_adapter
from tests.support.local_media_rehearsal import (
    expected_visual_count,
    rehearsal_input_payload,
    rehearsal_script,
    rehearsal_settings,
    write_synthetic_narration_wav,
    write_synthetic_visual_import,
)


def _assert_review_link(home_html: str, approval_id: int, action: str) -> None:
    """The pending-approval alert names the action and links to its review page."""
    alert = re.search(
        r'<div class="alert alert-info">(?:(?!<div class="alert ).)*?'
        rf"<code>{re.escape(action)}</code>.*?"
        rf'href="/approvals/{approval_id}/review"',
        home_html,
        re.DOTALL,
    )
    assert alert is not None, f"no review link for {action} #{approval_id}"


@pytest.fixture
async def session_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _seed_dashboard(factory: async_sessionmaker[AsyncSession]) -> int:
    observed_at = datetime(2026, 7, 23, 12, 0)
    async with factory() as session:
        automation = Automation(
            slug="content-engine",
            name="Content Engine",
            owner="private-owner",
        )
        session.add(automation)
        await session.flush()
        run = Run(
            automation_id=automation.id,
            idempotency_key="private-run-key",
            trigger="manual",
            input_payload={"private": "input-secret"},
            output_payload={"private": "output-secret"},
            error={
                "code": "RUN_FAILED",
                "exception_type": "RuntimeError",
                "message": "error-secret",
            },
            status=RunStatus.FAILED.value,
            queued_at=observed_at,
            started_at=observed_at + timedelta(seconds=1),
            finished_at=observed_at + timedelta(seconds=13, milliseconds=345),
        )
        session.add(run)
        await session.flush()
        approval = Approval(
            run_id=run.id,
            idempotency_key="private-approval-key",
            action="publish",
            summary="private approval summary",
            payload_digest="a" * 64,
            status=ApprovalStatus.PENDING.value,
            requested_at=observed_at,
        )
        session.add(approval)
        await session.flush()
        step = StepRun(
            run_id=run.id,
            approval_id=approval.id,
            name="render-video",
            queue=QueueClass.CPU.value,
            ordinal=1,
            attempt=2,
            idempotency_key="private-step-key",
            input_payload={"private": "step-input-secret"},
            output_payload={"private": "step-output-secret"},
            status=RunStatus.FAILED.value,
            error={
                "code": "UNEXPECTED_TASK_ERROR",
                "exception_type": "PrivateAdapterError",
                "message": "step-error-secret",
            },
            queued_at=observed_at,
            started_at=observed_at + timedelta(seconds=2),
            finished_at=observed_at + timedelta(seconds=4, milliseconds=500),
        )
        session.add(step)
        await session.flush()
        session.add(
            Artifact(
                run_id=run.id,
                step_run_id=step.id,
                idempotency_key="private-artifact-key",
                artifact_type="video",
                relative_path="private/artifact/path.mp4",
                media_type="video/mp4",
                sha256="b" * 64,
                size_bytes=2048,
                origin="private-artifact-origin",
                sensitivity=ArtifactSensitivity.CONFIDENTIAL.value,
                created_at=observed_at,
                verified_at=observed_at,
            )
        )
        metric = MetricPoint(
            automation_id=automation.id,
            run_id=run.id,
            step_run_id=step.id,
            idempotency_key="private-metric-key",
            name="render_duration",
            kind=MetricKind.DURATION.value,
            value=Decimal("2.5000000000"),
            unit="seconds",
            source="private-metric-source",
            confidence=Decimal("0.7500"),
            dimensions={"private": "metric-dimension-secret"},
            observed_at=observed_at,
        )
        session.add(metric)
        await session.flush()
        session.add(
            PlatformAlert(
                automation_id=automation.id,
                deduplication_key="private-alert-key",
                title="Worker unavailable",
                summary="private alert summary",
                severity=AlertSeverity.ERROR.value,
                status=AlertStatus.OPEN.value,
                source="private-alert-source",
                occurrence_count=2,
                first_seen_at=observed_at,
                last_seen_at=observed_at,
            )
        )
        for index, (entry_type, amount) in enumerate(
            (
                (LedgerEntryType.COST, "0.1"),
                (LedgerEntryType.COST, "0.2"),
                (LedgerEntryType.REVENUE, "1.0"),
                (LedgerEntryType.ATTRIBUTED_VALUE, "2.0"),
            ),
            start=1,
        ):
            session.add(
                LedgerEntry(
                    automation_id=automation.id,
                    run_id=run.id,
                    step_run_id=step.id,
                    metric_point_id=metric.id,
                    idempotency_key=f"private-ledger-key-{index}",
                    entry_type=entry_type.value,
                    category="private-ledger-category",
                    amount=Decimal(amount),
                    currency="BRL",
                    source="private-ledger-source",
                    confidence=Decimal("0.5000"),
                    observed_at=observed_at,
                )
            )
        await session.commit()
        return run.id


async def _seed_cancellable_run(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as session:
        automation = Automation(
            slug="safe-local-control",
            name="Safe Local Control",
            owner="private-owner",
        )
        session.add(automation)
        await session.flush()
        run = Run(
            automation_id=automation.id,
            idempotency_key="private-cancellable-run-key",
            trigger="manual",
            input_payload={"private": "cancellable-input-secret"},
            status=RunStatus.QUEUED.value,
        )
        session.add(run)
        await session.commit()
        return run.id


async def _seed_pending_approval(
    factory: async_sessionmaker[AsyncSession],
    *,
    with_review_projection: bool = False,
) -> tuple[int, int]:
    async with factory() as session:
        automation = Automation(
            slug="approval-control",
            name="Approval Control",
            owner="private-owner",
        )
        session.add(automation)
        await session.flush()
        run = Run(
            automation_id=automation.id,
            idempotency_key="private-approval-run-key",
            trigger="manual",
            input_payload={"private": "protected-payload-secret"},
            status=RunStatus.AWAITING_APPROVAL.value,
        )
        session.add(run)
        await session.flush()
        approval = Approval(
            run_id=run.id,
            idempotency_key="private-pending-approval-key",
            action="publish",
            summary="private protected approval summary",
            payload_digest="c" * 64,
            review_payload=(
                {"artifact": "Video #42", "visibility": "Private"}
                if with_review_projection
                else None
            ),
            status=ApprovalStatus.PENDING.value,
        )
        session.add(approval)
        await session.commit()
        return run.id, approval.id


async def _seed_schedules(factory: async_sessionmaker[AsyncSession]) -> None:
    base_time = datetime(2026, 8, 10, 12, 0)
    async with factory() as session:
        automation = Automation(
            slug="scheduled-module",
            name="Scheduled Module",
            owner="private-schedule-owner",
        )
        session.add(automation)
        await session.flush()
        sooner = Schedule(
            automation_id=automation.id,
            name="Sooner enabled schedule",
            cron_expression="*/5 * * * *",
            timezone="America/Sao_Paulo",
            input_payload={"token": "private-sooner-payload"},
            status=ScheduleStatus.ENABLED.value,
            next_run_at=base_time + timedelta(minutes=5),
        )
        later = Schedule(
            automation_id=automation.id,
            name="Later enabled schedule",
            cron_expression="0 * * * *",
            timezone="UTC",
            input_payload={"token": "private-later-payload"},
            status=ScheduleStatus.ENABLED.value,
            next_run_at=base_time + timedelta(hours=1),
        )
        disabled = Schedule(
            automation_id=automation.id,
            name="Disabled schedule",
            cron_expression="0 9 * * 1-5",
            timezone="UTC",
            input_payload={"token": "private-disabled-payload"},
            status=ScheduleStatus.DISABLED.value,
            next_run_at=None,
        )
        session.add_all((later, disabled, sooner))
        await session.flush()
        session.add_all(
            (
                ScheduleOccurrence(
                    schedule_id=sooner.id,
                    scheduled_for=base_time - timedelta(minutes=10),
                    status=ScheduleOccurrenceStatus.SKIPPED.value,
                    reason_code="private-old-skip-reason",
                ),
                ScheduleOccurrence(
                    schedule_id=sooner.id,
                    scheduled_for=base_time - timedelta(minutes=5),
                    status=ScheduleOccurrenceStatus.SKIPPED.value,
                    reason_code="EXECUTOR_NOT_REGISTERED",
                ),
            )
        )
        await session.commit()


async def _seed_attributed_ledger(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    observed_at = datetime(2026, 8, 10, 12, 0)
    async with factory() as session:
        alpha = Automation(
            slug="alpha-automation",
            name="Alpha Automation",
            owner="private-alpha-owner",
        )
        beta = Automation(
            slug="beta-automation",
            name="Beta Automation",
            owner="private-beta-owner",
        )
        session.add_all((alpha, beta))
        await session.flush()
        entries = (
            (alpha, LedgerEntryType.COST, "0.1000000001", "BRL"),
            (alpha, LedgerEntryType.COST, "0.2000000002", "BRL"),
            (alpha, LedgerEntryType.REVENUE, "1.0000000004", "BRL"),
            (alpha, LedgerEntryType.ATTRIBUTED_VALUE, "2.3456789012", "USD"),
            (beta, LedgerEntryType.COST, "0.4000000004", "BRL"),
            (beta, LedgerEntryType.REVENUE, "0.5000000005", "BRL"),
            (beta, LedgerEntryType.REVENUE, "3.0000000003", "USD"),
        )
        for index, (automation, entry_type, amount, currency) in enumerate(
            entries,
            start=1,
        ):
            session.add(
                LedgerEntry(
                    automation_id=automation.id,
                    idempotency_key=f"private-attributed-ledger-{index}",
                    entry_type=entry_type.value,
                    category="private-attributed-category",
                    amount=Decimal(amount),
                    currency=currency,
                    source="private-attributed-source",
                    observed_at=observed_at,
                )
            )
        await session.commit()


async def _seed_execution_metrics(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    base_time = datetime(2026, 8, 10, 12, 0)
    async with factory() as session:
        alpha = Automation(
            slug="alpha-execution",
            name="Alpha Execution",
            owner="private-alpha-execution-owner",
        )
        beta = Automation(
            slug="beta-execution",
            name="Beta Execution",
            owner="private-beta-execution-owner",
        )
        gamma = Automation(
            slug="gamma-no-results",
            name="Gamma No Results",
            owner="private-gamma-execution-owner",
        )
        session.add_all((alpha, beta, gamma))
        await session.flush()
        run_specs = (
            (alpha, "alpha-invalid-duration", RunStatus.FAILED, 5, None),
            (alpha, "alpha-recent-success", RunStatus.SUCCEEDED, 4, 10),
            (alpha, "alpha-recent-failure", RunStatus.FAILED, 3, 20),
            (alpha, "alpha-older-success", RunStatus.SUCCEEDED, 2, 100),
            (beta, "beta-success-five", RunStatus.SUCCEEDED, 4, 5),
            (beta, "beta-success-seven", RunStatus.SUCCEEDED, 3, 7),
        )
        for automation, key, status, finished_hour, duration_seconds in run_specs:
            finished_at = base_time + timedelta(hours=finished_hour)
            session.add(
                Run(
                    automation_id=automation.id,
                    idempotency_key=key,
                    trigger="private-execution-trigger",
                    input_payload={"private": "private-execution-input"},
                    status=status.value,
                    error=(
                        {"message": "private-execution-error"}
                        if status is RunStatus.FAILED
                        else None
                    ),
                    queued_at=finished_at - timedelta(minutes=1),
                    started_at=(
                        finished_at - timedelta(seconds=duration_seconds)
                        if duration_seconds is not None
                        else None
                    ),
                    finished_at=finished_at,
                )
            )
        session.add_all(
            (
                Run(
                    automation_id=alpha.id,
                    idempotency_key="alpha-running",
                    trigger="private-running-trigger",
                    input_payload={"private": "private-running-input"},
                    status=RunStatus.RUNNING.value,
                    queued_at=base_time + timedelta(hours=6),
                    started_at=base_time + timedelta(hours=6),
                ),
                Run(
                    automation_id=alpha.id,
                    idempotency_key="alpha-cancelled",
                    trigger="private-cancelled-trigger",
                    input_payload={"private": "private-cancelled-input"},
                    status=RunStatus.CANCELLED.value,
                    queued_at=base_time + timedelta(hours=7),
                    started_at=base_time + timedelta(hours=7),
                    finished_at=base_time + timedelta(hours=7, seconds=30),
                ),
            )
        )
        await session.commit()


async def _seed_content_outcome(
    factory: async_sessionmaker[AsyncSession],
) -> int:
    observed_at = datetime(2026, 8, 12, 9, 0)
    async with factory() as session:
        automation = Automation(
            slug="content-engine",
            name="Content Engine",
            owner="private-content-owner",
        )
        unrelated_automation = Automation(
            slug="unrelated-module",
            name="Unrelated Module",
            owner="private-unrelated-owner",
        )
        session.add_all((automation, unrelated_automation))
        await session.flush()
        run = Run(
            automation_id=automation.id,
            idempotency_key="private-content-outcome-run",
            trigger="manual",
            input_payload={"private": "content-outcome-input"},
            output_payload={"published": False, "private": "content-outcome-output"},
            status=RunStatus.SUCCEEDED.value,
            queued_at=observed_at,
            started_at=observed_at,
            finished_at=observed_at + timedelta(seconds=12),
        )
        unrelated_run = Run(
            automation_id=unrelated_automation.id,
            idempotency_key="private-unrelated-run",
            trigger="manual",
            status=RunStatus.SUCCEEDED.value,
            queued_at=observed_at,
            started_at=observed_at,
            finished_at=observed_at + timedelta(seconds=1),
        )
        session.add_all((run, unrelated_run))
        await session.flush()
        step = StepRun(
            run_id=run.id,
            name="content-originality-a6",
            queue=QueueClass.CPU.value,
            ordinal=6,
            attempt=1,
            idempotency_key="private-content-outcome-step",
            status=RunStatus.SUCCEEDED.value,
            queued_at=observed_at,
            started_at=observed_at,
            finished_at=observed_at + timedelta(seconds=1),
        )
        session.add(step)
        await session.flush()
        metric_specs = (
            (
                "content_caption_transcript_match_ratio",
                MetricKind.GAUGE,
                "0.9876543210",
                "ratio",
                "0.9000",
            ),
            (
                "content_originality_max_similarity",
                MetricKind.RATIO,
                "0.1250000000",
                "ratio",
                None,
            ),
            (
                "content_tts_energy_estimate_kwh",
                MetricKind.GAUGE,
                "0.0100000000",
                "kWh",
                "0.5000",
            ),
            (
                "content_assembly_energy_estimate_kwh",
                MetricKind.GAUGE,
                "0.0200000000",
                "kWh",
                "0.4000",
            ),
            (
                "private_unclassified_metric",
                MetricKind.GAUGE,
                "999.0000000000",
                "secret-unit",
                None,
            ),
        )
        for index, (name, kind, value, unit, confidence) in enumerate(
            metric_specs,
            start=1,
        ):
            session.add(
                MetricPoint(
                    automation_id=automation.id,
                    run_id=run.id,
                    step_run_id=step.id,
                    idempotency_key=f"private-content-outcome-metric-{index}",
                    name=name,
                    kind=kind.value,
                    value=Decimal(value),
                    unit=unit,
                    source="private-content-outcome-source",
                    confidence=(Decimal(confidence) if confidence else None),
                    dimensions={"private": "content-outcome-dimension"},
                    observed_at=observed_at + timedelta(seconds=index),
                )
            )
        ledger_specs = (
            (LedgerEntryType.COST, "0.1000000001", "BRL"),
            (LedgerEntryType.COST, "0.2000000002", "BRL"),
            (LedgerEntryType.REVENUE, "1.0000000004", "BRL"),
            (LedgerEntryType.ATTRIBUTED_VALUE, "2.3456789012", "BRL"),
            (LedgerEntryType.COST, "0.4000000004", "USD"),
        )
        for index, (entry_type, amount, currency) in enumerate(ledger_specs, start=1):
            session.add(
                LedgerEntry(
                    automation_id=automation.id,
                    run_id=run.id,
                    step_run_id=step.id,
                    idempotency_key=f"private-content-outcome-ledger-{index}",
                    entry_type=entry_type.value,
                    category="private-content-outcome-category",
                    amount=Decimal(amount),
                    currency=currency,
                    source="private-content-outcome-ledger-source",
                    observed_at=observed_at + timedelta(seconds=10 + index),
                )
            )
        session.add(
            LedgerEntry(
                automation_id=unrelated_automation.id,
                run_id=unrelated_run.id,
                idempotency_key="private-unrelated-ledger",
                entry_type=LedgerEntryType.REVENUE.value,
                category="private-unrelated-category",
                amount=Decimal("999.9999999999"),
                currency="BRL",
                source="private-unrelated-source",
                observed_at=observed_at + timedelta(hours=1),
            )
        )
        await session.commit()
        return run.id


async def test_snapshot_is_bounded_redacted_and_decimal_exact(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_dashboard(session_factory)
    async with session_factory() as session:
        snapshot = await load_dashboard_snapshot(
            session,
            now=datetime(2026, 7, 25, 15, 30, tzinfo=UTC),
        )

    assert snapshot.automations[0].slug == "content-engine"
    assert snapshot.recent_runs[0].status == RunStatus.FAILED.value
    assert snapshot.pending_approvals[0].action == "publish"
    assert snapshot.pending_approvals[0].pending_for_seconds == 185_400
    assert snapshot.pending_approvals[0].pending_for_label == "2 d 3 h"
    assert snapshot.active_alerts[0].title == "Worker unavailable"
    assert snapshot.ledger_totals[0].cost == Decimal("0.3000000000")
    assert snapshot.ledger_totals[0].revenue == Decimal("1.0000000000")
    assert snapshot.ledger_totals[0].net_revenue == Decimal("0.7000000000")
    assert snapshot.ledger_totals[0].attributed_value == Decimal("2.0000000000")
    assert snapshot.automation_ledger_totals[0].automation_slug == "content-engine"
    assert snapshot.automation_ledger_totals[0].cost == Decimal("0.3000000000")
    rendered = repr(snapshot)
    for private_value in (
        "private-owner",
        "private-run-key",
        "input-secret",
        "output-secret",
        "error-secret",
        "private approval summary",
        "private alert summary",
        "private-alert-source",
        "private-ledger-category",
        "private-ledger-source",
        "private-step-key",
        "step-input-secret",
        "step-output-secret",
        "step-error-secret",
        "private/artifact/path.mp4",
        "private-artifact-origin",
        "private-metric-source",
        "metric-dimension-secret",
    ):
        assert private_value not in rendered


async def test_connector_snapshot_and_htmx_fragment_are_latest_redacted_and_bounded(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        automation = (
            await get_or_create_automation(
                session,
                slug="content-engine-connectors",
                name="Content Engine connectors",
                owner="private-connector-owner",
            )
        ).automation
        common = {
            "automation": automation,
            "status_slo_seconds": 600,
            "freshness_slo_seconds": 900,
        }
        await record_connector_observation(
            session,
            connector_key="youtube-analytics",
            idempotency_key="private-youtube-observation-old",
            status=ConnectorStatus.HEALTHY,
            last_success_at=datetime(2026, 8, 11, 11, 55, tzinfo=UTC),
            quality_status=DataQualityStatus.PASS,
            quality_score="0.9000",
            observed_at=datetime(2026, 8, 11, 12, 0, tzinfo=UTC),
            **common,
        )
        await record_connector_observation(
            session,
            connector_key="youtube-analytics",
            idempotency_key="private-youtube-observation-latest",
            status=ConnectorStatus.DEGRADED,
            last_success_at=datetime(2026, 8, 11, 12, 50, tzinfo=UTC),
            quality_status=DataQualityStatus.WARNING,
            quality_score="0.8200",
            observed_at=datetime(2026, 8, 11, 12, 55, tzinfo=UTC),
            **common,
        )
        await record_connector_observation(
            session,
            connector_key="stale-feed",
            idempotency_key="private-stale-observation",
            status=ConnectorStatus.UNAVAILABLE,
            status_slo_seconds=300,
            last_success_at=datetime(2026, 8, 11, 11, 0, tzinfo=UTC),
            freshness_slo_seconds=900,
            quality_status=DataQualityStatus.PASS,
            quality_score="0.9900",
            observed_at=datetime(2026, 8, 11, 12, 0, tzinfo=UTC),
            automation=automation,
        )
        await record_connector_observation(
            session,
            connector_key="local-import",
            idempotency_key="private-disabled-observation",
            status=ConnectorStatus.DISABLED,
            last_success_at=None,
            quality_status=DataQualityStatus.UNKNOWN,
            quality_score=None,
            observed_at=datetime(2026, 8, 11, 12, 59, tzinfo=UTC),
            **common,
        )
        await session.commit()

    async with session_factory() as session:
        connector_page = await load_connector_summaries(
            session,
            connector_limit=3,
            now=datetime(2026, 8, 11, 13, 0, tzinfo=UTC),
        )

    connectors = connector_page.items
    assert connector_page.truncated is False
    assert [connector.connector_key for connector in connectors] == [
        "stale-feed",
        "youtube-analytics",
        "local-import",
    ]
    stale, youtube, disabled = connectors
    assert disabled.effective_status == ConnectorStatus.DISABLED.value
    assert disabled.freshness_status == "not_applicable"
    assert stale.reported_status == ConnectorStatus.UNAVAILABLE.value
    assert stale.effective_status == "stale"
    assert stale.freshness_status == "stale"
    assert youtube.reported_status == ConnectorStatus.DEGRADED.value
    assert youtube.effective_status == ConnectorStatus.DEGRADED.value
    assert youtube.freshness_status == "fresh"
    assert youtube.data_age_seconds == 600
    assert youtube.quality_status == DataQualityStatus.WARNING.value
    assert youtube.quality_score == Decimal("0.8200")
    assert "private-youtube-observation" not in repr(connectors)
    assert "private-connector-owner" not in repr(connectors)

    application = create_app()

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        home = await client.get("/")
        fragment = await client.get("/data-observability/fragment")

    assert home.status_code == 200
    assert home.headers["cache-control"] == "no-store"
    assert '<script src="/static/htmx.min.js" defer></script>' in home.text
    assert 'id="data-observability"' in home.text
    assert fragment.status_code == 200
    assert fragment.headers["cache-control"] == "no-store"
    assert 'hx-get="/data-observability/fragment"' in fragment.text
    assert 'hx-trigger="every 30s"' in fragment.text
    assert "Connectors, freshness e qualidade" in fragment.text
    assert "youtube-analytics" in fragment.text
    assert "0.8200" in fragment.text
    assert "2026-08-11T12:55:00Z" in fragment.text
    # A freshness vencida precisa de ícone, texto e cor de perigo juntos, nunca só cor.
    assert re.search(
        r'class="badge badge-danger"><svg[^>]*><use[^>]*/></svg>stale',
        fragment.text,
    )
    assert "<html" not in fragment.text
    for private_value in (
        "private-youtube-observation",
        "private-stale-observation",
        "private-disabled-observation",
        "private-connector-owner",
    ):
        assert private_value not in home.text
        assert private_value not in fragment.text

    with pytest.raises(ValueError, match="positive"):
        async with session_factory() as session:
            await load_connector_summaries(session, connector_limit=0)

    async with session_factory() as session:
        limited_page = await load_connector_summaries(
            session,
            connector_limit=2,
            now=datetime(2026, 8, 11, 13, 0, tzinfo=UTC),
        )
    assert limited_page.truncated is True
    assert [item.connector_key for item in limited_page.items] == [
        "stale-feed",
        "youtube-analytics",
    ]


async def test_pending_approval_age_clamps_future_timestamp_to_zero(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_dashboard(session_factory)
    async with session_factory() as session:
        snapshot = await load_dashboard_snapshot(
            session,
            now=datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
        )

    approval = snapshot.pending_approvals[0]
    assert approval.pending_for_seconds == 0
    assert approval.pending_for_label == "menos de 1 min"


async def test_dashboard_schedules_are_ordered_current_and_redacted(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_schedules(session_factory)
    async with session_factory() as session:
        snapshot = await load_dashboard_snapshot(session)

    assert [schedule.name for schedule in snapshot.schedules] == [
        "Sooner enabled schedule",
        "Later enabled schedule",
        "Disabled schedule",
    ]
    assert snapshot.schedules[0].latest_occurrence_status == "skipped"
    assert snapshot.schedules[0].effective_status == "blocked"
    assert snapshot.schedules[0].status_note == "executor local não registrado"
    assert snapshot.schedules[0].latest_occurrence_note == (
        "executor local não registrado"
    )
    assert snapshot.schedules[0].latest_scheduled_for == datetime(
        2026,
        8,
        10,
        11,
        55,
    )
    assert snapshot.schedules[1].latest_scheduled_for is None
    assert snapshot.schedules[2].next_run_at is None
    for private_value in (
        "private-schedule-owner",
        "private-sooner-payload",
        "private-later-payload",
        "private-disabled-payload",
        "private-old-skip-reason",
    ):
        assert private_value not in repr(snapshot)

    application = create_app()

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.get("/")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "Schedules" in response.text
    assert "*/5 * * * *" in response.text
    assert "America/Sao_Paulo" in response.text
    assert "2026-08-10T12:05:00Z" in response.text
    assert "2026-08-10T11:55:00Z" in response.text
    assert response.text.index("Sooner enabled schedule") < response.text.index(
        "Later enabled schedule"
    )
    assert response.text.index("Later enabled schedule") < response.text.index("Disabled schedule")
    for private_value in (
        "private-sooner-payload",
        "private-old-skip-reason",
    ):
        assert private_value not in response.text


async def test_dashboard_toggles_schedule_idempotently_with_audit_evidence(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        automation = Automation(
            slug="platform-smoke",
            name="Dashboard Schedule Toggle",
            owner="local-operator",
        )
        session.add(automation)
        await session.flush()
        schedule = Schedule(
            automation_id=automation.id,
            name="Local dashboard schedule",
            cron_expression="*/15 * * * *",
            timezone="UTC",
            input_payload={"private": "private-schedule-payload"},
            status=ScheduleStatus.DISABLED.value,
            next_run_at=None,
        )
        session.add(schedule)
        await session.commit()
        schedule_id = schedule.id

    application = create_app(csrf_token="test-csrf-token")

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    enable_payload = {
        "csrf_token": "test-csrf-token",
        "confirmation": "schedule-enable",
        "target": "enable",
        "actor": "local-owner",
        "reason": "start local periodic intake",
    }
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        initial = await client.get("/")
        enabled = await client.post(
            f"/schedules/{schedule_id}/enabled",
            data=enable_payload,
        )
        duplicate = await client.post(
            f"/schedules/{schedule_id}/enabled",
            data={**enable_payload, "reason": "duplicate must not replace evidence"},
        )
        enabled_page = await client.get("/")
        disabled = await client.post(
            f"/schedules/{schedule_id}/enabled",
            data={
                "csrf_token": "test-csrf-token",
                "confirmation": "schedule-disable",
                "target": "disable",
                "actor": "local-owner",
                "reason": "pause local periodic intake",
            },
        )

    assert initial.status_code == 200
    assert f'action="/schedules/{schedule_id}/enabled"' in initial.text
    assert "Habilitar schedule" in initial.text
    assert "private-schedule-payload" not in initial.text
    assert enabled.status_code == 303
    assert duplicate.status_code == 303
    assert enabled_page.status_code == 200
    assert "Desabilitar schedule" in enabled_page.text
    assert disabled.status_code == 303
    async with session_factory() as session:
        fetched = await session.get(Schedule, schedule_id)
        events = list(
            (
                await session.scalars(
                    select(ScheduleEvent)
                    .where(ScheduleEvent.schedule_id == schedule_id)
                    .order_by(ScheduleEvent.id)
                )
            ).all()
        )
    assert fetched is not None
    assert fetched.status == ScheduleStatus.DISABLED.value
    assert fetched.next_run_at is None
    assert [event.event_type for event in events] == [
        ScheduleEventType.ENABLED.value,
        ScheduleEventType.DISABLED.value,
    ]
    assert events[0].actor == "local-owner"
    assert events[0].reason == "start local periodic intake"
    assert events[0].next_run_at is not None
    assert events[1].previous_next_run_at == events[0].next_run_at
    assert events[1].next_run_at is None


@pytest.mark.parametrize(
    ("automation_enabled", "kill_switch_active"),
    ((False, False), (True, True)),
)
async def test_dashboard_schedule_enable_respects_automation_safety_blocks(
    session_factory: async_sessionmaker[AsyncSession],
    automation_enabled: bool,
    kill_switch_active: bool,
) -> None:
    async with session_factory() as session:
        automation = Automation(
            slug="platform-smoke",
            name="Blocked Dashboard Schedule",
            owner="local-operator",
            enabled=automation_enabled,
            kill_switch_active=kill_switch_active,
        )
        session.add(automation)
        await session.flush()
        schedule = Schedule(
            automation_id=automation.id,
            name="Blocked local schedule",
            cron_expression="0 * * * *",
            timezone="UTC",
            input_payload={},
            status=ScheduleStatus.DISABLED.value,
            next_run_at=None,
        )
        session.add(schedule)
        await session.commit()
        schedule_id = schedule.id

    application = create_app(csrf_token="test-csrf-token")

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.post(
            f"/schedules/{schedule_id}/enabled",
            data={
                "csrf_token": "test-csrf-token",
                "confirmation": "schedule-enable",
                "target": "enable",
                "actor": "local-owner",
                "reason": "must remain blocked",
            },
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/?notice=schedule-change-blocked"
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        notice = await client.get(response.headers["location"])
    assert notice.status_code == 200
    assert "O schedule não foi habilitado" in notice.text
    async with session_factory() as session:
        fetched = await session.get(Schedule, schedule_id)
        events = list(
            (
                await session.scalars(
                    select(ScheduleEvent).where(ScheduleEvent.schedule_id == schedule_id)
                )
            ).all()
        )
    assert fetched is not None
    assert fetched.status == ScheduleStatus.DISABLED.value
    assert fetched.next_run_at is None
    assert events == []


async def test_dashboard_schedule_enable_requires_registered_executor(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        automation = Automation(
            slug="future-module-without-executor",
            name="Future Module",
            owner="local-operator",
        )
        session.add(automation)
        await session.flush()
        schedule = Schedule(
            automation_id=automation.id,
            name="Unavailable future schedule",
            cron_expression="0 * * * *",
            timezone="UTC",
            input_payload={},
            status=ScheduleStatus.DISABLED.value,
            next_run_at=None,
        )
        session.add(schedule)
        await session.commit()
        schedule_id = schedule.id

    application = create_app(csrf_token="test-csrf-token")

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        page = await client.get("/")
        response = await client.post(
            f"/schedules/{schedule_id}/enabled",
            data={
                "csrf_token": "test-csrf-token",
                "confirmation": "schedule-enable",
                "target": "enable",
                "actor": "local-owner",
                "reason": "must remain unavailable",
            },
        )

    assert page.status_code == 200
    assert "Habilitação indisponível" in page.text
    assert "executor local não registrado" in page.text
    assert response.status_code == 303
    assert response.headers["location"] == "/?notice=schedule-change-blocked"
    async with session_factory() as session:
        fetched = await session.get(Schedule, schedule_id)
        events = list(
            (
                await session.scalars(
                    select(ScheduleEvent).where(ScheduleEvent.schedule_id == schedule_id)
                )
            ).all()
        )
    assert fetched is not None
    assert fetched.status == ScheduleStatus.DISABLED.value
    assert fetched.next_run_at is None
    assert events == []


async def test_dashboard_can_disable_schedule_while_kill_switch_is_active(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    next_run_at = datetime(2026, 8, 12, 12, 0)
    async with session_factory() as session:
        automation = Automation(
            slug="platform-smoke",
            name="Platform Smoke",
            owner="local-operator",
            kill_switch_active=True,
        )
        session.add(automation)
        await session.flush()
        schedule = Schedule(
            automation_id=automation.id,
            name="Stoppable schedule",
            cron_expression="0 * * * *",
            timezone="UTC",
            input_payload={},
            status=ScheduleStatus.ENABLED.value,
            next_run_at=next_run_at,
        )
        session.add(schedule)
        await session.commit()
        schedule_id = schedule.id

    application = create_app(csrf_token="test-csrf-token")

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        page = await client.get("/")
        response = await client.post(
            f"/schedules/{schedule_id}/enabled",
            data={
                "csrf_token": "test-csrf-token",
                "confirmation": "schedule-disable",
                "target": "disable",
                "actor": "local-owner",
                "reason": "stop while operationally blocked",
            },
        )

    assert page.status_code == 200
    assert "blocked" in page.text
    assert "kill switch ativo" in page.text
    assert response.status_code == 303
    async with session_factory() as session:
        fetched = await session.get(Schedule, schedule_id)
        event = await session.scalar(
            select(ScheduleEvent).where(ScheduleEvent.schedule_id == schedule_id)
        )
    assert fetched is not None
    assert fetched.status == ScheduleStatus.DISABLED.value
    assert fetched.next_run_at is None
    assert event is not None
    assert event.event_type == ScheduleEventType.DISABLED.value
    assert event.previous_next_run_at == next_run_at


@pytest.mark.parametrize(
    ("csrf_token", "confirmation", "expected_status"),
    (
        ("wrong-token", "schedule-enable", 403),
        ("tökén-nao-ascii", "schedule-enable", 403),
        ("test-csrf-token", "not-confirmed", 400),
    ),
)
async def test_dashboard_schedule_toggle_rejects_untrusted_or_unconfirmed_requests(
    session_factory: async_sessionmaker[AsyncSession],
    csrf_token: str,
    confirmation: str,
    expected_status: int,
) -> None:
    async with session_factory() as session:
        automation = Automation(
            slug="untrusted-dashboard-schedule",
            name="Untrusted Dashboard Schedule",
            owner="local-operator",
        )
        session.add(automation)
        await session.flush()
        schedule = Schedule(
            automation_id=automation.id,
            name="Protected local schedule",
            cron_expression="0 * * * *",
            timezone="UTC",
            input_payload={},
            status=ScheduleStatus.DISABLED.value,
            next_run_at=None,
        )
        session.add(schedule)
        await session.commit()
        schedule_id = schedule.id

    application = create_app(csrf_token="test-csrf-token")

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.post(
            f"/schedules/{schedule_id}/enabled",
            data={
                "csrf_token": csrf_token,
                "confirmation": confirmation,
                "target": "enable",
                "actor": "local-owner",
                "reason": "must not persist",
            },
        )

    assert response.status_code == expected_status
    async with session_factory() as session:
        fetched = await session.get(Schedule, schedule_id)
        events = list(
            (
                await session.scalars(
                    select(ScheduleEvent).where(ScheduleEvent.schedule_id == schedule_id)
                )
            ).all()
        )
    assert fetched is not None
    assert fetched.status == ScheduleStatus.DISABLED.value
    assert events == []


async def test_dashboard_ledger_is_exactly_attributed_by_automation_and_currency(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_attributed_ledger(session_factory)
    async with session_factory() as session:
        snapshot = await load_dashboard_snapshot(session)

    assert [
        (total.automation_slug, total.currency)
        for total in snapshot.automation_ledger_totals
    ] == [
        ("alpha-automation", "BRL"),
        ("alpha-automation", "USD"),
        ("beta-automation", "BRL"),
        ("beta-automation", "USD"),
    ]
    alpha_brl, alpha_usd, beta_brl, beta_usd = snapshot.automation_ledger_totals
    assert alpha_brl.cost == Decimal("0.3000000003")
    assert alpha_brl.revenue == Decimal("1.0000000004")
    assert alpha_brl.net_revenue == Decimal("0.7000000001")
    assert alpha_brl.attributed_value == Decimal("0E-10")
    assert alpha_usd.attributed_value == Decimal("2.3456789012")
    assert beta_brl.cost == Decimal("0.4000000004")
    assert beta_brl.revenue == Decimal("0.5000000005")
    assert beta_brl.net_revenue == Decimal("0.1000000001")
    assert beta_usd.revenue == Decimal("3.0000000003")
    assert [total.currency for total in snapshot.ledger_totals] == ["BRL", "USD"]
    assert snapshot.ledger_totals[0].cost == Decimal("0.7000000007")
    assert snapshot.ledger_totals[1].attributed_value == Decimal("2.3456789012")
    for private_value in (
        "private-alpha-owner",
        "private-beta-owner",
        "private-attributed-ledger",
        "private-attributed-category",
        "private-attributed-source",
    ):
        assert private_value not in repr(snapshot)

    application = create_app()

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.get("/")

    assert response.status_code == 200
    assert "Ledger por automação" in response.text
    assert "alpha-automation" in response.text
    assert "beta-automation" in response.text
    assert "2.3456789012" in response.text
    assert "0E-10" not in response.text
    assert "private-attributed-category" not in response.text
    assert "private-attributed-source" not in response.text


async def test_execution_metrics_use_bounded_completed_samples_only(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_execution_metrics(session_factory)
    async with session_factory() as session:
        snapshot = await load_dashboard_snapshot(
            session,
            execution_sample_limit=3,
        )

    assert [summary.automation_slug for summary in snapshot.execution_summaries] == [
        "alpha-execution",
        "beta-execution",
        "gamma-no-results",
    ]
    alpha, beta, gamma = snapshot.execution_summaries
    assert alpha.sample_size == 3
    assert alpha.succeeded_count == 1
    assert alpha.success_rate_percent == Decimal("33.3")
    assert alpha.duration_sample_size == 2
    assert alpha.average_duration_seconds == Decimal("15.000")
    assert alpha.sample_limit == 3
    assert beta.sample_size == 2
    assert beta.succeeded_count == 2
    assert beta.success_rate_percent == Decimal("100.0")
    assert beta.duration_sample_size == 2
    assert beta.average_duration_seconds == Decimal("6.000")
    assert gamma.sample_size == 0
    assert gamma.succeeded_count == 0
    assert gamma.success_rate_percent is None
    assert gamma.duration_sample_size == 0
    assert gamma.average_duration_seconds is None
    for private_value in (
        "private-alpha-execution-owner",
        "private-execution-trigger",
        "private-execution-input",
        "private-execution-error",
        "private-running-input",
        "private-cancelled-input",
    ):
        assert private_value not in repr(snapshot.execution_summaries)

    application = create_app()

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.get("/")

    assert response.status_code == 200
    assert "Execução recente por automação" in response.text
    assert "Taxa de sucesso" in response.text
    assert "gamma-no-results" in response.text
    assert "private-execution-input" not in response.text


async def test_dashboard_home_renders_redacted_state_and_safe_controls(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run_id = await _seed_dashboard(session_factory)
    application = create_app()

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.get("/")

    assert response.status_code == 200
    assert "Automation Foundry" in response.text
    assert "Content Engine" in response.text
    assert "Worker unavailable" in response.text
    assert ">Idade<" in response.text
    assert ">0.30<" in response.text
    assert ">0.70<" in response.text
    assert f'href="/runs/{run_id}"' in response.text
    assert "/kill-switch" in response.text
    assert "Ativar kill switch" in response.text
    for private_value in (
        "private-owner",
        "private-run-key",
        "input-secret",
        "output-secret",
        "error-secret",
        "private approval summary",
        "private alert summary",
        "private-alert-source",
        "private-ledger-category",
        "private-ledger-source",
        "private-step-key",
        "step-input-secret",
        "step-output-secret",
        "step-error-secret",
        "private/artifact/path.mp4",
        "private-artifact-origin",
        "private-metric-source",
        "metric-dimension-secret",
    ):
        assert private_value not in response.text

    application_routes = [
        route for route in application.routes if isinstance(route, APIRoute)
    ]
    assert len(application_routes) == 22
    route_methods = [route.methods for route in application_routes]
    assert route_methods.count({"GET"}) == 14
    assert route_methods.count({"POST"}) == 8


async def test_health_page_renders_only_redacted_probe_projection(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async def collect(_session: AsyncSession) -> HealthReport:
        return HealthReport(
            status=HealthStatus.DEGRADED,
            checked_at=datetime(2026, 8, 10, 12, 0),
            checks=(
                HealthCheck(
                    name="redis",
                    status=HealthStatus.PASS,
                    summary="broker respondeu ao ping",
                    metrics={"latency_ms": 1.2},
                ),
                HealthCheck(
                    name="workers",
                    status=HealthStatus.DEGRADED,
                    summary="filas sem worker: gpu",
                    metrics={"gpu_workers": 0, "cpu_workers": 1, "io_workers": 1},
                ),
            ),
        )

    application = create_app(health_collector=collect)

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "Saúde operacional" in response.text
    assert "latency_ms" in response.text
    assert "gpu_workers" in response.text
    assert "private-host" not in response.text


async def test_dashboard_starts_idempotent_local_example_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    application = create_app(csrf_token="test-csrf-token")

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    payload = {
        "csrf_token": "test-csrf-token",
        "confirmation": "start-example-run",
        "idempotency_key": "dashboard-smoke-001",
    }
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        first = await client.post("/automations/platform-smoke/runs", data=payload)
        second = await client.post("/automations/platform-smoke/runs", data=payload)

    assert first.status_code == 303
    assert second.status_code == 303
    assert second.headers["location"] == first.headers["location"]
    async with session_factory() as session:
        runs = list((await session.scalars(select(Run))).all())
        steps = list((await session.scalars(select(StepRun))).all())
    assert len(runs) == 1 and runs[0].status == RunStatus.SUCCEEDED.value
    assert len(steps) == 1 and steps[0].status == RunStatus.SUCCEEDED.value


async def test_dashboard_enqueues_validated_content_a1_without_contacting_ollama(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    published: list[tuple[int, str, QueueClass]] = []

    def publish(dispatch_id: int, delivery_id: str, queue: QueueClass) -> None:
        published.append((dispatch_id, delivery_id, queue))

    application = create_app(
        csrf_token="test-csrf-token",
        dispatch_publisher=publish,
    )

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    payload = {
        "csrf_token": "test-csrf-token",
        "confirmation": "start-example-run",
        "idempotency_key": "dashboard-content-a1-001",
        "topic": "private dashboard topic",
        "persona": "private editorial persona",
        "format": "short",
        "recent_openings": "private recent opening",
    }
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        dashboard = await client.get("/")
        first = await client.post("/automations/content-engine/runs", data=payload)
        second = await client.post("/automations/content-engine/runs", data=payload)
        detail = await client.get(first.headers["location"])
        invalid = await client.post(
            "/automations/content-engine/runs",
            data={**payload, "idempotency_key": "invalid-content-a1", "format": "video"},
        )

    assert dashboard.status_code == 200
    assert "Content Engine · roteiro A1" in dashboard.text
    assert 'name="topic"' in dashboard.text
    assert 'name="persona"' in dashboard.text
    assert 'name="format"' in dashboard.text
    assert first.status_code == 303
    assert second.status_code == 303
    assert second.headers["location"] == first.headers["location"]
    assert invalid.status_code == 400
    assert detail.status_code == 200
    assert RunStatus.QUEUED.value in detail.text
    assert "private dashboard topic" not in detail.text
    assert "private editorial persona" not in detail.text
    assert len(published) == 2
    assert published[0][0] == published[1][0]
    assert published[0][1] == published[1][1]
    assert published[0][2] is QueueClass.GPU
    async with session_factory() as session:
        runs = list((await session.scalars(select(Run))).all())
        dispatches = list((await session.scalars(select(RunDispatch))).all())
    assert len(runs) == 1
    assert runs[0].input_payload == {
        "topic": "private dashboard topic",
        "persona": "private editorial persona",
        "format": "short",
        "recent_openings": ["private recent opening"],
    }
    assert len(dispatches) == 1
    assert dispatches[0].queue == QueueClass.GPU.value
    assert dispatches[0].status == "published"


async def test_dashboard_retries_content_a1_as_a_new_gpu_dispatch(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    input_payload = {
        "topic": "Sono e memória",
        "persona": "Ciência acessível",
        "format": "short",
        "recent_openings": [],
    }
    async with session_factory() as session:
        automation = Automation(
            slug="content-engine",
            name="Content Engine",
            owner="local-operator",
        )
        session.add(automation)
        await session.flush()
        source = Run(
            automation_id=automation.id,
            idempotency_key="content-dashboard-retry-source",
            trigger="manual",
            input_payload=input_payload,
            status=RunStatus.FAILED.value,
            error={"code": "CONTENT_LLM_UNAVAILABLE", "retryable": True},
        )
        session.add(source)
        await session.commit()
        source_id = source.id

    published: list[tuple[int, str, QueueClass]] = []
    application = create_app(
        csrf_token="test-csrf-token",
        dispatch_publisher=lambda dispatch_id, delivery_id, queue: published.append(
            (dispatch_id, delivery_id, queue)
        ),
    )

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.post(
            f"/runs/{source_id}/retry",
            data={
                "csrf_token": "test-csrf-token",
                "confirmation": "retry-run",
                "idempotency_key": "content-dashboard-retry-successor",
                "actor": "local-owner",
                "reason": "Ollama local recuperado",
            },
        )

    assert response.status_code == 303
    assert len(published) == 1 and published[0][2] is QueueClass.GPU
    async with session_factory() as session:
        successor = await session.scalar(
            select(Run).where(Run.retry_of_run_id == source_id)
        )
        dispatch = await session.scalar(select(RunDispatch))
    assert successor is not None and successor.status == RunStatus.QUEUED.value
    assert successor.input_payload == input_payload
    assert successor.retry_requested_by == "local-owner"
    assert successor.retry_reason == "Ollama local recuperado"
    assert dispatch is not None and dispatch.run_id == successor.id


async def test_dashboard_shows_experiment_and_attributes_manual_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        automation = Automation(
            slug="platform-smoke",
            name="Platform Smoke Automation",
            owner="local-operator",
        )
        session.add(automation)
        await session.flush()
        experiment = (
            await get_or_create_experiment(
                session,
                automation=automation,
                key="smoke-variant",
                name="Smoke Variant",
                hypothesis="private hypothesis must remain redacted",
                primary_metric="smoke.success_rate",
                primary_metric_unit="ratio",
                control_variant="baseline",
                candidate_variant="candidate",
                actor="local-owner",
                reason="private creation reason",
            )
        ).experiment
        await transition_experiment(
            session,
            experiment=experiment,
            target=ExperimentStatus.RUNNING,
            actor="local-owner",
            reason="private start reason",
        )
        await session.commit()
        experiment_id = experiment.id

    application = create_app(csrf_token="test-csrf-token")

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        dashboard = await client.get("/")
        response = await client.post(
            "/automations/platform-smoke/runs",
            data={
                "csrf_token": "test-csrf-token",
                "confirmation": "start-example-run",
                "idempotency_key": "experiment-smoke-001",
                "experiment_id": str(experiment_id),
            },
        )
        detail = await client.get(response.headers["location"])

    assert dashboard.status_code == 200
    assert "Smoke Variant" in dashboard.text
    assert "smoke.success_rate" in dashboard.text
    assert "private hypothesis must remain redacted" not in dashboard.text
    assert "private creation reason" not in dashboard.text
    assert response.status_code == 303
    assert f"Experimento #{experiment_id}" in detail.text
    async with session_factory() as session:
        attributed_run = await session.scalar(
            select(Run).where(Run.experiment_id == experiment_id)
        )
        stored_experiment = await session.get(Experiment, experiment_id)
    assert attributed_run is not None
    assert stored_experiment is not None


async def test_dashboard_retries_only_eligible_registered_run(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        automation = Automation(
            slug="platform-smoke",
            name="Platform Smoke Automation",
            owner="local-operator",
        )
        session.add(automation)
        await session.flush()
        source = Run(
            automation_id=automation.id,
            idempotency_key="dashboard-retry-source",
            trigger="manual",
            input_payload={"operation": "noop"},
            status=RunStatus.FAILED.value,
            error={"code": "TEMPORARY_FAILURE", "retryable": True},
        )
        session.add(source)
        await session.commit()
        source_id = source.id

    application = create_app(csrf_token="test-csrf-token")

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    payload = {
        "csrf_token": "test-csrf-token",
        "confirmation": "retry-run",
        "idempotency_key": "dashboard-retry-001",
        "actor": "local-owner",
        "reason": "temporary dependency recovered",
    }
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        detail = await client.get(f"/runs/{source_id}")
        first = await client.post(f"/runs/{source_id}/retry", data=payload)
        second = await client.post(f"/runs/{source_id}/retry", data=payload)

    assert detail.status_code == 200
    assert "Retentar run" in detail.text
    assert first.status_code == 303
    assert second.headers["location"] == first.headers["location"]
    async with session_factory() as session:
        retry = await session.scalar(
            select(Run).where(Run.retry_of_run_id == source_id)
        )
    assert retry is not None
    assert retry.status == RunStatus.SUCCEEDED.value
    assert retry.retry_requested_by == "local-owner"
    assert retry.retry_reason == "temporary dependency recovered"


async def test_dashboard_kill_switch_is_confirmed_and_audited(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run_id = await _seed_dashboard(session_factory)
    async with session_factory() as session:
        run = await session.get(Run, run_id)
        assert run is not None
        automation_id = run.automation_id

    application = create_app(csrf_token="test-csrf-token")

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.post(
            f"/automations/{automation_id}/kill-switch",
            data={
                "csrf_token": "test-csrf-token",
                "confirmation": "kill-switch-enable",
                "target": "enable",
                "actor": "local-owner",
                "reason": "pause module for maintenance",
            },
        )
        updated = await client.get("/")

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert "Desativar kill switch" in updated.text
    async with session_factory() as session:
        automation = await session.get(Automation, automation_id)
        events = list(
            (
                await session.scalars(
                    select(ControlEvent).where(
                        ControlEvent.automation_id == automation_id
                    )
                )
            ).all()
        )
    assert automation is not None and automation.kill_switch_active is True
    assert len(events) == 1
    assert events[0].event_type == ControlEventType.KILL_SWITCH_ENABLED.value
    assert events[0].actor == "local-owner"


async def test_dashboard_disables_automation_idempotently_and_blocks_new_runs(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        automation = Automation(
            slug="platform-smoke",
            name="Platform Smoke Automation",
            owner="local-operator",
        )
        session.add(automation)
        await session.commit()
        automation_id = automation.id

    application = create_app(csrf_token="test-csrf-token")

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    disable_payload = {
        "csrf_token": "test-csrf-token",
        "confirmation": "automation-disable",
        "target": "disable",
        "actor": "local-owner",
        "reason": "pause new dashboard intake",
    }
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        first = await client.post(
            f"/automations/{automation_id}/enabled",
            data=disable_payload,
        )
        duplicate = await client.post(
            f"/automations/{automation_id}/enabled",
            data={**disable_payload, "reason": "duplicate must not replace evidence"},
        )
        updated = await client.get("/")
        blocked_start = await client.post(
            "/automations/platform-smoke/runs",
            data={
                "csrf_token": "test-csrf-token",
                "confirmation": "start-example-run",
                "idempotency_key": "disabled-dashboard-run",
            },
        )

    assert first.status_code == 303
    assert duplicate.status_code == 303
    assert updated.status_code == 200
    assert "Habilitar módulo" in updated.text
    assert "administrativamente desabilitada" in updated.text
    assert blocked_start.status_code == 409
    async with session_factory() as session:
        fetched = await session.get(Automation, automation_id)
        events = list(
            (
                await session.scalars(
                    select(ControlEvent).where(
                        ControlEvent.automation_id == automation_id
                    )
                )
            ).all()
        )
        runs = list((await session.scalars(select(Run))).all())
    assert fetched is not None and fetched.enabled is False
    assert len(events) == 1
    assert events[0].event_type == ControlEventType.AUTOMATION_DISABLED.value
    assert events[0].actor == "local-owner"
    assert events[0].reason == "pause new dashboard intake"
    assert runs == []


@pytest.mark.parametrize(
    ("csrf_token", "confirmation", "expected_status"),
    (
        ("wrong-token", "automation-disable", 403),
        ("test-csrf-token", "not-confirmed", 400),
    ),
)
async def test_dashboard_enabled_toggle_rejects_untrusted_or_unconfirmed_requests(
    session_factory: async_sessionmaker[AsyncSession],
    csrf_token: str,
    confirmation: str,
    expected_status: int,
) -> None:
    run_id = await _seed_dashboard(session_factory)
    async with session_factory() as session:
        run = await session.get(Run, run_id)
        assert run is not None
        automation_id = run.automation_id
    application = create_app(csrf_token="test-csrf-token")

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.post(
            f"/automations/{automation_id}/enabled",
            data={
                "csrf_token": csrf_token,
                "confirmation": confirmation,
                "target": "disable",
                "actor": "local-owner",
                "reason": "must not persist",
            },
        )

    assert response.status_code == expected_status
    async with session_factory() as session:
        automation = await session.get(Automation, automation_id)
        events = list(
            (
                await session.scalars(
                    select(ControlEvent).where(
                        ControlEvent.automation_id == automation_id
                    )
                )
            ).all()
        )
    assert automation is not None and automation.enabled is True
    assert events == []


async def test_dashboard_kill_switch_rejects_invalid_csrf_without_mutation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run_id = await _seed_dashboard(session_factory)
    async with session_factory() as session:
        run = await session.get(Run, run_id)
        assert run is not None
        automation_id = run.automation_id
    application = create_app(csrf_token="test-csrf-token")

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.post(
            f"/automations/{automation_id}/kill-switch",
            data={
                "csrf_token": "wrong-token",
                "confirmation": "kill-switch-enable",
                "target": "enable",
                "actor": "local-owner",
                "reason": "must not persist",
            },
        )

    assert response.status_code == 403
    async with session_factory() as session:
        automation = await session.get(Automation, automation_id)
        events = list(
            (
                await session.scalars(
                    select(ControlEvent).where(
                        ControlEvent.automation_id == automation_id
                    )
                )
            ).all()
        )
    assert automation is not None and automation.kill_switch_active is False
    assert events == []


async def test_run_detail_projection_is_ordered_exact_and_redacted(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run_id = await _seed_dashboard(session_factory)
    async with session_factory() as session:
        detail = await load_run_detail(session, run_id=run_id)

    assert detail is not None
    assert detail.automation_slug == "content-engine"
    assert detail.duration_seconds == Decimal("12.345")
    assert detail.failure is not None
    assert detail.failure.code == "RUN_FAILED"
    assert detail.steps[0].name == "render-video"
    assert detail.steps[0].attempt == 2
    assert detail.steps[0].queue == QueueClass.CPU.value
    assert detail.steps[0].duration_seconds == Decimal("2.5")
    assert detail.steps[0].failure is not None
    assert detail.steps[0].failure.code == "UNEXPECTED_TASK_ERROR"
    assert detail.approvals[0].action == "publish"
    assert detail.artifacts[0].media_type == "video/mp4"
    assert detail.metrics[0].value == Decimal("2.5000000000")
    assert detail.ledger_entries[0].amount == Decimal("0.1000000000")
    rendered = repr(detail)
    for private_value in (
        "private-run-key",
        "input-secret",
        "output-secret",
        "error-secret",
        "private approval summary",
        "private-step-key",
        "step-input-secret",
        "step-output-secret",
        "step-error-secret",
        "private/artifact/path.mp4",
        "private-artifact-origin",
        "private-metric-source",
        "metric-dimension-secret",
        "private-ledger-category",
        "private-ledger-source",
    ):
        assert private_value not in rendered


async def test_content_outcome_is_run_scoped_allowlisted_and_decimal_exact(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run_id = await _seed_content_outcome(session_factory)
    async with session_factory() as session:
        detail = await load_run_detail(session, run_id=run_id)

    assert detail is not None
    outcome = detail.content_outcome
    assert outcome is not None
    assert outcome.published is False
    assert outcome.observed_through == datetime(2026, 8, 12, 9, 0, 15)
    assert [indicator.name for indicator in outcome.indicators] == [
        "content_caption_transcript_match_ratio",
        "content_originality_max_similarity",
    ]
    assert outcome.indicators[0].label == "Correspondência da legenda"
    assert outcome.indicators[0].value == Decimal("0.9876543210")
    assert outcome.estimated_energy_kwh == Decimal("0.0300000000")
    assert [(total.currency, total.cost) for total in outcome.ledger_totals] == [
        ("BRL", Decimal("0.3000000003")),
        ("USD", Decimal("0.4000000004")),
    ]
    assert outcome.ledger_totals[0].revenue == Decimal("1.0000000004")
    assert outcome.ledger_totals[0].attributed_value == Decimal("2.3456789012")
    assert outcome.ledger_totals[0].net_revenue == Decimal("0.7000000001")
    assert outcome.ledger_totals[1].net_revenue == Decimal("-0.4000000004")
    rendered = repr(outcome)
    for excluded_value in (
        "private_unclassified_metric",
        "secret-unit",
        "999.9999999999",
        "private-content-outcome-source",
        "content-outcome-dimension",
        "private-content-outcome-category",
        "private-content-outcome-ledger-source",
    ):
        assert excluded_value not in rendered


async def test_content_outcome_renders_local_scope_and_no_external_claim(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run_id = await _seed_content_outcome(session_factory)
    application = create_app()

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.get(f"/runs/{run_id}")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "Resultado local do conteúdo" in response.text
    assert "não publicado" in response.text
    assert "Correspondência da legenda" in response.text
    assert "Similaridade máxima" in response.text
    assert "0.0300000000 kWh" in response.text
    assert "0.3000000003" in response.text
    assert "0.7000000001" in response.text
    assert "-0.4000000004" in response.text
    assert "E-" not in response.text
    assert "Nenhuma plataforma externa foi consultada" in response.text
    assert "private-content-outcome-source" not in response.text
    assert "content-outcome-dimension" not in response.text
    assert "private-content-outcome-category" not in response.text
    assert "private-content-outcome-ledger-source" not in response.text


async def _seed_caption_alignment_outcome(
    factory: async_sessionmaker[AsyncSession],
) -> int:
    observed_at = datetime(2026, 8, 13, 10, 0)
    async with factory() as session:
        automation = Automation(
            slug="content-engine",
            name="Content Engine",
            owner="private-alignment-owner",
        )
        session.add(automation)
        await session.flush()
        run = Run(
            automation_id=automation.id,
            idempotency_key="private-alignment-run",
            trigger="manual",
            input_payload={"private": "alignment-input"},
            output_payload={"published": False},
            status=RunStatus.SUCCEEDED.value,
            queued_at=observed_at,
            started_at=observed_at,
            finished_at=observed_at + timedelta(seconds=5),
        )
        session.add(run)
        await session.flush()
        specs = (
            (
                "content_caption_alignment_speech_coverage_ratio",
                MetricKind.RATIO,
                "0.4200000000",
                "ratio",
            ),
            (
                "content_caption_alignment_onset_offset_seconds",
                MetricKind.GAUGE,
                "-0.7500000000",
                "seconds",
            ),
            (
                "content_caption_alignment_speech_segment_count",
                MetricKind.COUNTER,
                "3.0000000000",
                "segments",
            ),
        )
        for index, (name, kind, value, unit) in enumerate(specs, start=1):
            session.add(
                MetricPoint(
                    automation_id=automation.id,
                    run_id=run.id,
                    idempotency_key=f"private-alignment-metric-{index}",
                    name=name,
                    kind=kind.value,
                    value=Decimal(value),
                    unit=unit,
                    source="private-alignment-source",
                    dimensions={"private": "alignment-dimension"},
                    observed_at=observed_at + timedelta(seconds=index),
                )
            )
        await session.commit()
        assert run.id is not None
        return run.id


async def test_caption_alignment_indicators_are_run_scoped_and_keep_their_sign(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run_id = await _seed_caption_alignment_outcome(session_factory)
    application = create_app()

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    async with session_factory() as session:
        detail = await load_run_detail(session, run_id=run_id)

    assert detail is not None
    outcome = detail.content_outcome
    assert outcome is not None
    assert [indicator.name for indicator in outcome.indicators] == [
        "content_caption_alignment_speech_coverage_ratio",
        "content_caption_alignment_onset_offset_seconds",
        "content_caption_alignment_speech_segment_count",
    ]
    assert all(indicator.step_run_id is None for indicator in outcome.indicators)
    assert outcome.indicators[1].value == Decimal("-0.7500000000")

    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.get(f"/runs/{run_id}")

    assert response.status_code == 200
    assert "Fala coberta pela legenda" in response.text
    assert "Desvio no início da legenda" in response.text
    assert "-0.7500000000" in response.text
    assert "private-alignment-source" not in response.text
    assert "alignment-dimension" not in response.text


async def test_run_detail_renders_the_verified_caption_alignment_report(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    relative_path = "content-engine/run-1/captions/alignment-0123456789abcdef.json"
    report_path = storage_root / relative_path
    report_path.parent.mkdir(parents=True)
    audio_relative_path = "content-engine/run-1/audio/narration.wav"
    caption_relative_path = "content-engine/run-1/captions/captions.ass"
    audio_path = storage_root / audio_relative_path
    caption_path = storage_root / caption_relative_path
    audio_path.parent.mkdir(parents=True)
    audio_path.write_bytes(b"verified dashboard audio fixture")
    caption_path.write_bytes(b"verified dashboard caption fixture")
    async with session_factory() as session:
        automation = Automation(
            slug="content-engine",
            name="Content Engine",
            owner="private-alignment-owner",
        )
        session.add(automation)
        await session.flush()
        run = Run(
            automation_id=automation.id,
            idempotency_key="private-alignment-view-run",
            trigger="manual",
            input_payload={"private": "alignment-input"},
            output_payload={"published": False},
            status=RunStatus.SUCCEEDED.value,
        )
        session.add(run)
        await session.flush()
        audio_artifact = Artifact(
            run_id=run.id,
            idempotency_key="private-alignment-view-audio",
            artifact_type="narration_audio",
            relative_path=audio_relative_path,
            media_type="audio/wav",
            sha256=hashlib.sha256(audio_path.read_bytes()).hexdigest(),
            size_bytes=audio_path.stat().st_size,
            origin="content-engine:a2",
            sensitivity=ArtifactSensitivity.INTERNAL.value,
        )
        caption_artifact = Artifact(
            run_id=run.id,
            idempotency_key="private-alignment-view-caption",
            artifact_type="caption_ass",
            relative_path=caption_relative_path,
            media_type="text/x-ssa",
            sha256=hashlib.sha256(caption_path.read_bytes()).hexdigest(),
            size_bytes=caption_path.stat().st_size,
            origin="content-engine:a4",
            sensitivity=ArtifactSensitivity.INTERNAL.value,
        )
        session.add_all((audio_artifact, caption_artifact))
        await session.flush()
        payload = {
            "schema_version": 1,
            "algorithm": "rms-window-energy-vs-ass-events-v1",
            "run_id": run.id,
            "caption_artifact_id": caption_artifact.id,
            "caption_sha256": caption_artifact.sha256,
            "audio_artifact_id": audio_artifact.id,
            "audio_sha256": audio_artifact.sha256,
            "parameters": {"min_speech_coverage": "0.90"},
            "parameters_digest": "0123456789abcdef",
            "measurement": {
                "audio_duration_seconds": "12.000000",
                "speech_seconds": "6.000000",
                "caption_seconds": "12.000000",
                "speech_covered_seconds": "6.000000",
                "caption_outside_speech_seconds": "6.000000",
                "speech_coverage_ratio": "1.0000000000",
                "caption_outside_speech_ratio": "0.5000000000",
                "speech_segment_count": 2,
                "caption_block_count": 1,
                "caption_event_count": 3,
                "events_beyond_audio": 0,
                "onset_offset_seconds": "-0.750000",
                "end_offset_seconds": "0.000000",
                "max_block_onset_offset_seconds": None,
                "peak_dbfs": "-11.70",
                "threshold_dbfs": "-41.70",
                "status": "warning",
                "reasons": ["CAPTIONS_OVER_SILENCE"],
            },
            "external_service_consulted": False,
            "published": False,
        }
        encoded = (
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        report_path.write_bytes(encoded)
        session.add(
            Artifact(
                run_id=run.id,
                idempotency_key="private-alignment-view-report",
                artifact_type="caption_alignment_report",
                relative_path=relative_path,
                media_type="application/json",
                sha256=hashlib.sha256(encoded).hexdigest(),
                size_bytes=len(encoded),
                origin="content-engine:a4-alignment",
                sensitivity=ArtifactSensitivity.INTERNAL.value,
            )
        )
        await session.commit()
        run_id = run.id

    application = create_app()

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            caption_alignment,
            "settings",
            Settings(_env_file=None, storage_root=str(storage_root)),
        )
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://127.0.0.1",
        ) as client:
            response = await client.get(f"/runs/{run_id}")

            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert "Alinhamento da legenda" in response.text
            assert "fora da tolerância" in response.text
            assert "CAPTIONS_OVER_SILENCE" in response.text
            assert "-0.750000 s" in response.text
            assert "0123456789abcdef" in response.text
            assert relative_path not in response.text
            assert str(storage_root) not in response.text

            report_path.write_bytes(b"{}\n")
            degraded = await client.get(f"/runs/{run_id}")

    assert degraded.status_code == 200
    assert "não pôde ser reconferido" in degraded.text
    assert "CAPTIONS_OVER_SILENCE" not in degraded.text


async def test_run_detail_renders_linked_evidence_without_mutation_or_private_data(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run_id = await _seed_dashboard(session_factory)
    application = create_app()

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.get(f"/runs/{run_id}")
        unavailable_export = await client.get(f"/runs/{run_id}/local-export")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert unavailable_export.status_code == 404
    assert f"Run #{run_id}" in response.text
    assert "12.345 s" in response.text
    assert "render-video" in response.text
    assert "UNEXPECTED_TASK_ERROR" in response.text
    assert "video/mp4" in response.text
    assert "render_duration" in response.text
    assert ">0.10<" in response.text
    assert "controle local" in response.text
    assert '<script src="/static/htmx.min.js" defer></script>' in response.text
    assert "cdn.jsdelivr.net" not in response.text
    assert 'id="run-live-status"' in response.text
    assert "hx-get=" not in response.text
    assert "<form" not in response.text
    for private_value in (
        "private-run-key",
        "input-secret",
        "output-secret",
        "error-secret",
        "private approval summary",
        "private-step-key",
        "step-input-secret",
        "step-output-secret",
        "step-error-secret",
        "private/artifact/path.mp4",
        "private-artifact-origin",
        "private-metric-source",
        "metric-dimension-secret",
        "private-ledger-category",
        "private-ledger-source",
    ):
        assert private_value not in response.text


async def test_run_detail_live_fragment_polls_locally_until_terminal(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run_id = await _seed_cancellable_run(session_factory)
    application = create_app()

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        page = await client.get(f"/runs/{run_id}")
        fragment = await client.get(f"/runs/{run_id}/fragment")
        asset = await client.get("/static/htmx.min.js")

        async with session_factory() as session:
            run = await session.scalar(select(Run).where(Run.id == run_id))
            assert run is not None
            await transition_run(session, run, RunStatus.RUNNING)
            await transition_run(session, run, RunStatus.SUCCEEDED)
            await session.commit()

        terminal_fragment = await client.get(f"/runs/{run_id}/fragment")

    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert f'hx-get="/runs/{run_id}/fragment"' in page.text
    assert 'hx-trigger="every 2s"' in page.text
    assert 'hx-swap="outerHTML"' in page.text
    assert '<script src="/static/htmx.min.js" defer></script>' in page.text

    assert fragment.status_code == 200
    assert fragment.headers["cache-control"] == "no-store"
    assert 'id="run-live-status"' in fragment.text
    assert "Steps e tentativas" in fragment.text
    assert RunStatus.QUEUED.value in fragment.text
    assert "<html" not in fragment.text
    assert "<form" not in fragment.text
    assert "private-cancellable-run-key" not in fragment.text
    assert "cancellable-input-secret" not in fragment.text

    assert asset.status_code == 200
    assert asset.headers["content-type"].startswith("text/javascript")
    assert 'version:"2.0.10"' in asset.text

    assert terminal_fragment.status_code == 200
    assert RunStatus.SUCCEEDED.value in terminal_fragment.text
    assert "hx-get=" not in terminal_fragment.text
    assert "hx-trigger=" not in terminal_fragment.text


async def test_run_detail_returns_not_found_without_database_mutation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    application = create_app()

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.get("/runs/999")
        fragment = await client.get("/runs/999/fragment")

    assert response.status_code == 404
    assert fragment.status_code == 404


async def test_dashboard_cancellation_is_confirmed_idempotent_and_audited(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run_id = await _seed_cancellable_run(session_factory)
    application = create_app(csrf_token="test-csrf-token")

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        detail = await client.get(f"/runs/{run_id}")
        first = await client.post(
            f"/runs/{run_id}/cancel",
            data={
                "csrf_token": "test-csrf-token",
                "confirmation": "cancel-run",
                "reason": "operator requested a safe local stop",
            },
        )
        duplicate = await client.post(
            f"/runs/{run_id}/cancel",
            data={
                "csrf_token": "test-csrf-token",
                "confirmation": "cancel-run",
                "reason": "duplicate request must not replace the first reason",
            },
        )
        updated_detail = await client.get(f"/runs/{run_id}")

    assert detail.status_code == 200
    assert f'action="/runs/{run_id}/cancel"' in detail.text
    assert 'value="test-csrf-token"' in detail.text
    assert "Confirmo o cancelamento desta run" in detail.text
    assert first.status_code == 303
    assert first.headers["location"] == f"/runs/{run_id}"
    assert duplicate.status_code == 303
    assert updated_detail.status_code == 200
    assert "Cancelamento solicitado em" in updated_detail.text
    assert "operator requested a safe local stop" not in updated_detail.text
    assert "duplicate request must not replace the first reason" not in updated_detail.text
    assert "<form" not in updated_detail.text
    async with session_factory() as session:
        run = await session.get(Run, run_id)
        events = list(
            (
                await session.scalars(
                    select(ControlEvent).where(ControlEvent.run_id == run_id)
                )
            ).all()
        )

    assert run is not None
    assert run.status == RunStatus.CANCELLED.value
    assert run.cancellation_requested_at is not None
    assert run.cancellation_reason == "operator requested a safe local stop"
    assert len(events) == 1
    assert events[0].event_type == ControlEventType.CANCELLATION_REQUESTED.value


@pytest.mark.parametrize(
    ("csrf_token", "confirmation", "expected_status"),
    (
        ("wrong-token", "cancel-run", 403),
        ("test-csrf-token", "not-confirmed", 400),
    ),
)
async def test_dashboard_cancellation_rejects_untrusted_or_unconfirmed_requests(
    session_factory: async_sessionmaker[AsyncSession],
    csrf_token: str,
    confirmation: str,
    expected_status: int,
) -> None:
    run_id = await _seed_cancellable_run(session_factory)
    application = create_app(csrf_token="test-csrf-token")

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.post(
            f"/runs/{run_id}/cancel",
            data={
                "csrf_token": csrf_token,
                "confirmation": confirmation,
                "reason": "must not be persisted",
            },
        )

    assert response.status_code == expected_status
    async with session_factory() as session:
        run = await session.get(Run, run_id)
        events = list(
            (
                await session.scalars(
                    select(ControlEvent).where(ControlEvent.run_id == run_id)
                )
            ).all()
        )
    assert run is not None
    assert run.status == RunStatus.QUEUED.value
    assert run.cancellation_requested_at is None
    assert events == []


async def test_dashboard_cancellation_rejects_terminal_run_without_audit_event(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run_id = await _seed_dashboard(session_factory)
    application = create_app(csrf_token="test-csrf-token")

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.post(
            f"/runs/{run_id}/cancel",
            data={
                "csrf_token": "test-csrf-token",
                "confirmation": "cancel-run",
                "reason": "too late",
            },
        )

    assert response.status_code == 409
    async with session_factory() as session:
        events = list(
            (
                await session.scalars(
                    select(ControlEvent).where(ControlEvent.run_id == run_id)
                )
            ).all()
        )
    assert events == []


async def test_dashboard_rejects_non_loopback_host_before_database_access() -> None:
    application = create_app(csrf_token="test-csrf-token")
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://attacker.example",
    ) as client:
        response = await client.get("/")

    assert response.status_code == 400
    assert response.text == "invalid host"


async def test_dashboard_approval_rejection_is_confirmed_immutable_and_audited(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run_id, approval_id = await _seed_pending_approval(session_factory)
    application = create_app(csrf_token="test-csrf-token")

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        detail = await client.get(f"/runs/{run_id}")
        first = await client.post(
            f"/approvals/{approval_id}/reject",
            data={
                "csrf_token": "test-csrf-token",
                "confirmation": "reject-approval",
                "actor": "local-owner",
                "reason": "protected action must not proceed",
            },
        )
        duplicate = await client.post(
            f"/approvals/{approval_id}/reject",
            data={
                "csrf_token": "test-csrf-token",
                "confirmation": "reject-approval",
                "actor": "different-actor",
                "reason": "duplicate must not replace immutable evidence",
            },
        )
        updated_detail = await client.get(f"/runs/{run_id}")

    assert detail.status_code == 200
    assert f'action="/approvals/{approval_id}/reject"' in detail.text
    assert "Confirmo a rejeição definitiva" in detail.text
    assert "Aprovar approval" not in detail.text
    assert "/approve" not in detail.text
    assert "private protected approval summary" not in detail.text
    assert "protected-payload-secret" not in detail.text
    assert first.status_code == 303
    assert first.headers["location"] == f"/runs/{run_id}"
    assert duplicate.status_code == 303
    assert updated_detail.status_code == 200
    assert "rejected" in updated_detail.text
    assert "<form" not in updated_detail.text
    for private_value in (
        "private protected approval summary",
        "protected-payload-secret",
        "local-owner",
        "protected action must not proceed",
        "different-actor",
        "duplicate must not replace immutable evidence",
    ):
        assert private_value not in updated_detail.text

    async with session_factory() as session:
        run = await session.get(Run, run_id)
        approval = await session.get(Approval, approval_id)
        events = list(
            (
                await session.scalars(
                    select(ApprovalEvent).where(
                        ApprovalEvent.approval_id == approval_id
                    )
                )
            ).all()
        )

    assert run is not None
    assert run.status == RunStatus.CANCELLED.value
    assert approval is not None
    assert approval.status == ApprovalStatus.REJECTED.value
    assert approval.decided_by == "local-owner"
    assert approval.decision_reason == "protected action must not proceed"
    assert len(events) == 1
    assert events[0].to_status == ApprovalStatus.REJECTED.value
    assert events[0].actor == "local-owner"


async def test_dashboard_approval_can_be_safely_reviewed_and_approved(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run_id, approval_id = await _seed_pending_approval(
        session_factory,
        with_review_projection=True,
    )
    application = create_app(csrf_token="test-csrf-token")

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        detail = await client.get(f"/runs/{run_id}")
        response = await client.post(
            f"/approvals/{approval_id}/approve",
            data={
                "csrf_token": "test-csrf-token",
                "confirmation": "approve-approval",
                "actor": "local-owner",
                "reason": "reviewed private artifact",
            },
        )

    assert detail.status_code == 200
    assert "Video #42" in detail.text
    assert "Aprovar approval" in detail.text
    # Sem página de revisão dedicada, a decisão acontece num diálogo da própria run.
    assert f'id="decidir-{approval_id}"' in detail.text
    assert f'action="/approvals/{approval_id}/approve"' in detail.text
    assert f'action="/approvals/{approval_id}/reject"' in detail.text
    assert f'href="/approvals/{approval_id}/review"' not in detail.text
    assert response.status_code == 303
    async with session_factory() as session:
        run = await session.get(Run, run_id)
        approval = await session.get(Approval, approval_id)
    assert run is not None and run.status == RunStatus.RUNNING.value
    assert approval is not None and approval.status == ApprovalStatus.APPROVED.value


async def test_dashboard_reviews_and_approves_complete_a1_script(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_root = tmp_path / "storage"
    monkeypatch.setattr(
        a1_executor,
        "settings",
        SimpleNamespace(
            storage_root=str(storage_root),
            ollama_model="fake-local-model",
            celery_soft_time_limit_seconds=300,
        ),
    )
    async with session_factory() as session:
        result = await execute_content_script_run(
            session,
            idempotency_key="dashboard-content-a1-review",
            input_payload={
                "topic": "Sono e memória",
                "persona": "persona-private-must-not-render",
                "format": "short",
                "recent_openings": [],
            },
            script_generator=lambda *_args: ScriptResult(
                angle="Ângulo editorial completo",
                outline="1. Hook\n2. Explicação\n3. Fecho",
                full_script="Roteiro completo para revisão humana.",
                hook="Hook editorial",
                narration="Narração integral para revisão humana.",
            ),
            storage_root=storage_root,
        )
        await session.commit()
    assert result.approval_id is not None

    application = create_app(csrf_token="test-csrf-token")

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        home = await client.get("/")
        detail = await client.get(f"/runs/{result.run_id}")
        review = await client.get(f"/approvals/{result.approval_id}/review")
        approved = await client.post(
            f"/approvals/{result.approval_id}/approve",
            data={
                "csrf_token": "test-csrf-token",
                "confirmation": "approve-approval",
                "actor": "local-owner",
                "reason": "roteiro completo revisado",
            },
        )
        decided_review = await client.get(
            f"/approvals/{result.approval_id}/review"
        )

    assert home.status_code == 200
    _assert_review_link(home.text, result.approval_id, "review_content_script_a1")
    assert detail.status_code == 200
    assert "Abrir roteiro completo e verificado" in detail.text
    # Com página de revisão, ela é o único lugar de decidir: sem formulário inline.
    assert f'href="/approvals/{result.approval_id}/review"' in detail.text
    assert f'action="/approvals/{result.approval_id}/approve"' not in detail.text
    assert f'action="/approvals/{result.approval_id}/reject"' not in detail.text
    assert review.status_code == 200
    assert review.headers["cache-control"] == "no-store"
    assert "Roteiro completo para revisão humana." in review.text
    assert "Narração integral para revisão humana." in review.text
    assert "persona-private-must-not-render" not in review.text
    assert f'action="/approvals/{result.approval_id}/approve"' in review.text
    assert f'action="/approvals/{result.approval_id}/reject"' in review.text
    assert 'value="test-csrf-token"' in review.text
    assert approved.status_code == 303
    assert decided_review.status_code == 200
    assert "Decisão registrada" in decided_review.text
    assert f'action="/approvals/{result.approval_id}/approve"' not in decided_review.text
    async with session_factory() as session:
        run = await session.get(Run, result.run_id)
        approval = await session.get(Approval, result.approval_id)
    assert run is not None and run.status == RunStatus.SUCCEEDED.value
    assert approval is not None and approval.status == ApprovalStatus.APPROVED.value


async def test_dashboard_approval_requeues_enabled_tts_continuation_once(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_root = tmp_path / "storage"
    monkeypatch.setattr(
        a1_executor,
        "generate_script",
        lambda *_args: ScriptResult(
            angle="Ângulo editorial",
            outline="Estrutura editorial",
            full_script="Roteiro integral aprovado.",
            hook="Hook editorial",
            narration="Narração integral aprovada.",
        ),
    )
    monkeypatch.setattr(
        a1_executor,
        "settings",
        SimpleNamespace(
            storage_root=str(storage_root),
            ollama_model="fake-local-model",
            celery_soft_time_limit_seconds=300,
            content_tts_backend="test",
            content_tts_voice_id="pf_test",
            content_tts_language_code="p",
            gpu_power_watts=400,
        ),
    )
    async with session_factory() as session:
        prepared = await publish_registered_dispatch(
            session,
            slug="content-engine",
            idempotency_key="dashboard-a2-continuation",
            input_payload={
                "topic": "Sono e memória",
                "persona": "Ciência acessível",
                "format": "short",
                "recent_openings": [],
            },
            publish=lambda *_args: None,
        )
        await execute_claimed_dispatch(
            session,
            dispatch_id=prepared.dispatch_id,
            delivery_id=prepared.delivery_id,
            worker_id="gpu-worker-a1",
            lease_seconds=600,
        )
        approval = await session.scalar(
            select(Approval).where(Approval.run_id == prepared.run_id)
        )
        assert approval is not None
        approval_id = approval.id

    published: list[tuple[int, str, QueueClass]] = []
    application = create_app(
        csrf_token="test-csrf-token",
        dispatch_publisher=lambda dispatch_id, delivery_id, queue: published.append(
            (dispatch_id, delivery_id, queue)
        ),
    )

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    decision = {
        "csrf_token": "test-csrf-token",
        "confirmation": "approve-approval",
        "actor": "local-owner",
        "reason": "roteiro completo revisado",
    }
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        first = await client.post(f"/approvals/{approval_id}/approve", data=decision)
        duplicate = await client.post(
            f"/approvals/{approval_id}/approve",
            data=decision,
        )

    assert first.status_code == 303 and duplicate.status_code == 303
    assert published == [
        (prepared.dispatch_id, prepared.delivery_id, QueueClass.GPU)
    ]
    async with session_factory() as session:
        run = await session.get(Run, prepared.run_id)
        dispatch = await session.get(RunDispatch, prepared.dispatch_id)
        approval = await session.get(Approval, approval_id)
    assert run is not None and run.status == RunStatus.RUNNING.value
    assert dispatch is not None and dispatch.status == "published"
    assert approval is not None and approval.status == ApprovalStatus.APPROVED.value


@pytest.mark.parametrize(
    ("csrf_token", "confirmation", "actor", "expected_status"),
    (
        ("wrong-token", "reject-approval", "local-owner", 403),
        ("test-csrf-token", "not-confirmed", "local-owner", 400),
        ("test-csrf-token", "reject-approval", "", 400),
    ),
)
async def test_dashboard_approval_rejection_fails_closed_for_invalid_requests(
    session_factory: async_sessionmaker[AsyncSession],
    csrf_token: str,
    confirmation: str,
    actor: str,
    expected_status: int,
) -> None:
    run_id, approval_id = await _seed_pending_approval(session_factory)
    application = create_app(csrf_token="test-csrf-token")

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.post(
            f"/approvals/{approval_id}/reject",
            data={
                "csrf_token": csrf_token,
                "confirmation": confirmation,
                "actor": actor,
                "reason": "must not be persisted",
            },
        )

    assert response.status_code == expected_status
    async with session_factory() as session:
        run = await session.get(Run, run_id)
        approval = await session.get(Approval, approval_id)
        events = list(
            (
                await session.scalars(
                    select(ApprovalEvent).where(
                        ApprovalEvent.approval_id == approval_id
                    )
                )
            ).all()
        )
    assert run is not None
    assert run.status == RunStatus.AWAITING_APPROVAL.value
    assert approval is not None
    assert approval.status == ApprovalStatus.PENDING.value
    assert events == []


async def test_dashboard_approval_rejection_conflicts_with_invalid_run_state(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    run_id = await _seed_dashboard(session_factory)
    async with session_factory() as session:
        approval = await session.scalar(
            select(Approval).where(Approval.run_id == run_id)
        )
    assert approval is not None
    application = create_app(csrf_token="test-csrf-token")

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.post(
            f"/approvals/{approval.id}/reject",
            data={
                "csrf_token": "test-csrf-token",
                "confirmation": "reject-approval",
                "actor": "local-owner",
                "reason": "run is already terminal",
            },
        )

    assert response.status_code == 409
    async with session_factory() as session:
        fetched = await session.get(Approval, approval.id)
        events = list(
            (
                await session.scalars(
                    select(ApprovalEvent).where(
                        ApprovalEvent.approval_id == approval.id
                    )
                )
            ).all()
        )
    assert fetched is not None
    assert fetched.status == ApprovalStatus.PENDING.value
    assert events == []


async def test_dashboard_home_handles_empty_database(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    application = create_app()

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.get("/")

    assert response.status_code == 200
    assert "Nenhuma automação registrada" in response.text
    assert "Nenhuma run registrada" in response.text
    assert "Nenhum schedule registrado" in response.text
    assert "Nenhuma observação financeira atribuída a uma automação" in response.text
    assert "Nenhuma automação registrada para calcular indicadores" in response.text


async def test_dashboard_reviews_and_approves_the_final_video_gate(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_root = tmp_path / "storage"
    import_root = tmp_path / "imports" / "content-visuals"
    write_synthetic_visual_import(
        import_root,
        count=expected_visual_count(),
        width=1080,
        height=1920,
    )
    monkeypatch.setattr(
        a1_executor,
        "settings",
        rehearsal_settings(
            storage_root,
            import_root=import_root,
            final_review_enabled=True,
        ),
    )
    async with session_factory() as session:
        first = await execute_content_script_run(
            session,
            idempotency_key="dashboard-content-a7",
            input_payload=rehearsal_input_payload(),
            script_generator=lambda *_args: rehearsal_script(),
            storage_root=storage_root,
        )
        script_approval = await session.get(Approval, first.approval_id)
        assert script_approval is not None
        await decide_approval(
            session,
            approval=script_approval,
            decision=ApprovalStatus.APPROVED,
            actor="local-owner",
            reason="roteiro integral revisado",
        )
        gated = await execute_content_script_run(
            session,
            idempotency_key="dashboard-content-a7",
            input_payload=rehearsal_input_payload(),
            tts_synthesizer=write_synthetic_narration_wav,
            assembly_adapter=_assembly_adapter,
            storage_root=storage_root,
        )
        final_approval = await session.scalar(
            select(Approval).where(
                Approval.run_id == gated.run_id,
                Approval.action == FINAL_REVIEW_APPROVAL_ACTION,
            )
        )
        assert final_approval is not None
        final_approval_id = final_approval.id
        await session.commit()

    application = create_app(csrf_token="test-csrf-token")

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        home = await client.get("/")
        detail = await client.get(f"/runs/{gated.run_id}")
        approved = await client.post(
            f"/approvals/{final_approval_id}/approve",
            data={
                "csrf_token": "test-csrf-token",
                "confirmation": "approve-approval",
                "actor": "local-owner",
                "reason": "vídeo final revisado localmente",
            },
        )
        local_export_page = await client.get(
            f"/runs/{gated.run_id}/local-export"
        )
        no_thumbnail = await client.get(
            f"/runs/{gated.run_id}/local-export/thumbnail"
        )

    assert home.status_code == 200
    _assert_review_link(home.text, final_approval_id, "review_content_final_video_a7")
    assert detail.status_code == 200
    assert FINAL_REVIEW_APPROVAL_ACTION in detail.text
    assert "Nenhum upload" in detail.text
    assert "Abrir roteiro completo e verificado" not in detail.text
    assert approved.status_code == 303
    assert local_export_page.status_code == 200
    assert no_thumbnail.status_code == 404
    async with session_factory() as session:
        run = await session.get(Run, gated.run_id)
        approval = await session.get(Approval, final_approval_id)
    assert run is not None and run.status == RunStatus.SUCCEEDED.value
    assert run.output_payload is not None
    assert run.output_payload["published"] is False
    assert approval is not None and approval.status == ApprovalStatus.APPROVED.value


async def test_dashboard_final_video_review_page_serves_verified_evidence(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_root = tmp_path / "storage"
    import_root = tmp_path / "imports" / "content-visuals"
    write_synthetic_visual_import(
        import_root,
        count=expected_visual_count(),
        width=1080,
        height=1920,
    )
    monkeypatch.setattr(
        a1_executor,
        "settings",
        rehearsal_settings(
            storage_root,
            import_root=import_root,
            final_review_enabled=True,
        ),
    )
    async with session_factory() as session:
        first = await execute_content_script_run(
            session,
            idempotency_key="dashboard-content-a7-page",
            input_payload=rehearsal_input_payload(),
            script_generator=lambda *_args: rehearsal_script(),
            storage_root=storage_root,
        )
        script_approval = await session.get(Approval, first.approval_id)
        assert script_approval is not None
        script_approval_id = script_approval.id
        await decide_approval(
            session,
            approval=script_approval,
            decision=ApprovalStatus.APPROVED,
            actor="local-owner",
            reason="roteiro integral revisado",
        )
        gated = await execute_content_script_run(
            session,
            idempotency_key="dashboard-content-a7-page",
            input_payload=rehearsal_input_payload(),
            tts_synthesizer=write_synthetic_narration_wav,
            assembly_adapter=_assembly_adapter,
            storage_root=storage_root,
        )
        final_approval = await session.scalar(
            select(Approval).where(
                Approval.run_id == gated.run_id,
                Approval.action == FINAL_REVIEW_APPROVAL_ACTION,
            )
        )
        video = await session.scalar(
            select(Artifact).where(
                Artifact.run_id == gated.run_id,
                Artifact.artifact_type == "final_video",
            )
        )
        assert final_approval is not None and video is not None
        final_approval_id = final_approval.id
        video_sha256 = video.sha256
        video_path = storage_root / video.relative_path
        await session.commit()

    application = create_app(csrf_token="test-csrf-token")

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        detail = await client.get(f"/runs/{gated.run_id}")
        review = await client.get(f"/approvals/{final_approval_id}/review")
        media = await client.get(f"/approvals/{final_approval_id}/final-video")
        wrong_gate = await client.get(f"/approvals/{script_approval_id}/final-video")

    assert detail.status_code == 200
    assert "Assistir ao vídeo final verificado antes de decidir" in detail.text
    assert review.status_code == 200
    assert review.headers["cache-control"] == "no-store"
    assert "1080x1920" in review.text
    assert video_sha256 in review.text
    assert f'src="/approvals/{final_approval_id}/final-video"' in review.text
    assert f'action="/approvals/{final_approval_id}/approve"' in review.text
    assert f'action="/approvals/{final_approval_id}/reject"' in review.text
    assert 'value="test-csrf-token"' in review.text
    assert "Nenhum upload, envio ou gasto existe" in review.text
    assert str(storage_root) not in review.text
    assert media.status_code == 200
    assert media.headers["content-type"] == "video/mp4"
    assert media.headers["cache-control"] == "no-store"
    assert media.content == video_path.read_bytes()
    assert wrong_gate.status_code == 404

    encoded = bytearray(video_path.read_bytes())
    encoded[-1] ^= 0xFF
    video_path.write_bytes(bytes(encoded))
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        tampered_review = await client.get(f"/approvals/{final_approval_id}/review")
        tampered_media = await client.get(f"/approvals/{final_approval_id}/final-video")

    assert tampered_review.status_code == 409
    assert tampered_media.status_code == 409


async def test_dashboard_narration_review_streams_only_verified_audio(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_root = tmp_path / "storage"
    import_root = tmp_path / "imports" / "content-visuals"
    write_synthetic_visual_import(
        import_root,
        count=expected_visual_count(),
        width=1080,
        height=1920,
    )
    configured = rehearsal_settings(
        storage_root,
        import_root=import_root,
        narration_review_enabled=True,
    )
    configured.content_caption_backend = "disabled"
    configured.content_assembly_backend = "disabled"
    monkeypatch.setattr(a1_executor, "settings", configured)

    async with session_factory() as session:
        first = await execute_content_script_run(
            session,
            idempotency_key="dashboard-content-a2-page",
            input_payload=rehearsal_input_payload(),
            script_generator=lambda *_args: rehearsal_script(),
            storage_root=storage_root,
        )
        script_approval = await session.get(Approval, first.approval_id)
        assert script_approval is not None
        script_approval_id = script_approval.id
        await decide_approval(
            session,
            approval=script_approval,
            decision=ApprovalStatus.APPROVED,
            actor="local-owner",
            reason="roteiro integral revisado",
        )
        gated = await execute_content_script_run(
            session,
            idempotency_key="dashboard-content-a2-page",
            input_payload=rehearsal_input_payload(),
            tts_synthesizer=write_synthetic_narration_wav,
            storage_root=storage_root,
        )
        narration_approval = await session.scalar(
            select(Approval).where(
                Approval.run_id == gated.run_id,
                Approval.action == NARRATION_REVIEW_APPROVAL_ACTION,
            )
        )
        audio = await session.scalar(
            select(Artifact).where(
                Artifact.run_id == gated.run_id,
                Artifact.artifact_type == "narration_audio",
            )
        )
        assert narration_approval is not None and audio is not None
        narration_approval_id = narration_approval.id
        audio_path = storage_root / audio.relative_path
        audio_sha256 = audio.sha256
        await session.commit()

    application = create_app(csrf_token="test-csrf-token")

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        home = await client.get("/")
        review = await client.get(f"/approvals/{narration_approval_id}/review")
        media = await client.get(
            f"/approvals/{narration_approval_id}/narration-audio"
        )
        wrong_gate = await client.get(
            f"/approvals/{script_approval_id}/narration-audio"
        )

    assert home.status_code == 200
    _assert_review_link(home.text, narration_approval_id, NARRATION_REVIEW_APPROVAL_ACTION)
    assert review.status_code == 200
    assert review.headers["cache-control"] == "no-store"
    assert audio_sha256 in review.text
    assert f">{expected_visual_count()} PNGs<" in review.text
    assert "1080x1920" in review.text
    assert str(storage_root) not in review.text
    assert media.status_code == 200
    assert media.headers["content-type"] == "audio/wav"
    assert media.headers["cache-control"] == "no-store"
    assert media.headers["x-content-type-options"] == "nosniff"
    assert media.content == audio_path.read_bytes()
    assert wrong_gate.status_code == 404

    audio_path.write_bytes(audio_path.read_bytes() + b"tampered")
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        tampered_review = await client.get(
            f"/approvals/{narration_approval_id}/review"
        )
        tampered_media = await client.get(
            f"/approvals/{narration_approval_id}/narration-audio"
        )

    assert tampered_review.status_code == 409
    assert tampered_media.status_code == 409


async def test_storage_page_shows_totals_without_leaking_private_paths(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    private_path = "content-engine/7/script/customer-name.json"

    async def collect(_session: AsyncSession) -> RetentionInventory:
        return RetentionInventory(
            generated_at=datetime(2026, 8, 11, 12, 0),
            storage_root_present=True,
            scanned_file_count=2,
            scanned_bytes=2560,
            excluded_file_count=1,
            excluded_bytes=99,
            classes=(
                RetentionClassSummary(RetentionClass.RETAINED, 1, 512),
                RetentionClassSummary(RetentionClass.EXPIRED, 1, 2048),
            ),
            automations=(
                RetentionAutomationSummary(
                    automation_slug="content-engine",
                    file_count=2,
                    total_bytes=2560,
                    expired_file_count=1,
                    expired_bytes=2048,
                    hold_file_count=0,
                    missing_file_count=0,
                ),
            ),
            items=(
                RetentionItem(
                    classification=RetentionClass.EXPIRED,
                    reason="retention_elapsed",
                    relative_path=private_path,
                    size_bytes=2048,
                    artifact_id=41,
                    run_id=7,
                    automation_slug="content-engine",
                ),
            ),
        )

    application = create_app(retention_collector=collect)

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.get("/storage")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "Retenção de storage" in response.text
    assert "content-engine" in response.text
    assert "2048" in response.text
    assert "apagados 0" in response.text
    assert private_path not in response.text
    assert "customer-name" not in response.text


async def test_storage_page_fails_safe_when_inventory_is_unsafe(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async def collect(_session: AsyncSession) -> RetentionInventory:
        raise RetentionInventoryError("C:/private/customer-name/storage is a symlink")

    application = create_app(retention_collector=collect)

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.get("/storage")

    assert response.status_code == 200
    assert "não pôde ser concluído" in response.text
    assert "customer-name" not in response.text
    assert "symlink" not in response.text
    # The declared policy is static evidence, so it stays visible even when the
    # inventory itself cannot be produced safely.
    assert "Política declarativa por tipo de artifact" in response.text
    assert "script_bundle" in response.text


async def test_storage_page_declares_the_retention_policy_per_artifact_type(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async def collect(_session: AsyncSession) -> RetentionInventory:
        return RetentionInventory(
            generated_at=datetime(2026, 8, 13, 12, 0),
            storage_root_present=True,
            scanned_file_count=0,
            scanned_bytes=0,
            excluded_file_count=0,
            excluded_bytes=0,
            classes=(),
            automations=(),
            items=(),
        )

    application = create_app(retention_collector=collect)

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.get("/storage")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    for artifact_type in (
        "script_bundle",
        "narration_audio",
        "visual_manifest",
        "visual_image",
        "caption_ass",
        "caption_alignment_report",
        "final_video",
        "originality_report",
    ):
        assert artifact_type in response.text
    assert "90 dias" in response.text
    assert "retenção indefinida" in response.text
    assert "nunca antecipa o vencimento de algo já registrado" in response.text


async def test_dashboard_approves_a_purge_gate_without_deleting_anything(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    relative_path = "content-engine/1/customer-name.json"
    target = storage_root / relative_path
    target.parent.mkdir(parents=True)
    content = b'{"expired":"but still on disk"}\n'
    target.write_bytes(content)
    configuration = Settings(
        _env_file=None,
        storage_root=str(storage_root),
        retention_purge_enabled=True,
    )

    async with session_factory() as session:
        automation = (
            await get_or_create_automation(
                session,
                slug="content-engine",
                name="Content Engine",
                owner="local-owner",
            )
        ).automation
        run = (
            await get_or_create_run(
                session,
                automation=automation,
                idempotency_key="dashboard-purge-source",
            )
        ).run
        await transition_run(session, run, RunStatus.RUNNING)
        artifact = (
            await register_local_artifact(
                session,
                run=run,
                storage_root=storage_root,
                file_path=Path(relative_path),
                idempotency_key="dashboard-purge:artifact",
                artifact_type="script",
                media_type="application/json",
                origin="content-engine",
                retention_days=30,
            )
        ).artifact
        artifact.retention_until = datetime(2026, 1, 1)
        await transition_run(session, run, RunStatus.SUCCEEDED)
        plan = await plan_retention_purge(
            session,
            actor="local-owner",
            reason="dashboard review",
            configuration=configuration,
        )
        await session.commit()
        purge_run_id = plan.run_id
        approval_id = plan.approval_id

    assert approval_id is not None
    application = create_app(csrf_token="test-csrf-token")

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        detail = await client.get(f"/runs/{purge_run_id}")
        response = await client.post(
            f"/approvals/{approval_id}/approve",
            data={
                "csrf_token": "test-csrf-token",
                "confirmation": "approve-approval",
                "actor": "local-owner",
                "reason": "reviewed the expired list",
            },
        )

    assert detail.status_code == 200
    assert "irrever" in detail.text.casefold()
    assert relative_path not in detail.text
    assert "customer-name" not in detail.text
    assert response.status_code == 303

    # Approving authorizes the purge; it never performs it.
    assert target.read_bytes() == content
    async with session_factory() as session:
        approval = await session.get(Approval, approval_id)
        purged = await session.get(Artifact, artifact.id)
    assert approval is not None
    assert approval.status == ApprovalStatus.APPROVED.value
    assert purged is not None
    assert purged.purged_at is None


async def _prepare_dashboard_a8(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    idempotency_key: str,
) -> tuple[int, int, Path]:
    storage_root = tmp_path / "storage"
    import_root = tmp_path / "imports" / "content-visuals"
    write_synthetic_visual_import(
        import_root,
        count=expected_visual_count(),
        width=1080,
        height=1920,
    )
    monkeypatch.setattr(
        a1_executor,
        "settings",
        rehearsal_settings(
            storage_root,
            import_root=import_root,
            final_review_enabled=True,
            thumbnail_review_enabled=True,
        ),
    )
    async with session_factory() as session:
        first = await execute_content_script_run(
            session,
            idempotency_key=idempotency_key,
            input_payload=rehearsal_input_payload(),
            script_generator=lambda *_args: rehearsal_script(),
            storage_root=storage_root,
        )
        script_approval = await session.get(Approval, first.approval_id)
        assert script_approval is not None
        await decide_approval(
            session,
            approval=script_approval,
            decision=ApprovalStatus.APPROVED,
            actor="local-owner",
            reason="roteiro integral revisado",
        )
        gated = await execute_content_script_run(
            session,
            idempotency_key=idempotency_key,
            input_payload=rehearsal_input_payload(),
            tts_synthesizer=write_synthetic_narration_wav,
            assembly_adapter=_assembly_adapter,
            storage_root=storage_root,
        )
        final_approval = await session.scalar(
            select(Approval).where(
                Approval.run_id == gated.run_id,
                Approval.action == FINAL_REVIEW_APPROVAL_ACTION,
            )
        )
        assert final_approval is not None
        final_approval_id = final_approval.id
        await session.commit()
    assert final_approval_id is not None
    return gated.run_id, final_approval_id, storage_root


async def test_dashboard_a8_serves_verified_pngs_and_persists_exact_selection(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, final_approval_id, storage_root = await _prepare_dashboard_a8(
        session_factory,
        tmp_path,
        monkeypatch,
        idempotency_key="dashboard-content-a8",
    )
    application = create_app(csrf_token="test-csrf-token")

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        a7_response = await client.post(
            f"/approvals/{final_approval_id}/approve",
            data={
                "csrf_token": "test-csrf-token",
                "confirmation": "approve-approval",
                "actor": "local-owner",
                "reason": "vídeo final revisado localmente",
            },
        )
    assert a7_response.status_code == 303

    async with session_factory() as session:
        thumbnail_approval = await session.scalar(
            select(Approval).where(
                Approval.run_id == run_id,
                Approval.action == THUMBNAIL_REVIEW_APPROVAL_ACTION,
            )
        )
        candidates = tuple(
            (
                await session.scalars(
                    select(Artifact)
                    .where(
                        Artifact.run_id == run_id,
                        Artifact.artifact_type == "visual_image",
                    )
                    .order_by(Artifact.id)
                )
            ).all()
        )
    assert thumbnail_approval is not None and candidates
    approval_id = thumbnail_approval.id
    selected = candidates[0]

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        home = await client.get("/")
        detail = await client.get(f"/runs/{run_id}")
        review = await client.get(f"/approvals/{approval_id}/review")
        images = [
            await client.get(
                f"/approvals/{approval_id}/thumbnails/{candidate.id}"
            )
            for candidate in candidates
        ]
        approved = await client.post(
            f"/approvals/{approval_id}/approve",
            data={
                "csrf_token": "test-csrf-token",
                "confirmation": "approve-approval",
                "actor": "local-owner",
                "reason": "melhor representação visual do vídeo",
                "thumbnail_artifact_id": str(selected.id),
            },
        )

    assert home.status_code == 200
    _assert_review_link(home.text, approval_id, "select_content_thumbnail_a8")
    assert detail.status_code == 200
    assert "Escolher uma thumbnail entre os PNGs A3 verificados" in detail.text
    assert "A aprovação A8 exige uma escolha" in detail.text
    assert review.status_code == 200
    assert review.headers["cache-control"] == "no-store"
    assert "Escolha da thumbnail A8" in review.text
    assert selected.sha256 in review.text
    assert f'action="/approvals/{approval_id}/approve"' in review.text
    assert f'action="/approvals/{approval_id}/reject"' in review.text
    assert "Rejeitar todos os candidatos" in review.text
    assert 'value="test-csrf-token"' in review.text
    assert str(storage_root) not in review.text
    assert all(response.status_code == 200 for response in images)
    assert all(response.headers["content-type"] == "image/png" for response in images)
    assert all(response.headers["cache-control"] == "no-store" for response in images)
    assert images[0].content == (storage_root / selected.relative_path).read_bytes()
    assert approved.status_code == 303

    async with session_factory() as session:
        run = await session.get(Run, run_id)
        approval = await session.get(Approval, approval_id)
        final_video = await session.scalar(
            select(Artifact).where(
                Artifact.run_id == run_id,
                Artifact.artifact_type == "final_video",
            )
        )
    assert run is not None and run.status == RunStatus.SUCCEEDED.value
    assert run.output_payload is not None
    complete_output = dict(run.output_payload)
    assert run.output_payload["thumbnail_artifact_id"] == selected.id
    assert run.output_payload["thumbnail_sha256"] == selected.sha256
    assert run.output_payload["published"] is False
    assert approval is not None
    assert final_video is not None
    assert approval.decision_payload == {
        "thumbnail_artifact_id": selected.id,
        "thumbnail_sha256": selected.sha256,
    }

    final_review_loads = 0
    original_load_final_video_review = local_export.load_final_video_review

    async def count_final_video_review(
        session: AsyncSession,
        *,
        approval: Approval,
        storage_root: Path,
    ) -> FinalVideoReview:
        nonlocal final_review_loads
        final_review_loads += 1
        return await original_load_final_video_review(
            session,
            approval=approval,
            storage_root=storage_root,
        )

    async def reject_duplicate_final_video_review(
        _session: AsyncSession,
        *,
        approval: Approval,
        storage_root: Path,
    ) -> FinalVideoReview:
        del approval, storage_root
        raise AssertionError("A7 evidence was verified twice in one export request")

    monkeypatch.setattr(
        local_export,
        "load_final_video_review",
        count_final_video_review,
    )
    monkeypatch.setattr(
        thumbnail_review,
        "load_final_video_review",
        reject_duplicate_final_video_review,
    )

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        completed_detail = await client.get(f"/runs/{run_id}")
        export_page = await client.get(f"/runs/{run_id}/local-export")
        manifest = await client.get(f"/runs/{run_id}/local-export/manifest.json")
        video = await client.get(f"/runs/{run_id}/local-export/video")
        thumbnail = await client.get(f"/runs/{run_id}/local-export/thumbnail")

    assert completed_detail.status_code == 200
    assert f'href="/runs/{run_id}/local-export"' in completed_detail.text
    assert export_page.status_code == 200
    assert export_page.headers["cache-control"] == "no-store"
    assert "Export local revisável" in export_page.text
    assert "published=false" in export_page.text
    assert final_video.sha256 in export_page.text
    assert selected.sha256 in export_page.text
    assert str(storage_root) not in export_page.text
    assert manifest.status_code == 200
    assert manifest.headers["cache-control"] == "no-store"
    manifest_payload = manifest.json()
    assert manifest_payload["run_id"] == run_id
    assert manifest_payload["published"] is False
    assert [item["role"] for item in manifest_payload["files"]] == [
        "final_video",
        "thumbnail",
    ]
    assert str(storage_root) not in manifest.text
    assert video.status_code == 200
    assert video.headers["cache-control"] == "no-store"
    assert video.content == (storage_root / final_video.relative_path).read_bytes()
    assert thumbnail.status_code == 200
    assert thumbnail.headers["cache-control"] == "no-store"
    assert thumbnail.content == (storage_root / selected.relative_path).read_bytes()
    assert final_review_loads == 4

    selected_path = storage_root / selected.relative_path
    tampered = bytearray(selected_path.read_bytes())
    tampered[-1] ^= 0xFF
    selected_path.write_bytes(bytes(tampered))
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        unsafe_export = await client.get(f"/runs/{run_id}/local-export")
    assert unsafe_export.status_code == 409

    selected_path.write_bytes(thumbnail.content)
    async with session_factory() as session:
        persisted_run = await session.get(Run, run_id)
        assert persisted_run is not None and persisted_run.output_payload is not None
        divergent_output = dict(persisted_run.output_payload)
        divergent_output["thumbnail_sha256"] = "0" * 64
        persisted_run.output_payload = divergent_output
        await session.commit()
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        divergent_export = await client.get(f"/runs/{run_id}/local-export")
    assert divergent_export.status_code == 409

    omitted_a8_output = dict(complete_output)
    for key in (
        "thumbnail_artifact_id",
        "thumbnail_sha256",
        "thumbnail_review_approval_id",
    ):
        omitted_a8_output.pop(key)
    async with session_factory() as session:
        persisted_run = await session.get(Run, run_id)
        assert persisted_run is not None
        persisted_run.output_payload = omitted_a8_output
        await session.commit()
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        omitted_a8_export = await client.get(f"/runs/{run_id}/local-export")
    assert omitted_a8_export.status_code == 409


async def test_dashboard_a8_rejects_arbitrary_id_and_tampered_candidate(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, final_approval_id, storage_root = await _prepare_dashboard_a8(
        session_factory,
        tmp_path,
        monkeypatch,
        idempotency_key="dashboard-content-a8-invalid",
    )
    application = create_app(csrf_token="test-csrf-token")

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        opened = await client.post(
            f"/approvals/{final_approval_id}/approve",
            data={
                "csrf_token": "test-csrf-token",
                "confirmation": "approve-approval",
                "actor": "local-owner",
                "reason": "vídeo revisado",
            },
        )
    assert opened.status_code == 303

    async with session_factory() as session:
        approval = await session.scalar(
            select(Approval).where(
                Approval.run_id == run_id,
                Approval.action == THUMBNAIL_REVIEW_APPROVAL_ACTION,
            )
        )
        candidate = await session.scalar(
            select(Artifact).where(
                Artifact.run_id == run_id,
                Artifact.artifact_type == "visual_image",
            )
        )
    assert approval is not None and candidate is not None
    approval_id = approval.id
    candidate_path = storage_root / candidate.relative_path

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        arbitrary = await client.post(
            f"/approvals/{approval_id}/approve",
            data={
                "csrf_token": "test-csrf-token",
                "confirmation": "approve-approval",
                "actor": "local-owner",
                "reason": "ID forjado pelo navegador",
                "thumbnail_artifact_id": "999999",
            },
        )
    assert arbitrary.status_code == 400

    encoded = bytearray(candidate_path.read_bytes())
    encoded[-1] ^= 0xFF
    candidate_path.write_bytes(bytes(encoded))
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        tampered_review = await client.get(f"/approvals/{approval_id}/review")
        tampered_image = await client.get(
            f"/approvals/{approval_id}/thumbnails/{candidate.id}"
        )
    assert tampered_review.status_code == 409
    assert tampered_image.status_code == 409

    async with session_factory() as session:
        run = await session.get(Run, run_id)
        persisted = await session.get(Approval, approval_id)
    assert run is not None and run.status == RunStatus.AWAITING_APPROVAL.value
    assert run.output_payload is None
    assert persisted is not None and persisted.status == ApprovalStatus.PENDING.value
    assert persisted.decision_payload is None


async def test_dashboard_reviews_and_approves_the_operations_brief(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_root = tmp_path / "storage"
    monkeypatch.setattr(
        brief_executor,
        "settings",
        SimpleNamespace(storage_root=str(storage_root)),
    )
    async with session_factory() as session:
        result = await brief_executor.execute_operations_brief_run(
            session,
            idempotency_key="dashboard-operations-brief",
            input_payload={"window_days": 7, "window_end": "2026-10-08"},
            storage_root=storage_root,
            clock=lambda: datetime(2026, 10, 8, 12, 0, 0),
        )
        await session.commit()
    assert result.approval_id is not None

    application = create_app(csrf_token="test-csrf-token")

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        home = await client.get("/")
        detail = await client.get(f"/runs/{result.run_id}")
        review = await client.get(f"/approvals/{result.approval_id}/review")
        brief_file = next(storage_root.rglob("brief.md"))
        brief_file.write_text(
            brief_file.read_text(encoding="utf-8") + "adulterado", encoding="utf-8"
        )
        tampered = await client.get(f"/approvals/{result.approval_id}/review")

    assert home.status_code == 200
    _assert_review_link(home.text, result.approval_id, "review_operations_brief")
    assert "Abrir o brief completo e verificado" in detail.text
    assert review.status_code == 200
    assert review.headers["cache-control"] == "no-store"
    assert "# Brief operacional do Automation Foundry" in review.text
    assert "Nada foi enviado, publicado ou gasto" in review.text
    assert f'action="/approvals/{result.approval_id}/approve"' in review.text
    assert f'action="/approvals/{result.approval_id}/reject"' in review.text
    assert 'value="test-csrf-token"' in review.text
    assert tampered.status_code == 409


async def test_javascript_media_type_does_not_depend_on_the_operating_system(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The Windows registry reports application/javascript on some machines, such as the CI runner.
    monkeypatch.setattr(mimetypes, "types_map", {**mimetypes.types_map, ".js": "application/javascript"})
    monkeypatch.setattr(mimetypes, "_db", None, raising=False)
    mimetypes.add_type("application/javascript", ".js")
    application = create_app()

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        asset = await client.get("/static/htmx.min.js")

    assert asset.status_code == 200
    assert asset.headers["content-type"].startswith("text/javascript")


async def _fetch_home(session_factory: async_sessionmaker[AsyncSession]) -> str:
    application = create_app(csrf_token="test-csrf-token")

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.get("/")
    assert response.status_code == 200
    return response.text


async def test_dashboard_home_puts_pending_decisions_and_alerts_first(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_dashboard(session_factory)

    html = await _fetch_home(session_factory)

    attention = html.index('id="precisa-de-voce"')
    assert attention < html.index('id="runs"') < html.index('id="automacoes"')
    assert "Approval pendente" in html
    assert "Worker unavailable" in html
    assert "Nada precisa de você agora" not in html
    assert "Tudo em ordem" not in html
    assert re.search(r"\d+ itens? pedem? atenção", html)


async def test_dashboard_home_shows_a_good_empty_state_when_nothing_needs_attention(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    html = await _fetch_home(session_factory)

    assert "Nada precisa de você agora" in html
    assert "Nenhuma aprovação pendente e nenhum alerta ativo." in html
    assert "Tudo em ordem" in html
    assert "pedem atenção" not in html


async def test_dashboard_home_has_unique_ids_and_a_dialog_per_manual_executor(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_dashboard(session_factory)

    html = await _fetch_home(session_factory)

    ids = re.findall(r'\sid="([^"]+)"', html)
    duplicated = sorted({value for value in ids if ids.count(value) > 1})
    assert duplicated == []
    manual_slugs = [
        executor.slug
        for executor in list_automation_executors()
        if executor.supports_manual_start
    ]
    assert manual_slugs
    for slug in manual_slugs:
        assert f'id="executar-{slug}"' in html
        assert f'action="/automations/{slug}/runs"' in html
    # Cada ação de escrita continua sendo um POST com token CSRF e confirmação.
    assert html.count('name="csrf_token"') == html.count("<form ")
    assert html.count('name="confirmation"') == html.count("<form ")
