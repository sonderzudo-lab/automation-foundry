"""Calendar, persistence, and safety tests for local automation schedules."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from src.core.database import Base
from src.platform.control_service import set_automation_kill_switch
from src.platform.models import (
    Automation,
    Schedule,
    ScheduleEventType,
    ScheduleStatus,
)
from src.platform.run_service import IdempotencyConflictError, get_or_create_automation
from src.platform.schedule_service import (
    ScheduleControlError,
    ScheduleValidationError,
    calculate_next_run_at,
    get_or_create_schedule,
    set_schedule_enabled,
)


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as database_session:
        yield database_session
    await engine.dispose()


async def _automation(session: AsyncSession) -> Automation:
    return (
        await get_or_create_automation(
            session,
            slug="schedule-tests",
            name="Schedule Tests",
            owner="local-owner",
        )
    ).automation


async def _schedule(session: AsyncSession, automation: Automation) -> Schedule:
    return (
        await get_or_create_schedule(
            session,
            automation=automation,
            name="daily-report",
            cron_expression="0 9 * * *",
            timezone_name="America/Sao_Paulo",
            actor="local-owner",
            reason="create test schedule",
            input_payload={"report": "daily"},
            now=datetime(2026, 7, 23, 11, 30, tzinfo=UTC),
        )
    ).schedule


def test_next_run_uses_schedule_timezone_and_returns_utc_database_value() -> None:
    next_run = calculate_next_run_at(
        "  0   9  * * * ",
        "America/Sao_Paulo",
        after=datetime(2026, 7, 23, 11, 30, tzinfo=UTC),
    )
    after_dst_change = calculate_next_run_at(
        "0 9 * * *",
        "America/New_York",
        after=datetime(2026, 3, 7, 15, 0, tzinfo=UTC),
    )

    assert next_run == datetime(2026, 7, 23, 12, 0)
    assert next_run.tzinfo is None
    assert after_dst_change == datetime(2026, 3, 8, 13, 0)


@pytest.mark.parametrize(
    ("expression", "timezone_name", "error"),
    [
        ("0 0 31 2 *", "UTC", "cron_expression is invalid"),
        ("0 0 * * * *", "UTC", "exactly five"),
        ("0 9 * * *", "Mars/Olympus_Mons", "IANA"),
    ],
)
def test_invalid_schedule_definitions_are_rejected(
    expression: str,
    timezone_name: str,
    error: str,
) -> None:
    with pytest.raises(ScheduleValidationError, match=error):
        calculate_next_run_at(
            expression,
            timezone_name,
            after=datetime(2026, 7, 23, tzinfo=UTC),
        )

    with pytest.raises(ValueError, match="timezone-aware"):
        calculate_next_run_at(
            "0 9 * * *",
            "UTC",
            after=datetime(2026, 7, 23),
        )


async def test_schedule_creation_is_disabled_audited_and_idempotent(
    session: AsyncSession,
) -> None:
    automation = await _automation(session)
    first = await get_or_create_schedule(
        session,
        automation=automation,
        name="daily-report",
        cron_expression=" 0  9 * * * ",
        timezone_name="America/Sao_Paulo",
        actor="local-owner",
        reason="manual registration",
        input_payload={"report": "daily"},
        allow_overlap=False,
        misfire_grace_seconds=120,
        now=datetime(2026, 7, 23, 11, 30, tzinfo=UTC),
    )
    replay = await get_or_create_schedule(
        session,
        automation=automation,
        name="daily-report",
        cron_expression="0 9 * * *",
        timezone_name="America/Sao_Paulo",
        actor="second-operator",
        reason="idempotent replay",
        input_payload={"report": "daily"},
        allow_overlap=False,
        misfire_grace_seconds=120,
        now=datetime(2026, 7, 24, 11, 30, tzinfo=UTC),
    )
    await session.commit()

    fetched = await session.scalar(
        select(Schedule)
        .where(Schedule.id == first.schedule.id)
        .options(selectinload(Schedule.events))
    )
    assert fetched is not None
    assert first.created is True
    assert replay.created is False
    assert replay.schedule.id == first.schedule.id
    assert fetched.status == ScheduleStatus.DISABLED.value
    assert fetched.next_run_at is None
    assert fetched.cron_expression == "0 9 * * *"
    assert fetched.timezone == "America/Sao_Paulo"
    assert fetched.input_payload == {"report": "daily"}
    assert len(fetched.events) == 1
    assert fetched.events[0].event_type == ScheduleEventType.CREATED.value
    assert fetched.events[0].actor == "local-owner"

    with pytest.raises(IdempotencyConflictError, match="different inputs"):
        await get_or_create_schedule(
            session,
            automation=automation,
            name="daily-report",
            cron_expression="0 10 * * *",
            timezone_name="America/Sao_Paulo",
            actor="local-owner",
            reason="conflicting definition",
        )


async def test_enable_and_disable_are_audited_and_idempotent(
    session: AsyncSession,
) -> None:
    automation = await _automation(session)
    schedule = await _schedule(session, automation)
    enabled = await set_schedule_enabled(
        session,
        schedule=schedule,
        enabled=True,
        actor="local-owner",
        reason="reviewed schedule",
        now=datetime(2026, 7, 23, 11, 30, tzinfo=UTC),
    )
    replay = await set_schedule_enabled(
        session,
        schedule=schedule,
        enabled=True,
        actor="local-owner",
        reason="duplicate click",
        now=datetime(2026, 7, 23, 11, 31, tzinfo=UTC),
    )
    disabled = await set_schedule_enabled(
        session,
        schedule=schedule,
        enabled=False,
        actor="local-owner",
        reason="pause recurring work",
        now=datetime(2026, 7, 23, 11, 32, tzinfo=UTC),
    )
    await session.commit()

    fetched = await session.scalar(
        select(Schedule)
        .where(Schedule.id == schedule.id)
        .options(selectinload(Schedule.events))
    )
    assert fetched is not None
    assert enabled.changed is True
    assert replay.changed is False
    assert disabled.changed is True
    assert fetched.status == ScheduleStatus.DISABLED.value
    assert fetched.next_run_at is None
    assert [event.event_type for event in fetched.events] == [
        ScheduleEventType.CREATED.value,
        ScheduleEventType.ENABLED.value,
        ScheduleEventType.DISABLED.value,
    ]
    assert fetched.events[1].next_run_at == datetime(2026, 7, 23, 12, 0)
    assert fetched.events[2].previous_next_run_at == datetime(2026, 7, 23, 12, 0)


async def test_schedule_cannot_enable_under_automation_safety_blocks(
    session: AsyncSession,
) -> None:
    automation = await _automation(session)
    schedule = await _schedule(session, automation)
    await set_automation_kill_switch(
        session,
        automation=automation,
        active=True,
        reason="maintenance",
    )

    with pytest.raises(ScheduleControlError, match="kill switch"):
        await set_schedule_enabled(
            session,
            schedule=schedule,
            enabled=True,
            actor="local-owner",
            reason="must remain blocked",
        )
    await set_automation_kill_switch(
        session,
        automation=automation,
        active=False,
        reason="maintenance complete",
    )
    automation.enabled = False
    await session.flush()

    with pytest.raises(ScheduleControlError, match="disabled automation"):
        await set_schedule_enabled(
            session,
            schedule=schedule,
            enabled=True,
            actor="local-owner",
            reason="must remain blocked",
        )
    assert schedule.status == ScheduleStatus.DISABLED.value
    assert schedule.next_run_at is None


async def test_database_rejects_enabled_schedule_without_next_run(
    session: AsyncSession,
) -> None:
    automation = await _automation(session)
    session.add(
        Schedule(
            automation_id=automation.id,
            name="invalid-enabled",
            cron_expression="0 9 * * *",
            timezone="UTC",
            input_payload={},
            status=ScheduleStatus.ENABLED.value,
            allow_overlap=False,
            misfire_grace_seconds=300,
            next_run_at=None,
        )
    )

    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()
