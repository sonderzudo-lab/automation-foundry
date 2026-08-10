"""Persistence and lifecycle tests for measurable experiments."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from src.core.database import Base
from src.platform.control_service import set_automation_kill_switch
from src.platform.experiment_service import (
    ExperimentControlError,
    get_or_create_experiment,
    transition_experiment,
)
from src.platform.models import (
    Automation,
    Experiment,
    ExperimentEventType,
    ExperimentStatus,
)
from src.platform.run_service import (
    IdempotencyConflictError,
    InvalidRunTransitionError,
    get_or_create_automation,
    get_or_create_run,
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
            slug="experiment-tests",
            name="Experiment Tests",
            owner="local-owner",
        )
    ).automation


async def _experiment(session: AsyncSession, automation: Automation) -> Experiment:
    return (
        await get_or_create_experiment(
            session,
            automation=automation,
            key="headline-v2",
            name="Headline V2",
            hypothesis="A clearer headline improves completion rate.",
            primary_metric="content.completion_rate",
            primary_metric_unit="ratio",
            control_variant="current",
            candidate_variant="clearer-v2",
            actor="local-owner",
            reason="register reviewed hypothesis",
        )
    ).experiment


async def test_experiment_creation_is_draft_audited_and_idempotent(
    session: AsyncSession,
) -> None:
    automation = await _automation(session)
    experiment = await _experiment(session, automation)
    replay = await _experiment(session, automation)
    await session.commit()
    fetched = await session.scalar(
        select(Experiment)
        .where(Experiment.id == experiment.id)
        .options(selectinload(Experiment.events))
    )

    assert fetched is not None
    assert replay.id == experiment.id
    assert fetched.status == ExperimentStatus.DRAFT.value
    assert len(fetched.events) == 1
    assert fetched.events[0].event_type == ExperimentEventType.CREATED.value
    assert fetched.events[0].actor == "local-owner"

    with pytest.raises(IdempotencyConflictError, match="different inputs"):
        await get_or_create_experiment(
            session,
            automation=automation,
            key="headline-v2",
            name="Changed definition",
            hypothesis="A clearer headline improves completion rate.",
            primary_metric="content.completion_rate",
            primary_metric_unit="ratio",
            control_variant="current",
            candidate_variant="clearer-v2",
            actor="local-owner",
            reason="must conflict",
        )


async def test_experiment_lifecycle_is_audited_and_terminal_is_immutable(
    session: AsyncSession,
) -> None:
    automation = await _automation(session)
    experiment = await _experiment(session, automation)
    await transition_experiment(
        session,
        experiment=experiment,
        target=ExperimentStatus.RUNNING,
        actor="local-owner",
        reason="start measurement",
    )
    await transition_experiment(
        session,
        experiment=experiment,
        target=ExperimentStatus.PAUSED,
        actor="local-owner",
        reason="pause data collection",
    )
    await transition_experiment(
        session,
        experiment=experiment,
        target=ExperimentStatus.RUNNING,
        actor="local-owner",
        reason="resume measurement",
    )
    await transition_experiment(
        session,
        experiment=experiment,
        target=ExperimentStatus.COMPLETED,
        actor="local-owner",
        reason="measurement complete",
    )
    replay = await transition_experiment(
        session,
        experiment=experiment,
        target=ExperimentStatus.COMPLETED,
        actor="local-owner",
        reason="duplicate click",
    )
    await session.commit()
    fetched = await session.scalar(
        select(Experiment)
        .where(Experiment.id == experiment.id)
        .options(selectinload(Experiment.events))
    )

    assert fetched is not None
    assert replay.changed is False
    assert fetched.started_at is not None
    assert fetched.ended_at is not None
    assert [event.event_type for event in fetched.events] == [
        ExperimentEventType.CREATED.value,
        ExperimentEventType.STARTED.value,
        ExperimentEventType.PAUSED.value,
        ExperimentEventType.RESUMED.value,
        ExperimentEventType.COMPLETED.value,
    ]
    with pytest.raises(ExperimentControlError, match="cannot transition"):
        await transition_experiment(
            session,
            experiment=experiment,
            target=ExperimentStatus.RUNNING,
            actor="local-owner",
            reason="terminal experiment must stay closed",
        )


async def test_running_experiment_can_attribute_run_and_safety_blocks_start(
    session: AsyncSession,
) -> None:
    automation = await _automation(session)
    experiment = await _experiment(session, automation)
    await set_automation_kill_switch(
        session,
        automation=automation,
        active=True,
        actor="local-owner",
        reason="maintenance",
    )
    with pytest.raises(ExperimentControlError, match="kill switch"):
        await transition_experiment(
            session,
            experiment=experiment,
            target=ExperimentStatus.RUNNING,
            actor="local-owner",
            reason="must remain blocked",
        )
    await set_automation_kill_switch(
        session,
        automation=automation,
        active=False,
        actor="local-owner",
        reason="maintenance complete",
    )
    await transition_experiment(
        session,
        experiment=experiment,
        target=ExperimentStatus.RUNNING,
        actor="local-owner",
        reason="start reviewed experiment",
    )
    run = await get_or_create_run(
        session,
        automation=automation,
        idempotency_key="experiment-run-001",
        experiment_id=experiment.id,
    )
    assert run.run.experiment_id == experiment.id

    await transition_experiment(
        session,
        experiment=experiment,
        target=ExperimentStatus.PAUSED,
        actor="local-owner",
        reason="pause collection",
    )
    with pytest.raises(InvalidRunTransitionError, match="must be running"):
        await get_or_create_run(
            session,
            automation=automation,
            idempotency_key="experiment-run-blocked",
            experiment_id=experiment.id,
        )
