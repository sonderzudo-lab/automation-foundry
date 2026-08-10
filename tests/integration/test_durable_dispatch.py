"""Opt-in PostgreSQL + Redis + Celery durable dispatch integration test."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from alembic.config import Config
from celery.contrib.testing.worker import start_worker
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from alembic import command
from src.core.celery_app import build_celery_app
from src.core.config import Settings, settings
from src.core.database import build_engine
from src.platform.dispatch_service import publish_registered_dispatch
from src.platform.models import QueueClass, Run, RunDispatch, ScheduleOccurrence, StepRun
from src.platform.run_service import get_or_create_automation
from src.platform.schedule_service import get_or_create_schedule, set_schedule_enabled


@pytest.mark.postgresql
@pytest.mark.redis
def test_real_worker_completes_dispatch_and_ignores_duplicate_delivery() -> None:
    database_url = os.getenv("TEST_POSTGRESQL_URL")
    broker_url = os.getenv("TEST_REDIS_URL")
    result_url = os.getenv("TEST_REDIS_RESULT_URL")
    if not database_url or not broker_url or not result_url:
        pytest.skip("PostgreSQL and Redis integration URLs are not configured")
    if settings.database_url != database_url:
        pytest.skip("DATABASE_URL must match TEST_POSTGRESQL_URL for worker integration")

    configuration = Settings(  # type: ignore[call-arg]
        _env_file=None,
        database_url=database_url,
        redis_url=broker_url,
        celery_result_backend_url=result_url,
    )
    alembic_config = Config("alembic.ini")
    alembic_config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(alembic_config, "head")
    app = build_celery_app(configuration)
    engine = build_engine(configuration)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    key = f"durable-dispatch-{uuid4().hex}"

    def publish(dispatch_id: int, delivery_id: str, queue: QueueClass) -> None:
        queue_name = queue.value
        app.send_task(
            "automation_foundry.dispatch.execute",
            args=[dispatch_id, delivery_id],
            task_id=delivery_id,
            queue=queue_name,
            routing_key=queue_name,
            ignore_result=True,
        )

    async def enqueue() -> tuple[int, int, str]:
        async with factory() as session:
            prepared = await publish_registered_dispatch(
                session,
                slug="platform-smoke",
                idempotency_key=key,
                publish=publish,
            )
            return prepared.run_id, prepared.dispatch_id, prepared.delivery_id

    async def await_completion(run_id: int, dispatch_id: int) -> None:
        for _ in range(100):
            async with factory() as session:
                run = await session.get(Run, run_id)
                dispatch = await session.get(RunDispatch, dispatch_id)
                if (
                    run is not None
                    and dispatch is not None
                    and run.status == "succeeded"
                    and dispatch.status == "completed"
                ):
                    return
            await asyncio.sleep(0.1)
        raise AssertionError("worker did not complete durable dispatch")

    async def assert_single_step(run_id: int) -> None:
        async with factory() as session:
            count = await session.scalar(
                select(func.count()).select_from(StepRun).where(StepRun.run_id == run_id)
            )
            assert count == 1

    async def exercise() -> None:
        run_id, dispatch_id, delivery_id = await enqueue()
        await await_completion(run_id, dispatch_id)
        publish(dispatch_id, delivery_id, QueueClass.IO)
        await asyncio.sleep(0.5)
        await assert_single_step(run_id)

    try:
        with start_worker(
            app,
            pool="solo",
            concurrency=1,
            queues=["io"],
            perform_ping_check=False,
            loglevel="WARNING",
        ):
            asyncio.run(exercise())
    finally:
        asyncio.run(engine.dispose())
        app.close()


@pytest.mark.postgresql
@pytest.mark.redis
def test_real_schedule_tick_creates_and_completes_scheduled_run() -> None:
    database_url = os.getenv("TEST_POSTGRESQL_URL")
    broker_url = os.getenv("TEST_REDIS_URL")
    result_url = os.getenv("TEST_REDIS_RESULT_URL")
    if not database_url or not broker_url or not result_url:
        pytest.skip("PostgreSQL and Redis integration URLs are not configured")
    if settings.database_url != database_url:
        pytest.skip("DATABASE_URL must match TEST_POSTGRESQL_URL for worker integration")

    configuration = Settings(  # type: ignore[call-arg]
        _env_file=None,
        database_url=database_url,
        redis_url=broker_url,
        celery_result_backend_url=result_url,
    )
    alembic_config = Config("alembic.ini")
    alembic_config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(alembic_config, "head")
    app = build_celery_app(configuration)
    engine = build_engine(configuration)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    schedule_name = f"integration-{uuid4().hex}"

    async def prepare_due_schedule() -> int:
        current = datetime.now(UTC)
        async with factory() as session:
            automation = (
                await get_or_create_automation(
                    session,
                    slug="platform-smoke",
                    name="Platform Smoke Automation",
                    owner="local-operator",
                )
            ).automation
            schedule = (
                await get_or_create_schedule(
                    session,
                    automation=automation,
                    name=schedule_name,
                    cron_expression="* * * * *",
                    timezone_name="UTC",
                    actor="integration-test",
                    reason="real schedule boundary",
                    misfire_grace_seconds=300,
                    now=current - timedelta(minutes=2),
                )
            ).schedule
            await set_schedule_enabled(
                session,
                schedule=schedule,
                enabled=True,
                actor="integration-test",
                reason="real schedule boundary",
                now=current - timedelta(minutes=2),
            )
            await session.commit()
            if not isinstance(schedule.id, int):
                raise AssertionError("schedule was not persisted")
            return schedule.id

    async def await_scheduled_completion(schedule_id: int) -> None:
        for _ in range(150):
            async with factory() as session:
                occurrence = await session.scalar(
                    select(ScheduleOccurrence).where(
                        ScheduleOccurrence.schedule_id == schedule_id
                    )
                )
                run = (
                    None
                    if occurrence is None or occurrence.run_id is None
                    else await session.get(Run, occurrence.run_id)
                )
                if (
                    occurrence is not None
                    and occurrence.status == "published"
                    and run is not None
                    and run.trigger == "schedule"
                    and run.status == "succeeded"
                ):
                    return
            await asyncio.sleep(0.1)
        raise AssertionError("worker did not complete scheduled dispatch")

    async def exercise() -> None:
        schedule_id = await prepare_due_schedule()
        app.send_task(
            "automation_foundry.schedule.tick",
            queue="io",
            routing_key="io",
            ignore_result=True,
        )
        await await_scheduled_completion(schedule_id)

    try:
        with start_worker(
            app,
            pool="solo",
            concurrency=1,
            queues=["io"],
            perform_ping_check=False,
            loglevel="WARNING",
        ):
            asyncio.run(exercise())
    finally:
        asyncio.run(engine.dispose())
        app.close()
