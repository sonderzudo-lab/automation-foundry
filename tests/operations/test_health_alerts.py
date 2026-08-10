"""Durable health alert reconciliation lifecycle tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.config import Settings
from src.core.database import Base
from src.operations.health import HealthCheck, HealthReport, HealthStatus
from src.operations.health_alerts import reconcile_health_report
from src.platform.models import (
    Alert,
    AlertEvent,
    AlertEventType,
    AlertSeverity,
    AlertStatus,
    HealthCondition,
)


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as database_session:
        yield database_session
    await engine.dispose()


def _report(
    checked_at: datetime,
    status: HealthStatus,
    *,
    summary: str = "redacted probe summary",
    name: str = "redis",
) -> HealthReport:
    return HealthReport(
        status=status,
        checked_at=checked_at,
        checks=(HealthCheck(name, status, summary, {}),),
    )


async def test_persistent_failure_opens_deduplicates_and_resolves_alert(
    session: AsyncSession,
) -> None:
    configuration = Settings(
        _env_file=None,
        health_alert_failure_threshold=2,
        health_alert_recovery_threshold=2,
    )
    started_at = datetime(2026, 8, 10, 12, 0)

    first = await reconcile_health_report(
        session,
        _report(started_at, HealthStatus.FAIL),
        configuration=configuration,
    )
    second = await reconcile_health_report(
        session,
        _report(started_at + timedelta(minutes=1), HealthStatus.FAIL),
        configuration=configuration,
    )
    replay = await reconcile_health_report(
        session,
        _report(started_at + timedelta(minutes=1), HealthStatus.FAIL),
        configuration=configuration,
    )
    repeated = await reconcile_health_report(
        session,
        _report(started_at + timedelta(minutes=2), HealthStatus.FAIL),
        configuration=configuration,
    )

    alert = await session.scalar(select(Alert))
    assert alert is not None
    assert first.occurrences_recorded == 0
    assert second.occurrences_recorded == 1
    assert replay.replayed == 1 and replay.occurrences_recorded == 0
    assert repeated.occurrences_recorded == 1
    assert alert.status == AlertStatus.OPEN.value
    assert alert.severity == AlertSeverity.ERROR.value
    assert alert.occurrence_count == 2
    assert len(list((await session.scalars(select(AlertEvent))).all())) == 2

    first_pass = await reconcile_health_report(
        session,
        _report(started_at + timedelta(minutes=3), HealthStatus.PASS),
        configuration=configuration,
    )
    recovered = await reconcile_health_report(
        session,
        _report(started_at + timedelta(minutes=4), HealthStatus.PASS),
        configuration=configuration,
    )

    assert first_pass.alerts_resolved == 0
    assert recovered.alerts_resolved == 1
    assert alert.status == AlertStatus.RESOLVED.value
    events = list(
        (
            await session.scalars(
                select(AlertEvent).order_by(AlertEvent.occurred_at, AlertEvent.id)
            )
        ).all()
    )
    assert events[-1].event_type == AlertEventType.RESOLVED.value
    condition = await session.get(HealthCondition, "redis")
    assert condition is not None
    assert condition.consecutive_unhealthy == 0
    assert condition.consecutive_healthy == 2
    assert condition.first_unhealthy_at is None


async def test_recovered_alert_reopens_only_after_new_persistent_failure(
    session: AsyncSession,
) -> None:
    configuration = Settings(
        _env_file=None,
        health_alert_failure_threshold=2,
        health_alert_recovery_threshold=1,
    )
    started_at = datetime(2026, 8, 10, 13, 0)
    for offset, status in (
        (0, HealthStatus.DEGRADED),
        (1, HealthStatus.DEGRADED),
        (2, HealthStatus.PASS),
        (3, HealthStatus.FAIL),
    ):
        await reconcile_health_report(
            session,
            _report(started_at + timedelta(minutes=offset), status),
            configuration=configuration,
        )

    alert = await session.scalar(select(Alert))
    assert alert is not None
    assert alert.status == AlertStatus.RESOLVED.value
    assert alert.severity == AlertSeverity.WARNING.value

    reopened = await reconcile_health_report(
        session,
        _report(started_at + timedelta(minutes=4), HealthStatus.FAIL),
        configuration=configuration,
    )

    assert reopened.occurrences_recorded == 1
    assert alert.status == AlertStatus.OPEN.value
    assert alert.severity == AlertSeverity.ERROR.value
    assert alert.occurrence_count == 2


async def test_skip_is_inconclusive_and_probe_text_is_never_persisted(
    session: AsyncSession,
) -> None:
    configuration = Settings(
        _env_file=None,
        health_alert_failure_threshold=1,
        health_alert_recovery_threshold=1,
    )
    checked_at = datetime(2026, 8, 10, 14, 0)
    secret = "redis://user:secret@private-host"

    opened = await reconcile_health_report(
        session,
        _report(checked_at, HealthStatus.FAIL, summary=secret),
        configuration=configuration,
    )
    skipped = await reconcile_health_report(
        session,
        _report(checked_at + timedelta(minutes=1), HealthStatus.SKIP, summary=secret),
        configuration=configuration,
    )

    alert = await session.scalar(select(Alert))
    condition = await session.get(HealthCondition, "redis")
    assert alert is not None and condition is not None
    assert opened.occurrences_recorded == 1
    assert skipped.inconclusive == 1 and skipped.alerts_resolved == 0
    assert alert.status == AlertStatus.OPEN.value
    assert condition.consecutive_unhealthy == 1
    events = list((await session.scalars(select(AlertEvent))).all())
    persisted = repr(alert) + alert.summary + repr(
        [(event.actor, event.reason) for event in events]
    )
    assert "secret" not in persisted
    assert "private-host" not in persisted


async def test_unknown_health_check_is_ignored_without_persisting_its_text(
    session: AsyncSession,
) -> None:
    report = _report(
        datetime(2026, 8, 10, 15, 0),
        HealthStatus.FAIL,
        name="private-hostname",
        summary="private-secret",
    )

    result = await reconcile_health_report(session, report)

    assert result.ignored == 1
    assert await session.scalar(select(Alert)) is None
    assert await session.scalar(select(HealthCondition)) is None
