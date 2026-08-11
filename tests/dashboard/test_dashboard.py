"""Read-only dashboard projections and HTTP smoke tests."""

from __future__ import annotations

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

from src.core.database import Base, get_session
from src.dashboard.main import create_app
from src.dashboard.service import load_dashboard_snapshot, load_run_detail
from src.operations.health import HealthCheck, HealthReport, HealthStatus
from src.pipeline import a1_executor
from src.pipeline.a1_executor import execute_content_script_run
from src.pipeline.script_gen import ScriptResult
from src.platform.dispatch_service import (
    execute_claimed_dispatch,
    publish_registered_dispatch,
)
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
    ControlEvent,
    ControlEventType,
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
    ScheduleOccurrence,
    ScheduleOccurrenceStatus,
    ScheduleStatus,
    StepRun,
)
from src.platform.run_service import transition_run


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
                    reason_code="private-latest-skip-reason",
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
        "private-latest-skip-reason",
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
        "private-latest-skip-reason",
    ):
        assert private_value not in response.text


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
    assert ">0.3<" in response.text
    assert ">0.7<" in response.text
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
    assert len(application_routes) == 12
    route_methods = [route.methods for route in application_routes]
    assert route_methods.count({"GET"}) == 5
    assert route_methods.count({"POST"}) == 7


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

    assert response.status_code == 200
    assert f"Run #{run_id}" in response.text
    assert "12.345 s" in response.text
    assert "render-video" in response.text
    assert "UNEXPECTED_TASK_ERROR" in response.text
    assert "video/mp4" in response.text
    assert "render_duration" in response.text
    assert ">0.1<" in response.text
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

    assert detail.status_code == 200
    assert "Abrir roteiro completo e verificado" in detail.text
    assert review.status_code == 200
    assert review.headers["cache-control"] == "no-store"
    assert "Roteiro completo para revisão humana." in review.text
    assert "Narração integral para revisão humana." in review.text
    assert "persona-private-must-not-render" not in review.text
    assert approved.status_code == 303
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
