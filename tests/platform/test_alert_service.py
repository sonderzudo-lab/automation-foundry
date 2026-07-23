"""Lifecycle, deduplication, and attribution tests for platform alerts."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from src.core.database import Base
from src.platform.alert_service import (
    AlertTransitionError,
    AlertValidationError,
    StaleAlertOccurrenceError,
    record_alert_occurrence,
    transition_alert,
)
from src.platform.metric_service import record_metric_point
from src.platform.models import (
    Alert,
    AlertEvent,
    AlertEventType,
    AlertSeverity,
    AlertStatus,
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
    slug: str = "alert-tests",
) -> tuple[Automation, Run, StepRun, MetricPoint]:
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
            queue=QueueClass.IO,
            ordinal=1,
            idempotency_key=f"{slug}:step",
        )
    ).step_run
    metric_point = (
        await record_metric_point(
            session,
            automation=automation,
            run=run,
            step_run=step_run,
            idempotency_key=f"{slug}:metric",
            name="connector.freshness",
            kind=MetricKind.DURATION,
            value="3600",
            unit="s",
            source="connector-monitor",
            observed_at=datetime(2026, 7, 23, 12, 0, tzinfo=UTC),
            now=datetime(2026, 7, 23, 12, 0, tzinfo=UTC),
        )
    ).metric_point
    return automation, run, step_run, metric_point


async def test_alert_occurrence_is_scoped_audited_and_idempotent(
    session: AsyncSession,
) -> None:
    automation, run, step_run, metric_point = await _scope(session)
    first = await record_alert_occurrence(
        session,
        automation=automation,
        metric_point=metric_point,
        deduplication_key="connector:youtube:freshness",
        idempotency_key="connector:youtube:freshness:20260723T1205",
        title="Connector data is stale",
        summary="Last successful observation exceeded the local threshold.",
        severity=AlertSeverity.WARNING,
        source="connector-monitor",
        observed_at=datetime(2026, 7, 23, 12, 5, tzinfo=UTC),
        now=datetime(2026, 7, 23, 12, 6, tzinfo=UTC),
    )
    replay = await record_alert_occurrence(
        session,
        automation=automation,
        metric_point=metric_point,
        deduplication_key="connector:youtube:freshness",
        idempotency_key="connector:youtube:freshness:20260723T1205",
        title="Connector data is stale",
        summary="Last successful observation exceeded the local threshold.",
        severity=AlertSeverity.WARNING,
        source="connector-monitor",
        observed_at=datetime(2026, 7, 23, 12, 5, tzinfo=UTC),
        now=datetime(2026, 7, 23, 13, 0, tzinfo=UTC),
    )
    await session.commit()

    stored = await session.scalar(
        select(Alert)
        .where(Alert.id == first.alert.id)
        .options(selectinload(Alert.events))
    )
    assert stored is not None
    assert first.created is True
    assert first.recorded is True
    assert replay.created is False
    assert replay.recorded is False
    assert replay.event.id == first.event.id
    assert stored.run_id == run.id
    assert stored.step_run_id == step_run.id
    assert stored.metric_point_id == metric_point.id
    assert stored.status == AlertStatus.OPEN.value
    assert stored.occurrence_count == 1
    assert stored.first_seen_at == datetime(2026, 7, 23, 12, 5)
    assert stored.last_seen_at == datetime(2026, 7, 23, 12, 5)
    assert [event.event_type for event in stored.events] == [
        AlertEventType.OPENED.value
    ]


async def test_open_occurrences_escalate_without_replacing_newer_summary(
    session: AsyncSession,
) -> None:
    automation, _, _, _ = await _scope(session)
    opened = await record_alert_occurrence(
        session,
        automation=automation,
        deduplication_key="disk:free-space",
        idempotency_key="disk:free-space:1",
        title="Disk space is low",
        summary="Free space fell below 15 percent.",
        severity=AlertSeverity.WARNING,
        source="machine-health",
        observed_at=datetime(2026, 7, 23, 12, 0, tzinfo=UTC),
        now=datetime(2026, 7, 23, 12, 0, tzinfo=UTC),
    )
    await record_alert_occurrence(
        session,
        automation=automation,
        deduplication_key="disk:free-space",
        idempotency_key="disk:free-space:2",
        title="Disk space is low",
        summary="Free space fell below 5 percent.",
        severity=AlertSeverity.CRITICAL,
        source="machine-health",
        observed_at=datetime(2026, 7, 23, 12, 10, tzinfo=UTC),
        now=datetime(2026, 7, 23, 12, 10, tzinfo=UTC),
    )
    delayed = await record_alert_occurrence(
        session,
        automation=automation,
        deduplication_key="disk:free-space",
        idempotency_key="disk:free-space:delayed",
        title="Disk space is low",
        summary="Delayed warning from the earlier polling cycle.",
        severity=AlertSeverity.INFO,
        source="machine-health",
        observed_at=datetime(2026, 7, 23, 11, 50, tzinfo=UTC),
        now=datetime(2026, 7, 23, 12, 11, tzinfo=UTC),
    )

    assert delayed.alert.id == opened.alert.id
    assert delayed.event.event_type == AlertEventType.OCCURRED.value
    assert delayed.alert.severity == AlertSeverity.CRITICAL.value
    assert delayed.alert.summary == "Free space fell below 5 percent."
    assert delayed.alert.occurrence_count == 3
    assert delayed.alert.first_seen_at == datetime(2026, 7, 23, 11, 50)
    assert delayed.alert.last_seen_at == datetime(2026, 7, 23, 12, 10)


async def test_acknowledge_resolve_and_new_occurrence_reopen_with_audit(
    session: AsyncSession,
) -> None:
    automation, _, _, _ = await _scope(session)
    opened = await record_alert_occurrence(
        session,
        automation=automation,
        deduplication_key="worker:io:offline",
        idempotency_key="worker:io:offline:1",
        title="IO worker is offline",
        summary="No heartbeat was observed.",
        severity=AlertSeverity.ERROR,
        source="worker-health",
        observed_at=datetime(2026, 7, 23, 12, 0, tzinfo=UTC),
        now=datetime(2026, 7, 23, 12, 0, tzinfo=UTC),
    )
    acknowledged = await transition_alert(
        session,
        alert=opened.alert,
        target=AlertStatus.ACKNOWLEDGED,
        actor="local-owner",
        reason="Investigating the worker process.",
        now=datetime(2026, 7, 23, 12, 1, tzinfo=UTC),
    )
    acknowledgement_replay = await transition_alert(
        session,
        alert=opened.alert,
        target=AlertStatus.ACKNOWLEDGED,
        actor="second-click",
        reason="Duplicate command.",
        now=datetime(2026, 7, 23, 12, 1, tzinfo=UTC),
    )
    resolved = await transition_alert(
        session,
        alert=opened.alert,
        target=AlertStatus.RESOLVED,
        actor="local-owner",
        reason="Worker heartbeat recovered.",
        now=datetime(2026, 7, 23, 12, 2, tzinfo=UTC),
    )
    resolution_replay = await transition_alert(
        session,
        alert=opened.alert,
        target=AlertStatus.RESOLVED,
        actor="second-click",
        reason="Duplicate command.",
        now=datetime(2026, 7, 23, 12, 2, tzinfo=UTC),
    )
    reopened = await record_alert_occurrence(
        session,
        automation=automation,
        deduplication_key="worker:io:offline",
        idempotency_key="worker:io:offline:2",
        title="IO worker is offline",
        summary="Heartbeat stopped again after recovery.",
        severity=AlertSeverity.WARNING,
        source="worker-health",
        observed_at=datetime(2026, 7, 23, 12, 3, tzinfo=UTC),
        now=datetime(2026, 7, 23, 12, 3, tzinfo=UTC),
    )
    await session.commit()

    stored = await session.scalar(
        select(Alert)
        .where(Alert.id == opened.alert.id)
        .options(selectinload(Alert.events))
    )
    assert stored is not None
    assert acknowledged.changed is True
    assert acknowledgement_replay.changed is False
    assert resolved.changed is True
    assert resolution_replay.changed is False
    assert reopened.event.event_type == AlertEventType.REOPENED.value
    assert stored.status == AlertStatus.OPEN.value
    assert stored.severity == AlertSeverity.WARNING.value
    assert stored.acknowledged_at is None
    assert stored.acknowledged_by is None
    assert stored.acknowledgement_reason is None
    assert stored.resolved_at is None
    assert stored.resolved_by is None
    assert stored.resolution_reason is None
    assert [event.event_type for event in stored.events] == [
        AlertEventType.OPENED.value,
        AlertEventType.ACKNOWLEDGED.value,
        AlertEventType.RESOLVED.value,
        AlertEventType.REOPENED.value,
    ]


async def test_stale_occurrence_cannot_reopen_acknowledged_alert(
    session: AsyncSession,
) -> None:
    automation, _, _, _ = await _scope(session)
    opened = await record_alert_occurrence(
        session,
        automation=automation,
        deduplication_key="redis:offline",
        idempotency_key="redis:offline:1",
        title="Redis is offline",
        summary="The local broker did not accept a connection.",
        severity=AlertSeverity.ERROR,
        source="service-health",
        observed_at=datetime(2026, 7, 23, 12, 0, tzinfo=UTC),
        now=datetime(2026, 7, 23, 12, 0, tzinfo=UTC),
    )
    await transition_alert(
        session,
        alert=opened.alert,
        target=AlertStatus.ACKNOWLEDGED,
        actor="local-owner",
        reason="Known maintenance window.",
        now=datetime(2026, 7, 23, 12, 2, tzinfo=UTC),
    )

    with pytest.raises(StaleAlertOccurrenceError, match="predates"):
        await record_alert_occurrence(
            session,
            automation=automation,
            deduplication_key="redis:offline",
            idempotency_key="redis:offline:delayed",
            title="Redis is offline",
            summary="Delayed failure from before acknowledgement.",
            severity=AlertSeverity.ERROR,
            source="service-health",
            observed_at=datetime(2026, 7, 23, 12, 1, tzinfo=UTC),
            now=datetime(2026, 7, 23, 12, 3, tzinfo=UTC),
        )
    assert opened.alert.status == AlertStatus.ACKNOWLEDGED.value
    assert opened.alert.occurrence_count == 1


async def test_alert_occurrence_keys_reject_changed_inputs_and_attribution(
    session: AsyncSession,
) -> None:
    automation, run, _, _ = await _scope(session)
    await record_alert_occurrence(
        session,
        automation=automation,
        run=run,
        deduplication_key="run:failed",
        idempotency_key="run:failed:1",
        title="Run failed",
        summary="The run entered a failed state.",
        severity=AlertSeverity.ERROR,
        source="run-kernel",
    )

    with pytest.raises(IdempotencyConflictError, match="different inputs"):
        await record_alert_occurrence(
            session,
            automation=automation,
            run=run,
            deduplication_key="run:failed",
            idempotency_key="run:failed:1",
            title="Run failed",
            summary="Changed summary for the same occurrence.",
            severity=AlertSeverity.ERROR,
            source="run-kernel",
        )
    with pytest.raises(IdempotencyConflictError, match="different attribution"):
        await record_alert_occurrence(
            session,
            automation=automation,
            deduplication_key="run:failed",
            idempotency_key="run:failed:2",
            title="Run failed",
            summary="Missing the immutable run attribution.",
            severity=AlertSeverity.ERROR,
            source="run-kernel",
        )


async def test_alert_scope_rejects_cross_automation_links(
    session: AsyncSession,
) -> None:
    first_automation, _, _, _ = await _scope(session, slug="alert-first")
    second_automation, second_run, second_step, second_metric = await _scope(
        session,
        slug="alert-second",
    )
    other_second_run = (
        await get_or_create_run(
            session,
            automation=second_automation,
            idempotency_key="alert-second:other-run",
        )
    ).run
    await transition_run(session, other_second_run, RunStatus.RUNNING)

    with pytest.raises(AlertValidationError, match="does not belong to alert automation"):
        await record_alert_occurrence(
            session,
            automation=first_automation,
            run=second_run,
            deduplication_key="wrong-run",
            idempotency_key="wrong-run:1",
            title="Wrong run",
            summary="Cross-automation attribution must fail.",
            severity=AlertSeverity.ERROR,
            source="test",
        )
    with pytest.raises(AlertValidationError, match="requires its parent run"):
        await record_alert_occurrence(
            session,
            automation=second_automation,
            step_run=second_step,
            deduplication_key="missing-run",
            idempotency_key="missing-run:1",
            title="Missing run",
            summary="A step requires its explicit parent run.",
            severity=AlertSeverity.ERROR,
            source="test",
        )
    with pytest.raises(AlertValidationError, match="does not belong to alert run"):
        await record_alert_occurrence(
            session,
            automation=second_automation,
            run=other_second_run,
            metric_point=second_metric,
            deduplication_key="wrong-metric-run",
            idempotency_key="wrong-metric-run:1",
            title="Wrong metric run",
            summary="Metric attribution must match the run.",
            severity=AlertSeverity.ERROR,
            source="test",
        )


async def test_invalid_alert_transitions_are_rejected(session: AsyncSession) -> None:
    automation, _, _, _ = await _scope(session)
    with pytest.raises(AlertValidationError, match="later than the recording time"):
        await record_alert_occurrence(
            session,
            automation=automation,
            deduplication_key="future:occurrence",
            idempotency_key="future:occurrence:1",
            title="Future occurrence",
            summary="A future timestamp would make lifecycle ordering ambiguous.",
            severity=AlertSeverity.INFO,
            source="test",
            observed_at=datetime(2026, 7, 23, 12, 1, tzinfo=UTC),
            now=datetime(2026, 7, 23, 12, 0, tzinfo=UTC),
        )
    opened = await record_alert_occurrence(
        session,
        automation=automation,
        deduplication_key="ollama:offline",
        idempotency_key="ollama:offline:1",
        title="Ollama is offline",
        summary="The local endpoint did not respond.",
        severity=AlertSeverity.WARNING,
        source="service-health",
        observed_at=datetime(2026, 7, 23, 12, 0, tzinfo=UTC),
        now=datetime(2026, 7, 23, 12, 0, tzinfo=UTC),
    )

    with pytest.raises(AlertTransitionError, match="predate"):
        await transition_alert(
            session,
            alert=opened.alert,
            target=AlertStatus.ACKNOWLEDGED,
            actor="local-owner",
            reason="Impossible timestamp.",
            now=datetime(2026, 7, 23, 11, 59, tzinfo=UTC),
        )
    with pytest.raises(AlertTransitionError, match="reopen only"):
        await transition_alert(
            session,
            alert=opened.alert,
            target=AlertStatus.OPEN,
            actor="local-owner",
            reason="Manual reopen is forbidden.",
        )
    await transition_alert(
        session,
        alert=opened.alert,
        target=AlertStatus.RESOLVED,
        actor="local-owner",
        reason="Endpoint recovered.",
        now=datetime(2026, 7, 23, 12, 1, tzinfo=UTC),
    )
    with pytest.raises(AlertTransitionError, match="resolved alert"):
        await transition_alert(
            session,
            alert=opened.alert,
            target=AlertStatus.ACKNOWLEDGED,
            actor="local-owner",
            reason="Cannot acknowledge after resolution.",
        )


async def test_database_rejects_incomplete_acknowledged_alert(
    session: AsyncSession,
) -> None:
    automation, _, _, _ = await _scope(session)
    session.add(
        Alert(
            automation_id=automation.id,
            deduplication_key="invalid-acknowledged",
            title="Invalid acknowledged alert",
            summary="Missing acknowledgement metadata.",
            severity=AlertSeverity.WARNING.value,
            status=AlertStatus.ACKNOWLEDGED.value,
            source="direct-test",
            occurrence_count=1,
            first_seen_at=datetime(2026, 7, 23, 12, 0),
            last_seen_at=datetime(2026, 7, 23, 12, 0),
        )
    )

    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_database_rejects_invalid_alert_event_transition(
    session: AsyncSession,
) -> None:
    automation, _, _, _ = await _scope(session)
    opened = await record_alert_occurrence(
        session,
        automation=automation,
        deduplication_key="invalid-event",
        idempotency_key="invalid-event:1",
        title="Valid alert",
        summary="The alert itself is valid.",
        severity=AlertSeverity.INFO,
        source="direct-test",
    )
    session.add(
        AlertEvent(
            alert_id=opened.alert.id,
            idempotency_key=None,
            event_type=AlertEventType.ACKNOWLEDGED.value,
            from_status=AlertStatus.RESOLVED.value,
            to_status=AlertStatus.ACKNOWLEDGED.value,
            severity=AlertSeverity.INFO.value,
            actor="direct-test",
            reason="Invalid transition.",
        )
    )

    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()
