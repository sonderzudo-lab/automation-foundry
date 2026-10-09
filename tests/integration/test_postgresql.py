"""Opt-in PostgreSQL migration and concurrent-session integration test."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from alembic import command
from src.core.config import Settings
from src.core.database import build_engine
from src.operations.health import HealthCheck, HealthReport, HealthStatus
from src.operations.health_alerts import reconcile_health_report
from src.platform.connector_service import record_connector_observation
from src.platform.control_service import request_run_cancellation, set_automation_enabled
from src.platform.example_run import ExampleRunResult, execute_example_run
from src.platform.models import (
    Alert,
    AlertStatus,
    Automation,
    ConnectorObservation,
    ConnectorStatus,
    ControlEvent,
    ControlEventType,
    DataQualityStatus,
    HealthCondition,
    Run,
    Schedule,
    ScheduleEvent,
    ScheduleEventType,
    ScheduleStatus,
)
from src.platform.run_service import (
    IdempotencyConflictError,
    get_or_create_automation,
    get_or_create_run,
)
from src.platform.schedule_service import set_schedule_enabled


@pytest.mark.postgresql
def test_postgresql_migrations_and_concurrent_smoke_runs() -> None:
    database_url = os.getenv("TEST_POSTGRESQL_URL")
    if not database_url:
        pytest.skip("TEST_POSTGRESQL_URL is not configured")

    settings = Settings(_env_file=None, database_url=database_url)
    alembic_config = Config("alembic.ini")
    alembic_config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(alembic_config, "head")
    command.check(alembic_config)

    async def exercise() -> None:
        engine = build_engine(settings)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        prefix = f"postgres-integration-{uuid4().hex}"

        async def execute(key: str) -> ExampleRunResult:
            async with factory() as session:
                result = await execute_example_run(session, idempotency_key=key)
                await session.commit()
                return result

        try:
            first, second = await asyncio.gather(
                execute(f"{prefix}-a"),
                execute(f"{prefix}-b"),
            )
            replay = await execute(f"{prefix}-a")
            async with factory() as session:
                automation = (
                    await get_or_create_automation(
                        session,
                        slug="platform-smoke",
                        name="Platform Smoke Automation",
                        owner="local-operator",
                    )
                ).automation
                schedule = Schedule(
                    automation_id=automation.id,
                    name=f"{prefix}-concurrent-toggle",
                    cron_expression="0 * * * *",
                    timezone="UTC",
                    input_payload={},
                    status=ScheduleStatus.DISABLED.value,
                    next_run_at=None,
                )
                session.add(schedule)
                await session.commit()
                schedule_id = schedule.id

            both_loaded = asyncio.Event()
            loaded_count = 0

            async def enable_schedule() -> bool:
                nonlocal loaded_count
                async with factory() as session:
                    stale_schedule = await session.get(Schedule, schedule_id)
                    assert stale_schedule is not None
                    loaded_count += 1
                    if loaded_count == 2:
                        both_loaded.set()
                    await both_loaded.wait()
                    result = await set_schedule_enabled(
                        session,
                        schedule=stale_schedule,
                        enabled=True,
                        actor="local-owner",
                        reason="concurrent dashboard click",
                        now=datetime(2026, 8, 12, 12, 1, tzinfo=UTC),
                    )
                    await session.commit()
                    return result.changed

            schedule_changes = await asyncio.gather(
                enable_schedule(),
                enable_schedule(),
            )
            async with factory() as session:
                enabled_events = list(
                    (
                        await session.scalars(
                            select(ScheduleEvent).where(
                                ScheduleEvent.schedule_id == schedule_id,
                                ScheduleEvent.event_type
                                == ScheduleEventType.ENABLED.value,
                            )
                        )
                    ).all()
                )

            observations_loaded = asyncio.Event()
            observation_load_count = 0
            observation_time = datetime(2026, 8, 12, 12, 5, tzinfo=UTC)

            async def record_same_observation() -> bool:
                nonlocal observation_load_count
                async with factory() as session:
                    automation = await session.scalar(
                        select(Automation).where(Automation.slug == "platform-smoke")
                    )
                    assert automation is not None
                    observation_load_count += 1
                    if observation_load_count == 2:
                        observations_loaded.set()
                    await observations_loaded.wait()
                    result = await record_connector_observation(
                        session,
                        automation=automation,
                        connector_key="concurrency-probe",
                        idempotency_key=f"{prefix}-connector-observation",
                        status=ConnectorStatus.HEALTHY,
                        status_slo_seconds=60,
                        last_success_at=observation_time,
                        freshness_slo_seconds=60,
                        quality_status=DataQualityStatus.PASS,
                        quality_score="0.9000",
                        observed_at=observation_time,
                        now=observation_time,
                    )
                    await session.commit()
                    return result.created

            observation_changes = await asyncio.gather(
                record_same_observation(),
                record_same_observation(),
            )
            async with factory() as session:
                observations = list(
                    (
                        await session.scalars(
                            select(ConnectorObservation).where(
                                ConnectorObservation.idempotency_key
                                == f"{prefix}-connector-observation"
                            )
                        )
                    ).all()
                )

            divergent_loaded = asyncio.Event()
            divergent_load_count = 0

            async def record_divergent_observation(
                status: ConnectorStatus,
                quality_score: str,
            ) -> str:
                nonlocal divergent_load_count
                async with factory() as session:
                    automation = await session.scalar(
                        select(Automation).where(Automation.slug == "platform-smoke")
                    )
                    assert automation is not None
                    divergent_load_count += 1
                    if divergent_load_count == 2:
                        divergent_loaded.set()
                    await divergent_loaded.wait()
                    try:
                        result = await record_connector_observation(
                            session,
                            automation=automation,
                            connector_key="concurrency-divergent-probe",
                            idempotency_key=(
                                f"{prefix}-divergent-connector-observation"
                            ),
                            status=status,
                            status_slo_seconds=60,
                            last_success_at=observation_time,
                            freshness_slo_seconds=60,
                            quality_status=DataQualityStatus.PASS,
                            quality_score=quality_score,
                            observed_at=observation_time,
                            now=observation_time,
                        )
                    except IdempotencyConflictError:
                        return "conflict"
                    await session.commit()
                    return "created" if result.created else "replayed"

            divergent_results = await asyncio.gather(
                record_divergent_observation(
                    ConnectorStatus.HEALTHY,
                    "0.9000",
                ),
                record_divergent_observation(
                    ConnectorStatus.DEGRADED,
                    "0.8000",
                ),
            )
            async with factory() as session:
                divergent_observations = list(
                    (
                        await session.scalars(
                            select(ConnectorObservation).where(
                                ConnectorObservation.idempotency_key
                                == f"{prefix}-divergent-connector-observation"
                            )
                        )
                    ).all()
                )

            async with factory() as session:
                controlled = Automation(
                    slug=f"{prefix}-controlled",
                    name="Concurrent control",
                    owner="local-owner",
                )
                session.add(controlled)
                await session.commit()
                controlled_id = controlled.id

            controls_loaded = asyncio.Event()
            control_load_count = 0

            async def disable_automation() -> bool:
                nonlocal control_load_count
                async with factory() as session:
                    stale_automation = await session.get(Automation, controlled_id)
                    assert stale_automation is not None
                    control_load_count += 1
                    if control_load_count == 2:
                        controls_loaded.set()
                    await controls_loaded.wait()
                    result = await set_automation_enabled(
                        session,
                        automation=stale_automation,
                        enabled=False,
                        actor="local-owner",
                        reason="concurrent administrative click",
                    )
                    await session.commit()
                    return result.changed

            control_changes = await asyncio.gather(
                disable_automation(),
                disable_automation(),
            )
            async with factory() as session:
                control_events = list(
                    (
                        await session.scalars(
                            select(ControlEvent).where(
                                ControlEvent.automation_id == controlled_id,
                                ControlEvent.event_type
                                == ControlEventType.AUTOMATION_DISABLED.value,
                            )
                        )
                    ).all()
                )

            async with factory() as session:
                automation = await session.scalar(
                    select(Automation).where(Automation.slug == "platform-smoke")
                )
                assert automation is not None
                cancellable = (
                    await get_or_create_run(
                        session,
                        automation=automation,
                        idempotency_key=f"{prefix}-concurrent-cancellation",
                        input_payload={},
                    )
                ).run
                await session.commit()
                cancellable_id = cancellable.id

            cancellations_loaded = asyncio.Event()
            cancellation_load_count = 0

            async def cancel_run(reason: str) -> bool:
                nonlocal cancellation_load_count
                async with factory() as session:
                    stale_run = await session.get(Run, cancellable_id)
                    assert stale_run is not None
                    cancellation_load_count += 1
                    if cancellation_load_count == 2:
                        cancellations_loaded.set()
                    await cancellations_loaded.wait()
                    result = await request_run_cancellation(
                        session,
                        run=stale_run,
                        reason=reason,
                    )
                    await session.commit()
                    return result.changed

            cancellation_changes = await asyncio.gather(
                cancel_run("first concurrent cancellation"),
                cancel_run("second concurrent cancellation"),
            )
            async with factory() as session:
                cancellation_events = list(
                    (
                        await session.scalars(
                            select(ControlEvent).where(
                                ControlEvent.run_id == cancellable_id,
                                ControlEvent.event_type
                                == ControlEventType.CANCELLATION_REQUESTED.value,
                            )
                        )
                    ).all()
                )
        finally:
            await engine.dispose()

        assert first.run_status == "succeeded"
        assert second.run_status == "succeeded"
        assert first.run_id != second.run_id
        assert replay.run_id == first.run_id
        assert replay.replayed is True
        assert sorted(schedule_changes) == [False, True]
        assert len(enabled_events) == 1
        assert sorted(observation_changes) == [False, True]
        assert len(observations) == 1
        assert sorted(divergent_results) == ["conflict", "created"]
        assert len(divergent_observations) == 1
        assert sorted(control_changes) == [False, True]
        assert len(control_events) == 1
        assert sorted(cancellation_changes) == [False, True]
        assert len(cancellation_events) == 1

    asyncio.run(exercise())


@pytest.mark.postgresql
def test_postgresql_persists_health_alert_consecutivity_and_recovery() -> None:
    database_url = os.getenv("TEST_POSTGRESQL_URL")
    if not database_url:
        pytest.skip("TEST_POSTGRESQL_URL is not configured")

    configuration = Settings(_env_file=None, database_url=database_url)
    alembic_config = Config("alembic.ini")
    alembic_config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(alembic_config, "head")
    command.check(alembic_config)

    async def exercise() -> None:
        engine = build_engine(configuration)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        started_at = datetime(2026, 8, 10, 16, 0)

        async def reconcile(at: datetime, status: HealthStatus) -> None:
            report = HealthReport(
                status=status,
                checked_at=at,
                checks=(
                    HealthCheck(
                        name="redis",
                        status=status,
                        summary="must not be persisted",
                        metrics={},
                    ),
                ),
            )
            async with factory() as session:
                await reconcile_health_report(
                    session,
                    report,
                    configuration=configuration,
                )
                await session.commit()

        try:
            await reconcile(started_at, HealthStatus.FAIL)
            await reconcile(started_at + timedelta(minutes=1), HealthStatus.FAIL)
            await reconcile(started_at + timedelta(minutes=2), HealthStatus.PASS)
            await reconcile(started_at + timedelta(minutes=3), HealthStatus.PASS)
            async with factory() as session:
                alert = await session.scalar(select(Alert))
                condition = await session.get(HealthCondition, "redis")
        finally:
            await engine.dispose()

        assert alert is not None and condition is not None
        assert alert.status == AlertStatus.RESOLVED.value
        assert alert.occurrence_count == 1
        assert condition.consecutive_healthy == 2
        assert condition.consecutive_unhealthy == 0

    asyncio.run(exercise())
