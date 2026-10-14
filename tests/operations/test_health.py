"""Tests for redacted local health projections and parsers."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Awaitable

import pytest
from celery import Celery
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.config import Settings
from src.core.database import Base
from src.operations import health
from src.operations.health import HealthCheck, HealthStatus


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as database_session:
        yield database_session
    await engine.dispose()


def test_worker_queue_projection_counts_bindings_without_hostnames() -> None:
    counts = health._worker_queue_counts(
        {
            "gpu@private-host": [{"name": "gpu"}],
            "cpu@private-host": [{"name": "cpu"}],
            "io@private-host": [{"name": "io"}, {"name": "unknown"}],
            "malformed": "private-value",
        }
    )

    assert counts == {"gpu": 1, "cpu": 1, "io": 1}
    assert "private" not in repr(counts)


def test_worker_queue_projection_accepts_current_task_as_io_evidence() -> None:
    counts = health._worker_queue_counts(
        {
            "gpu@private-host": [{"name": "gpu"}],
            "cpu@private-host": [{"name": "cpu"}],
        },
        assumed_queues=frozenset({"io", "unknown"}),
    )

    assert counts == {"gpu": 1, "cpu": 1, "io": 1}


async def test_worker_probe_allows_celery_inspection_completion_margin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    celery = Celery("health-worker-test")
    observed: dict[str, float] = {}

    class FakeInspector:
        def active_queues(self) -> dict[str, list[dict[str, str]]]:
            return {
                "gpu@private-host": [{"name": "gpu"}],
                "cpu@private-host": [{"name": "cpu"}],
                "io@private-host": [{"name": "io"}],
            }

    def inspect(*, timeout: float) -> FakeInspector:
        observed["inspect_timeout"] = timeout
        return FakeInspector()

    original_wait_for = health.asyncio.wait_for

    async def wait_for(
        awaitable: Awaitable[object],
        *,
        timeout: float,
    ) -> object:
        observed["outer_timeout"] = timeout
        return await original_wait_for(awaitable, timeout=timeout)

    monkeypatch.setattr(celery.control, "inspect", inspect)
    monkeypatch.setattr(health.asyncio, "wait_for", wait_for)

    check = await health.probe_workers(celery, timeout=1.5)

    assert check.status is HealthStatus.PASS
    assert check.metrics == {"gpu_workers": 1, "cpu_workers": 1, "io_workers": 1}
    assert observed == {"inspect_timeout": 1.5, "outer_timeout": 3.5}


def test_gpu_parser_accepts_only_bounded_numeric_rows() -> None:
    rows = health._parse_gpu_rows(
        "NVIDIA RTX 3090, 24576, 1024, 15\n"
        "malformed, secret\n"
        f"{'x' * 150}, 100, 20, 5\n"
    )

    assert rows == [
        ("NVIDIA RTX 3090", 24576, 1024, 15),
        ("x" * 100, 100, 20, 5),
    ]


async def test_collect_health_redacts_unexpected_probe_failure(
    monkeypatch: pytest.MonkeyPatch,
    session: AsyncSession,
) -> None:
    passing = HealthCheck("ok", HealthStatus.PASS, "seguro", {})

    async def machine(_configuration: Settings) -> tuple[HealthCheck, ...]:
        return (passing,)

    async def fail_redis(_configuration: Settings) -> HealthCheck:
        raise RuntimeError("redis://user:secret@private-host")

    async def single(*_args: object, **_kwargs: object) -> HealthCheck:
        return passing

    monkeypatch.setattr(health, "probe_machine", machine)
    monkeypatch.setattr(health, "probe_database", single)
    monkeypatch.setattr(health, "probe_redis", fail_redis)
    monkeypatch.setattr(health, "probe_workers", single)
    monkeypatch.setattr(health, "probe_beat", single)
    monkeypatch.setattr(health, "probe_gpu", single)

    report = await health.collect_health_report(
        session,
        configuration=Settings(_env_file=None),
        celery=Celery("health-test"),
    )

    assert report.status is HealthStatus.FAIL
    assert any(check.name == "redis" and check.status is HealthStatus.FAIL for check in report.checks)
    assert "secret" not in repr(report)
    assert "private-host" not in repr(report)


async def test_database_probe_returns_only_latency(session: AsyncSession) -> None:
    check = await health.probe_database(session, timeout=1)

    assert check.status is HealthStatus.PASS
    assert set(check.metrics) == {"latency_ms"}


def _replies_without_gpu() -> dict[str, list[dict[str, str]]]:
    return {"cpu@private-host": [{"name": "cpu"}], "io@private-host": [{"name": "io"}]}


async def test_worker_probe_counts_a_busy_worker_with_a_live_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    celery = Celery("health-busy-worker-test")

    class FakeInspector:
        def active_queues(self) -> dict[str, list[dict[str, str]]]:
            return _replies_without_gpu()

    monkeypatch.setattr(celery.control, "inspect", lambda *, timeout: FakeInspector())

    busy = await health.probe_workers(celery, timeout=1, busy_queues=frozenset({"gpu"}))
    missing = await health.probe_workers(celery, timeout=1)

    assert busy.status is HealthStatus.PASS
    assert busy.metrics["gpu_workers"] == 1
    assert "ocupado com tarefa em execução: gpu" in busy.summary
    assert missing.status is HealthStatus.FAIL
    assert missing.summary == "filas sem worker: gpu"


async def test_busy_queues_come_only_from_live_claims(session: AsyncSession) -> None:
    from datetime import UTC, datetime, timedelta

    from src.platform.models import Automation, Run, RunDispatch

    automation = Automation(slug="content-engine", name="Content Engine", owner="local-owner")
    session.add(automation)
    await session.flush()
    now = datetime.now(UTC).replace(tzinfo=None)
    rows = [
        ("gpu", "claimed", now + timedelta(minutes=5)),
        ("cpu", "claimed", now - timedelta(minutes=5)),
        ("io", "completed", now + timedelta(minutes=5)),
    ]
    for index, (queue, status, lease) in enumerate(rows):
        run = Run(
            automation_id=automation.id,
            idempotency_key=f"busy-{index}",
            status="running",
            trigger="manual",
        )
        session.add(run)
        await session.flush()
        session.add(
            RunDispatch(
                run_id=run.id,
                queue=queue,
                task_name="automation_foundry.run_dispatch",
                delivery_id=f"delivery-{index}",
                status=status,
                lease_expires_at=lease,
            )
        )
    await session.flush()

    assert await health._busy_dispatch_queues(session) == frozenset({"gpu"})
