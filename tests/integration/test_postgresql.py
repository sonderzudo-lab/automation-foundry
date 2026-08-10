"""Opt-in PostgreSQL migration and concurrent-session integration test."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta
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
from src.platform.example_run import ExampleRunResult, execute_example_run
from src.platform.models import Alert, AlertStatus, HealthCondition


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
        finally:
            await engine.dispose()

        assert first.run_status == "succeeded"
        assert second.run_status == "succeeded"
        assert first.run_id != second.run_id
        assert replay.run_id == first.run_id
        assert replay.replayed is True

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
