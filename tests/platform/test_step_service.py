"""Persistence tests for durable step execution state."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from src.core.database import Base
from src.platform.models import QueueClass, Run, RunStatus, StepRun
from src.platform.run_service import (
    IdempotencyConflictError,
    InvalidRunTransitionError,
    get_or_create_automation,
    get_or_create_run,
    transition_run,
)
from src.platform.step_service import get_or_create_step_run, transition_step_run


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as database_session:
        yield database_session
    await engine.dispose()


async def _running_run(session: AsyncSession) -> Run:
    automation = (
        await get_or_create_automation(
            session,
            slug="steps",
            name="Step Tests",
            owner="local-owner",
        )
    ).automation
    run = (
        await get_or_create_run(
            session,
            automation=automation,
            idempotency_key="step-test",
        )
    ).run
    await transition_run(session, run, RunStatus.RUNNING)
    return run


async def test_step_lifecycle_persists_queue_and_all_transitions(
    session: AsyncSession,
) -> None:
    run = await _running_run(session)
    creation = await get_or_create_step_run(
        session,
        run=run,
        name="fetch",
        queue=QueueClass.IO,
        ordinal=1,
        idempotency_key="step-test:fetch",
        input_payload={"source": "local"},
    )
    await transition_step_run(session, creation.step_run, RunStatus.RUNNING)
    await transition_step_run(
        session,
        creation.step_run,
        RunStatus.SUCCEEDED,
        output_payload={"count": 0},
    )
    await session.commit()

    fetched = await session.scalar(
        select(StepRun)
        .where(StepRun.id == creation.step_run.id)
        .options(selectinload(StepRun.transitions))
    )
    assert fetched is not None
    assert fetched.queue == QueueClass.IO.value
    assert fetched.status == RunStatus.SUCCEEDED.value
    assert fetched.output_payload == {"count": 0}
    assert [transition.to_status for transition in fetched.transitions] == [
        RunStatus.QUEUED.value,
        RunStatus.RUNNING.value,
        RunStatus.SUCCEEDED.value,
    ]


async def test_step_creation_is_idempotent_but_rejects_changed_inputs(
    session: AsyncSession,
) -> None:
    run = await _running_run(session)
    first = await get_or_create_step_run(
        session,
        run=run,
        name="noop",
        queue=QueueClass.IO,
        ordinal=1,
        idempotency_key="step-test:noop",
    )
    second = await get_or_create_step_run(
        session,
        run=run,
        name="noop",
        queue=QueueClass.IO,
        ordinal=1,
        idempotency_key="step-test:noop",
    )
    assert first.created is True
    assert second.created is False
    assert second.step_run.id == first.step_run.id

    with pytest.raises(IdempotencyConflictError, match="different inputs"):
        await get_or_create_step_run(
            session,
            run=run,
            name="changed",
            queue=QueueClass.IO,
            ordinal=1,
            idempotency_key="step-test:noop",
        )


async def test_step_requires_running_parent_and_structured_failure(
    session: AsyncSession,
) -> None:
    automation = (
        await get_or_create_automation(
            session,
            slug="queued-parent",
            name="Queued Parent",
            owner="local-owner",
        )
    ).automation
    queued_run = (
        await get_or_create_run(
            session,
            automation=automation,
            idempotency_key="queued-parent",
        )
    ).run
    with pytest.raises(InvalidRunTransitionError, match="running run"):
        await get_or_create_step_run(
            session,
            run=queued_run,
            name="noop",
            queue=QueueClass.IO,
            ordinal=1,
            idempotency_key="queued-parent:noop",
        )

    run = await _running_run(session)
    step = (
        await get_or_create_step_run(
            session,
            run=run,
            name="failure",
            queue=QueueClass.CPU,
            ordinal=1,
            idempotency_key="step-test:failure",
        )
    ).step_run
    await transition_step_run(session, step, RunStatus.RUNNING)
    with pytest.raises(InvalidRunTransitionError, match="structured error"):
        await transition_step_run(session, step, RunStatus.FAILED)
    await transition_step_run(
        session,
        step,
        RunStatus.FAILED,
        error={"code": "EXPECTED", "retryable": False},
    )
    assert step.status == RunStatus.FAILED.value
