"""Persistence and safety tests for cancellation and kill-switch controls."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from src.core.database import Base
from src.platform.control_service import (
    get_execution_control_state,
    request_run_cancellation,
    set_automation_enabled,
    set_automation_kill_switch,
)
from src.platform.models import (
    Automation,
    ControlEventType,
    Run,
    RunStatus,
)
from src.platform.run_service import (
    InvalidRunTransitionError,
    get_or_create_automation,
    get_or_create_run,
    transition_run,
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
            slug="controlled",
            name="Controlled Automation",
            owner="local-owner",
        )
    ).automation


async def _run(session: AsyncSession, automation: Automation, key: str = "run-1") -> Run:
    return (
        await get_or_create_run(
            session,
            automation=automation,
            idempotency_key=key,
        )
    ).run


async def test_kill_switch_changes_are_idempotent_and_audited(
    session: AsyncSession,
) -> None:
    automation = await _automation(session)
    enabled = await set_automation_kill_switch(
        session,
        automation=automation,
        active=True,
        actor="local-owner",
        reason="operator safety stop",
    )
    duplicate = await set_automation_kill_switch(
        session,
        automation=automation,
        active=True,
        actor="local-owner",
        reason="duplicate request",
    )
    disabled = await set_automation_kill_switch(
        session,
        automation=automation,
        active=False,
        actor="local-owner",
        reason="manual review completed",
    )
    await session.commit()

    fetched = await session.scalar(
        select(Automation)
        .where(Automation.id == automation.id)
        .options(selectinload(Automation.control_events))
    )
    assert fetched is not None
    assert enabled.changed is True
    assert duplicate.changed is False
    assert duplicate.event is None
    assert disabled.changed is True
    assert fetched.kill_switch_active is False
    assert fetched.kill_switch_reason == "manual review completed"
    assert fetched.kill_switch_changed_at is not None
    assert [event.event_type for event in fetched.control_events] == [
        ControlEventType.KILL_SWITCH_ENABLED.value,
        ControlEventType.KILL_SWITCH_DISABLED.value,
    ]
    assert [event.actor for event in fetched.control_events] == [
        "local-owner",
        "local-owner",
    ]


async def test_automation_enabled_changes_are_idempotent_and_audited(
    session: AsyncSession,
) -> None:
    automation = await _automation(session)
    disabled = await set_automation_enabled(
        session,
        automation=automation,
        enabled=False,
        actor="local-owner",
        reason="pause new intake",
    )
    duplicate = await set_automation_enabled(
        session,
        automation=automation,
        enabled=False,
        actor="different-actor",
        reason="duplicate must not append evidence",
    )
    enabled = await set_automation_enabled(
        session,
        automation=automation,
        enabled=True,
        actor="local-owner",
        reason="resume reviewed intake",
    )
    await session.commit()

    fetched = await session.scalar(
        select(Automation)
        .where(Automation.id == automation.id)
        .options(selectinload(Automation.control_events))
    )
    assert fetched is not None
    assert disabled.changed is True
    assert duplicate.changed is False
    assert duplicate.event is None
    assert enabled.changed is True
    assert fetched.enabled is True
    assert [event.event_type for event in fetched.control_events] == [
        ControlEventType.AUTOMATION_DISABLED.value,
        ControlEventType.AUTOMATION_ENABLED.value,
    ]
    assert [event.actor for event in fetched.control_events] == [
        "local-owner",
        "local-owner",
    ]


async def test_disabled_automation_blocks_new_and_queued_runs_but_not_active_run(
    session: AsyncSession,
) -> None:
    automation = await _automation(session)
    queued = await _run(session, automation)
    await set_automation_enabled(
        session,
        automation=automation,
        enabled=False,
        actor="local-owner",
        reason="administrative pause",
    )

    with pytest.raises(InvalidRunTransitionError, match="administratively disabled"):
        await get_or_create_run(
            session,
            automation=automation,
            idempotency_key="blocked-new-run",
        )
    with pytest.raises(InvalidRunTransitionError, match="administratively disabled"):
        await transition_run(session, queued, RunStatus.RUNNING)

    await set_automation_enabled(
        session,
        automation=automation,
        enabled=True,
        actor="local-owner",
        reason="allow reviewed work",
    )
    await transition_run(session, queued, RunStatus.RUNNING)
    await set_automation_enabled(
        session,
        automation=automation,
        enabled=False,
        actor="local-owner",
        reason="pause future intake only",
    )
    await transition_run(session, queued, RunStatus.SUCCEEDED)
    await session.commit()

    assert queued.status == RunStatus.SUCCEEDED.value
    blocked = await session.scalar(
        select(Run).where(Run.idempotency_key == "blocked-new-run")
    )
    assert blocked is None


async def test_cancellation_request_is_idempotent_audited_and_blocks_start(
    session: AsyncSession,
) -> None:
    automation = await _automation(session)
    run = await _run(session, automation)
    first = await request_run_cancellation(
        session,
        run=run,
        reason="operator cancelled queued work",
    )
    duplicate = await request_run_cancellation(
        session,
        run=run,
        reason="duplicate request",
    )
    await session.commit()

    fetched = await session.scalar(
        select(Run)
        .where(Run.id == run.id)
        .options(
            selectinload(Run.control_events),
            selectinload(Run.transitions),
        )
    )
    assert fetched is not None
    assert first.changed is True
    assert duplicate.changed is False
    assert fetched.cancellation_requested_at is not None
    assert fetched.cancellation_reason == "operator cancelled queued work"
    assert fetched.status == RunStatus.CANCELLED.value
    assert [event.event_type for event in fetched.control_events] == [
        ControlEventType.CANCELLATION_REQUESTED.value
    ]
    assert [transition.to_status for transition in fetched.transitions] == [
        RunStatus.QUEUED.value,
        RunStatus.CANCELLED.value,
    ]


async def test_kill_switch_blocks_start_and_control_state_reads_both_sources(
    session: AsyncSession,
) -> None:
    automation = await _automation(session)
    run = await _run(session, automation)
    await set_automation_kill_switch(
        session,
        automation=automation,
        active=True,
        actor="local-owner",
        reason="maintenance",
    )
    await session.commit()

    state = await get_execution_control_state(session, run_id=run.id)
    assert state.kill_switch_active is True
    assert state.cancellation_requested is False
    assert state.blocked is True
    assert state.code == "AUTOMATION_KILL_SWITCH"
    with pytest.raises(InvalidRunTransitionError, match="kill switch"):
        await transition_run(session, run, RunStatus.RUNNING)


async def test_terminal_run_rejects_cancellation_request(session: AsyncSession) -> None:
    automation = await _automation(session)
    run = await _run(session, automation)
    await transition_run(session, run, RunStatus.RUNNING)
    await transition_run(session, run, RunStatus.SUCCEEDED)

    with pytest.raises(InvalidRunTransitionError, match="terminal run"):
        await request_run_cancellation(
            session,
            run=run,
            reason="too late",
        )
