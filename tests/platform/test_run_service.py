"""Persistence and failure-path tests for the first shared run lifecycle."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from src.core.database import Base
from src.platform.models import Automation, Run, RunStatus
from src.platform.run_service import (
    IdempotencyConflictError,
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
    result = await get_or_create_automation(
        session,
        slug="example",
        name="Example Automation",
        owner="local-owner",
    )
    return result.automation


async def test_automation_registration_is_idempotent(session: AsyncSession) -> None:
    first = await get_or_create_automation(
        session,
        slug="example",
        name="Example Automation",
        owner="local-owner",
    )
    second = await get_or_create_automation(
        session,
        slug="example",
        name="Example Automation",
        owner="local-owner",
    )

    assert first.created is True
    assert second.created is False
    assert second.automation.id == first.automation.id


async def test_run_creation_is_idempotent_and_records_queued_transition(
    session: AsyncSession,
) -> None:
    automation = await _automation(session)
    first = await get_or_create_run(
        session,
        automation=automation,
        idempotency_key="manual:example:001",
        input_payload={"topic": "baseline"},
    )
    second = await get_or_create_run(
        session,
        automation=automation,
        idempotency_key="manual:example:001",
        input_payload={"topic": "baseline"},
    )
    await session.commit()

    fetched = await session.scalar(
        select(Run)
        .where(Run.id == first.run.id)
        .options(selectinload(Run.transitions))
    )

    assert fetched is not None
    assert first.created is True
    assert second.created is False
    assert second.run.id == first.run.id
    assert fetched.status == RunStatus.QUEUED.value
    assert [(item.from_status, item.to_status) for item in fetched.transitions] == [
        (None, RunStatus.QUEUED.value)
    ]


async def test_successful_lifecycle_persists_every_transition(
    session: AsyncSession,
) -> None:
    automation = await _automation(session)
    creation = await get_or_create_run(
        session,
        automation=automation,
        idempotency_key="manual:example:success",
    )
    await transition_run(session, creation.run, RunStatus.RUNNING)
    await transition_run(
        session,
        creation.run,
        RunStatus.SUCCEEDED,
        output_payload={"artifact_count": 1},
    )
    await session.commit()

    fetched = await session.scalar(
        select(Run)
        .where(Run.id == creation.run.id)
        .options(selectinload(Run.transitions))
    )

    assert fetched is not None
    assert fetched.status == RunStatus.SUCCEEDED.value
    assert fetched.started_at is not None
    assert fetched.finished_at is not None
    assert fetched.output_payload == {"artifact_count": 1}
    assert [item.to_status for item in fetched.transitions] == [
        RunStatus.QUEUED.value,
        RunStatus.RUNNING.value,
        RunStatus.SUCCEEDED.value,
    ]


async def test_failed_run_requires_and_persists_structured_error(
    session: AsyncSession,
) -> None:
    automation = await _automation(session)
    creation = await get_or_create_run(
        session,
        automation=automation,
        idempotency_key="manual:example:failure",
    )
    await transition_run(session, creation.run, RunStatus.RUNNING)

    with pytest.raises(InvalidRunTransitionError, match="structured error"):
        await transition_run(session, creation.run, RunStatus.FAILED)

    error = {"code": "EXAMPLE_FAILURE", "retryable": False}
    await transition_run(session, creation.run, RunStatus.FAILED, error=error)
    await session.commit()

    assert creation.run.status == RunStatus.FAILED.value
    assert creation.run.error == error
    assert creation.run.finished_at is not None


async def test_terminal_run_rejects_additional_transition(session: AsyncSession) -> None:
    automation = await _automation(session)
    creation = await get_or_create_run(
        session,
        automation=automation,
        idempotency_key="manual:example:cancel",
    )
    await transition_run(session, creation.run, RunStatus.CANCELLED)

    with pytest.raises(InvalidRunTransitionError, match="cannot transition"):
        await transition_run(session, creation.run, RunStatus.RUNNING)

    assert creation.run.status == RunStatus.CANCELLED.value


async def test_reused_idempotency_key_with_different_input_is_rejected(
    session: AsyncSession,
) -> None:
    automation = await _automation(session)
    await get_or_create_run(
        session,
        automation=automation,
        idempotency_key="manual:example:conflict",
        input_payload={"value": 1},
    )

    with pytest.raises(IdempotencyConflictError, match="different inputs"):
        await get_or_create_run(
            session,
            automation=automation,
            idempotency_key="manual:example:conflict",
            input_payload={"value": 2},
        )
