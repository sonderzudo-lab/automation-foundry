"""Tests for database-authoritative schedule ticks and durable publication."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.database import Base
from src.platform.models import (
    Automation,
    QueueClass,
    Run,
    RunDispatch,
    Schedule,
    ScheduleOccurrence,
)
from src.platform.run_service import get_or_create_automation
from src.platform.schedule_dispatcher import dispatch_due_schedules
from src.platform.schedule_service import get_or_create_schedule, set_schedule_enabled


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as database_session:
        yield database_session
    await engine.dispose()


async def _enabled_schedule(
    session: AsyncSession,
    *,
    grace: int = 300,
    allow_overlap: bool = False,
) -> tuple[Automation, Schedule]:
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
            name="every-minute",
            cron_expression="* * * * *",
            timezone_name="UTC",
            actor="operator",
            reason="test schedule",
            input_payload={"source": "schedule"},
            allow_overlap=allow_overlap,
            misfire_grace_seconds=grace,
            now=datetime(2026, 8, 10, 11, 59, 30, tzinfo=UTC),
        )
    ).schedule
    await set_schedule_enabled(
        session,
        schedule=schedule,
        enabled=True,
        actor="operator",
        reason="enable test",
        now=datetime(2026, 8, 10, 11, 59, 30, tzinfo=UTC),
    )
    await session.commit()
    return automation, schedule


async def test_due_tick_publishes_once_and_advances_calendar(
    session: AsyncSession,
) -> None:
    _automation, schedule = await _enabled_schedule(session)
    deliveries: list[tuple[int, str, QueueClass]] = []

    first = await dispatch_due_schedules(
        session,
        publish=lambda dispatch_id, delivery_id, queue: deliveries.append(
            (dispatch_id, delivery_id, queue)
        ),
        now=datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
    )
    replay = await dispatch_due_schedules(
        session,
        publish=lambda *_args: pytest.fail("replay must not publish"),
        now=datetime(2026, 8, 10, 12, 0, 30, tzinfo=UTC),
    )

    occurrence = await session.scalar(select(ScheduleOccurrence))
    run = await session.scalar(select(Run))
    dispatch = await session.scalar(select(RunDispatch))
    await session.refresh(schedule)
    assert (
        first.due_seen,
        first.prepared,
        first.skipped,
        first.published,
        first.publish_failed,
    ) == (1, 1, 0, 1, 0)
    assert replay.due_seen == 0
    assert len(deliveries) == 1
    assert occurrence is not None and occurrence.status == "published"
    assert occurrence.scheduled_for == datetime(2026, 8, 10, 12, 0)
    assert run is not None and run.trigger == "schedule"
    assert run.input_payload == {"operation": "noop", "source": "schedule"}
    assert dispatch is not None and dispatch.status == "published"
    assert schedule.next_run_at == datetime(2026, 8, 10, 12, 1)


async def test_broker_failure_keeps_pending_occurrence_for_next_tick(
    session: AsyncSession,
) -> None:
    _automation, _schedule = await _enabled_schedule(session)

    def fail(*_args: object) -> None:
        raise ConnectionError("broker unavailable")

    failed = await dispatch_due_schedules(
        session,
        publish=fail,
        now=datetime(2026, 8, 10, 12, 0, tzinfo=UTC),
    )
    delivered: list[str] = []
    recovered = await dispatch_due_schedules(
        session,
        publish=lambda _dispatch_id, delivery_id, _queue: delivered.append(delivery_id),
        now=datetime(2026, 8, 10, 12, 0, 10, tzinfo=UTC),
    )

    occurrence = await session.scalar(select(ScheduleOccurrence))
    dispatch = await session.scalar(select(RunDispatch))
    assert failed.publish_failed == 1
    assert recovered.published == 1
    assert len(delivered) == 1
    assert occurrence is not None and occurrence.status == "published"
    assert dispatch is not None and dispatch.publish_attempts == 2


async def test_misfire_and_overlap_are_skipped_with_evidence(
    session: AsyncSession,
) -> None:
    _automation, _schedule = await _enabled_schedule(session, grace=5)
    misfire = await dispatch_due_schedules(
        session,
        publish=lambda *_args: pytest.fail("misfire must not publish"),
        now=datetime(2026, 8, 10, 12, 0, 6, tzinfo=UTC),
    )
    assert misfire.skipped == 1

    # The next occurrence creates a queued run. Because no worker completes it,
    # the following minute is skipped by the no-overlap policy.
    schedule = await session.scalar(select(Schedule))
    assert schedule is not None
    schedule.misfire_grace_seconds = 300
    await session.commit()
    prepared = await dispatch_due_schedules(
        session,
        publish=lambda *_args: None,
        now=datetime(2026, 8, 10, 12, 1, tzinfo=UTC),
    )
    overlap = await dispatch_due_schedules(
        session,
        publish=lambda *_args: pytest.fail("overlap must not publish"),
        now=datetime(2026, 8, 10, 12, 2, tzinfo=UTC),
    )

    occurrences = list((await session.scalars(select(ScheduleOccurrence))).all())
    assert prepared.prepared == 1
    assert overlap.skipped == 1
    assert [item.reason_code for item in occurrences] == [
        "MISFIRE_GRACE_EXCEEDED",
        None,
        "OVERLAP_BLOCKED",
    ]
