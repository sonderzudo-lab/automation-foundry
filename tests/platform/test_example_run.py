"""End-to-end service tests for the local smoke automation."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.database import Base
from src.platform.example_run import execute_example_run
from src.platform.models import RunTransition, StepRunTransition


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as database_session:
        yield database_session
    await engine.dispose()


async def test_example_run_succeeds_and_replay_adds_no_transitions(
    session: AsyncSession,
) -> None:
    first = await execute_example_run(session, idempotency_key="smoke-001")
    await session.commit()
    run_transition_count = await session.scalar(select(func.count(RunTransition.id)))
    step_transition_count = await session.scalar(
        select(func.count(StepRunTransition.id))
    )

    second = await execute_example_run(session, idempotency_key="smoke-001")
    await session.commit()

    assert first.created is True
    assert first.replayed is False
    assert first.run_status == "succeeded"
    assert first.step_status == "succeeded"
    assert second.run_id == first.run_id
    assert second.step_run_id == first.step_run_id
    assert second.created is False
    assert second.replayed is True
    assert await session.scalar(select(func.count(RunTransition.id))) == run_transition_count
    assert (
        await session.scalar(select(func.count(StepRunTransition.id)))
        == step_transition_count
    )
    assert run_transition_count == 3
    assert step_transition_count == 3
