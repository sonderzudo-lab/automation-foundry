"""Behavior tests for the single-process durable task wrapper."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from src.core.database import Base
from src.platform.models import QueueClass, Run, RunStatus, StepRun
from src.platform.run_service import (
    get_or_create_automation,
    get_or_create_run,
    transition_run,
)
from src.platform.task_runner import (
    PermanentTaskError,
    RetryableTaskError,
    RetryPolicy,
    TaskAttemptContext,
    TaskStepSpec,
    execute_task_step,
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


async def _running_run(session: AsyncSession) -> Run:
    automation = (
        await get_or_create_automation(
            session,
            slug="task-runner",
            name="Task Runner Tests",
            owner="local-owner",
        )
    ).automation
    run = (
        await get_or_create_run(
            session,
            automation=automation,
            idempotency_key="task-runner:test",
        )
    ).run
    await transition_run(session, run, RunStatus.RUNNING)
    return run


def _spec() -> TaskStepSpec:
    return TaskStepSpec(
        name="local-task",
        queue=QueueClass.IO,
        ordinal=1,
        idempotency_key="task-runner:test:local-task",
        input_payload={"safe": True},
    )


async def _attempts(session: AsyncSession) -> list[StepRun]:
    return list(
        (
            await session.scalars(
                select(StepRun)
                .order_by(StepRun.attempt)
                .options(selectinload(StepRun.transitions))
            )
        ).all()
    )


async def test_success_is_persisted_and_replay_does_not_call_operation(
    session: AsyncSession,
) -> None:
    run = await _running_run(session)
    calls = 0

    async def operation(context: TaskAttemptContext) -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"attempt": context.attempt}

    first = await execute_task_step(
        session,
        run=run,
        spec=_spec(),
        policy=RetryPolicy(),
        operation=operation,
    )
    second = await execute_task_step(
        session,
        run=run,
        spec=_spec(),
        policy=RetryPolicy(),
        operation=operation,
    )

    assert first.status is RunStatus.SUCCEEDED
    assert first.output_payload == {"attempt": 1}
    assert first.replayed is False
    assert second.replayed is True
    assert second.step_run_ids == first.step_run_ids
    assert calls == 1
    assert [item.to_status for item in (await _attempts(session))[0].transitions] == [
        RunStatus.QUEUED.value,
        RunStatus.RUNNING.value,
        RunStatus.SUCCEEDED.value,
    ]


async def test_retryable_failures_use_capped_exponential_backoff(
    session: AsyncSession,
) -> None:
    run = await _running_run(session)
    delays: list[float] = []

    async def operation(context: TaskAttemptContext) -> dict[str, int]:
        if context.attempt < 3:
            raise RetryableTaskError("TEMPORARY_LOCAL_FAILURE")
        return {"attempt": context.attempt}

    async def sleep(delay: float) -> None:
        delays.append(delay)

    result = await execute_task_step(
        session,
        run=run,
        spec=_spec(),
        policy=RetryPolicy(
            max_attempts=3,
            initial_backoff_seconds=0.5,
            backoff_multiplier=3,
            max_backoff_seconds=1,
        ),
        operation=operation,
        sleep=sleep,
    )
    attempts = await _attempts(session)

    assert result.status is RunStatus.SUCCEEDED
    assert result.attempt_count == 3
    assert delays == [0.5, 1]
    assert [attempt.status for attempt in attempts] == [
        RunStatus.FAILED.value,
        RunStatus.FAILED.value,
        RunStatus.SUCCEEDED.value,
    ]
    assert attempts[0].error == {
        "code": "TEMPORARY_LOCAL_FAILURE",
        "exception_type": "RetryableTaskError",
        "retryable": True,
        "timed_out": False,
    }


async def test_failed_attempt_is_committed_before_backoff(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "durable-retry.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
        echo=False,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    observed_during_backoff = False

    async with factory() as worker_session:
        run = await _running_run(worker_session)

        async def operation(context: TaskAttemptContext) -> dict[str, int]:
            if context.attempt == 1:
                raise RetryableTaskError("FIRST_ATTEMPT_FAILED")
            return {"attempt": context.attempt}

        async def inspect_durable_state(_delay: float) -> None:
            nonlocal observed_during_backoff
            async with factory() as observer_session:
                failed_attempt = await observer_session.scalar(
                    select(StepRun).where(StepRun.attempt == 1)
                )
                observed_during_backoff = (
                    failed_attempt is not None
                    and failed_attempt.status == RunStatus.FAILED.value
                )

        result = await execute_task_step(
            worker_session,
            run=run,
            spec=_spec(),
            policy=RetryPolicy(max_attempts=2),
            operation=operation,
            sleep=inspect_durable_state,
        )

    await engine.dispose()
    assert observed_during_backoff is True
    assert result.status is RunStatus.SUCCEEDED


async def test_timeout_is_bounded_retried_and_persisted(
    session: AsyncSession,
) -> None:
    run = await _running_run(session)

    async def operation(_context: TaskAttemptContext) -> dict[str, object]:
        await asyncio.Event().wait()
        return {}

    async def no_sleep(_delay: float) -> None:
        return None

    result = await execute_task_step(
        session,
        run=run,
        spec=_spec(),
        policy=RetryPolicy(
            max_attempts=2,
            timeout_seconds=0.01,
            initial_backoff_seconds=0,
        ),
        operation=operation,
        sleep=no_sleep,
    )

    assert result.status is RunStatus.FAILED
    assert result.attempt_count == 2
    assert result.error == {
        "code": "TASK_TIMEOUT",
        "exception_type": "TimeoutError",
        "retryable": True,
        "timed_out": True,
    }


@pytest.mark.parametrize(
    ("raised", "expected_code", "expected_type"),
    [
        (PermanentTaskError("INVALID_INPUT"), "INVALID_INPUT", "PermanentTaskError"),
        (ValueError("private detail"), "UNEXPECTED_TASK_ERROR", "ValueError"),
    ],
)
async def test_terminal_errors_are_not_retried_or_leaked(
    session: AsyncSession,
    raised: Exception,
    expected_code: str,
    expected_type: str,
) -> None:
    run = await _running_run(session)

    async def operation(_context: TaskAttemptContext) -> dict[str, object]:
        raise raised

    result = await execute_task_step(
        session,
        run=run,
        spec=_spec(),
        policy=RetryPolicy(max_attempts=3),
        operation=operation,
    )

    assert result.status is RunStatus.FAILED
    assert result.attempt_count == 1
    assert result.error is not None
    assert result.error["code"] == expected_code
    assert result.error["exception_type"] == expected_type
    assert result.error["retryable"] is False
    assert "private detail" not in str(result.error)


async def test_cancellation_between_attempts_stops_before_more_work(
    session: AsyncSession,
) -> None:
    run = await _running_run(session)
    cancelled = False
    operation_calls = 0

    async def operation(_context: TaskAttemptContext) -> dict[str, object]:
        nonlocal operation_calls
        operation_calls += 1
        raise RetryableTaskError("TRY_AGAIN")

    async def sleep(_delay: float) -> None:
        nonlocal cancelled
        cancelled = True

    async def cancellation_requested() -> bool:
        return cancelled

    result = await execute_task_step(
        session,
        run=run,
        spec=_spec(),
        policy=RetryPolicy(max_attempts=3, initial_backoff_seconds=0),
        operation=operation,
        cancellation_requested=cancellation_requested,
        sleep=sleep,
    )
    attempts = await _attempts(session)

    assert result.status is RunStatus.CANCELLED
    assert result.attempt_count == 2
    assert operation_calls == 1
    assert [attempt.status for attempt in attempts] == [
        RunStatus.FAILED.value,
        RunStatus.CANCELLED.value,
    ]
    assert [item.to_status for item in attempts[1].transitions] == [
        RunStatus.QUEUED.value,
        RunStatus.CANCELLED.value,
    ]


def test_retry_policy_rejects_unbounded_or_invalid_values() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError, match="timeout_seconds"):
        RetryPolicy(timeout_seconds=0)
    with pytest.raises(ValueError, match="backoff_multiplier"):
        RetryPolicy(backoff_multiplier=0.5)
