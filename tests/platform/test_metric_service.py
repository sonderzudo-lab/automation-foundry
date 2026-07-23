"""Precision, attribution, and idempotency tests for durable metric points."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.database import Base
from src.platform.metric_service import MetricValidationError, record_metric_point
from src.platform.models import (
    Automation,
    MetricKind,
    MetricPoint,
    QueueClass,
    Run,
    RunStatus,
    StepRun,
)
from src.platform.run_service import (
    IdempotencyConflictError,
    get_or_create_automation,
    get_or_create_run,
    transition_run,
)
from src.platform.step_service import get_or_create_step_run


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as database_session:
        yield database_session
    await engine.dispose()


async def _scope(
    session: AsyncSession,
    *,
    slug: str = "metric-tests",
) -> tuple[Automation, Run, StepRun]:
    automation = (
        await get_or_create_automation(
            session,
            slug=slug,
            name=f"{slug} automation",
            owner="local-owner",
        )
    ).automation
    run = (
        await get_or_create_run(
            session,
            automation=automation,
            idempotency_key=f"{slug}:run",
        )
    ).run
    await transition_run(session, run, RunStatus.RUNNING)
    step_run = (
        await get_or_create_step_run(
            session,
            run=run,
            name="measure",
            queue=QueueClass.CPU,
            ordinal=1,
            idempotency_key=f"{slug}:step",
        )
    ).step_run
    return automation, run, step_run


async def test_metric_point_is_precise_attributable_and_idempotent(
    session: AsyncSession,
) -> None:
    automation, run, step_run = await _scope(session)
    observed_at = datetime(
        2026,
        7,
        23,
        9,
        30,
        tzinfo=timezone(timedelta(hours=-3)),
    )
    first = await record_metric_point(
        session,
        automation=automation,
        run=run,
        step_run=step_run,
        idempotency_key="metric-tests:duration:1",
        name="step.duration",
        kind=MetricKind.DURATION,
        value="12.345",
        unit="s",
        source="task-runner",
        confidence="0.9900",
        dimensions={"queue": "cpu", "attempt": 1},
        observed_at=observed_at,
        now=datetime(2026, 7, 23, 12, 31, tzinfo=UTC),
    )
    replay = await record_metric_point(
        session,
        automation=automation,
        run=run,
        step_run=step_run,
        idempotency_key="metric-tests:duration:1",
        name="step.duration",
        kind=MetricKind.DURATION,
        value=Decimal("12.3450000000"),
        unit="s",
        source="task-runner",
        confidence=Decimal("0.99"),
        dimensions={"attempt": 1, "queue": "cpu"},
        observed_at=observed_at,
    )
    await session.commit()

    stored = await session.scalar(
        select(MetricPoint).where(MetricPoint.id == first.metric_point.id)
    )
    assert stored is not None
    assert first.created is True
    assert replay.created is False
    assert replay.metric_point.id == first.metric_point.id
    assert stored.value == Decimal("12.3450000000")
    assert stored.confidence == Decimal("0.9900")
    assert stored.observed_at == datetime(2026, 7, 23, 12, 30)
    assert stored.recorded_at == datetime(2026, 7, 23, 12, 31)
    assert stored.automation_id == automation.id
    assert stored.run_id == run.id
    assert stored.step_run_id == step_run.id


async def test_replay_without_observed_at_preserves_first_recording_time(
    session: AsyncSession,
) -> None:
    automation, _, _ = await _scope(session)
    first = await record_metric_point(
        session,
        automation=automation,
        idempotency_key="metric-tests:queue-depth",
        name="queue.depth",
        kind=MetricKind.GAUGE,
        value=3,
        unit="tasks",
        source="local-control-plane",
        now=datetime(2026, 7, 23, 12, 0, tzinfo=UTC),
    )
    replay = await record_metric_point(
        session,
        automation=automation,
        idempotency_key="metric-tests:queue-depth",
        name="queue.depth",
        kind=MetricKind.GAUGE,
        value="3",
        unit="tasks",
        source="local-control-plane",
        now=datetime(2026, 7, 23, 13, 0, tzinfo=UTC),
    )

    assert replay.created is False
    assert replay.metric_point.id == first.metric_point.id
    assert replay.metric_point.observed_at == datetime(2026, 7, 23, 12, 0)


async def test_sqlite_preserves_full_supported_decimal_precision(
    session: AsyncSession,
) -> None:
    automation, _, _ = await _scope(session)
    expected = Decimal("99999999999999999999.9999999999")
    creation = await record_metric_point(
        session,
        automation=automation,
        idempotency_key="metric-tests:maximum-precision",
        name="inventory.total",
        kind=MetricKind.GAUGE,
        value=expected,
        unit="items",
        source="precision-test",
        confidence="1",
    )
    metric_point_id = creation.metric_point.id
    await session.commit()
    session.expire_all()

    stored = await session.get(MetricPoint, metric_point_id)
    assert stored is not None
    assert stored.value == expected
    assert stored.confidence == Decimal("1.0000")


async def test_metric_idempotency_key_rejects_changed_inputs(
    session: AsyncSession,
) -> None:
    automation, _, _ = await _scope(session)
    await record_metric_point(
        session,
        automation=automation,
        idempotency_key="metric-tests:success-rate",
        name="runs.success-rate",
        kind=MetricKind.RATIO,
        value="0.75",
        unit="ratio",
        source="run-kernel",
    )

    with pytest.raises(IdempotencyConflictError, match="different inputs"):
        await record_metric_point(
            session,
            automation=automation,
            idempotency_key="metric-tests:success-rate",
            name="runs.success-rate",
            kind=MetricKind.RATIO,
            value="0.80",
            unit="ratio",
            source="run-kernel",
        )


async def test_metric_scope_rejects_cross_automation_and_cross_run_links(
    session: AsyncSession,
) -> None:
    first_automation, first_run, _ = await _scope(session, slug="metric-first")
    second_automation, second_run, second_step = await _scope(
        session,
        slug="metric-second",
    )

    with pytest.raises(MetricValidationError, match="does not belong to metric automation"):
        await record_metric_point(
            session,
            automation=first_automation,
            run=second_run,
            idempotency_key="metric-first:wrong-run",
            name="run.duration",
            kind=MetricKind.DURATION,
            value=1,
            unit="s",
            source="test",
        )
    with pytest.raises(MetricValidationError, match="does not belong to metric run"):
        await record_metric_point(
            session,
            automation=second_automation,
            run=second_run,
            step_run=(
                await get_or_create_step_run(
                    session,
                    run=first_run,
                    name="other-step",
                    queue=QueueClass.CPU,
                    ordinal=2,
                    idempotency_key="metric-first:other-step",
                )
            ).step_run,
            idempotency_key="metric-second:wrong-step",
            name="step.duration",
            kind=MetricKind.DURATION,
            value=1,
            unit="s",
            source="test",
        )
    with pytest.raises(MetricValidationError, match="requires its parent run"):
        await record_metric_point(
            session,
            automation=second_automation,
            step_run=second_step,
            idempotency_key="metric-second:missing-run",
            name="step.duration",
            kind=MetricKind.DURATION,
            value=1,
            unit="s",
            source="test",
        )


@pytest.mark.parametrize(
    ("kind", "value", "unit", "error"),
    [
        (MetricKind.COUNTER, "1.5", "runs", "must be an integer"),
        (MetricKind.DURATION, "-1", "s", "must not be negative"),
        (MetricKind.RATIO, "1.1", "ratio", "between 0 and 1"),
        (MetricKind.RATIO, "0.5", "percent", "'ratio' unit"),
        (MetricKind.CURRENCY, "10", "brl", "three-letter uppercase"),
        (MetricKind.GAUGE, "NaN", "items", "must be finite"),
        (MetricKind.GAUGE, "0.00000000001", "items", "10 decimal places"),
    ],
)
async def test_metric_semantics_reject_ambiguous_values(
    session: AsyncSession,
    kind: MetricKind,
    value: str,
    unit: str,
    error: str,
) -> None:
    automation, _, _ = await _scope(session)

    with pytest.raises(MetricValidationError, match=error):
        await record_metric_point(
            session,
            automation=automation,
            idempotency_key=f"metric-tests:invalid:{kind}:{value}",
            name="invalid.metric",
            kind=kind,
            value=value,
            unit=unit,
            source="test",
        )


async def test_database_rejects_ratio_outside_contract(session: AsyncSession) -> None:
    automation, _, _ = await _scope(session)
    session.add(
        MetricPoint(
            automation_id=automation.id,
            idempotency_key="metric-tests:invalid-db-ratio",
            name="invalid.ratio",
            kind=MetricKind.RATIO.value,
            value=Decimal("2"),
            unit="ratio",
            source="direct-test",
            confidence=None,
            dimensions={},
            observed_at=datetime(2026, 7, 23, 12, 0),
        )
    )

    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()
