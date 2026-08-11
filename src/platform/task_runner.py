"""Single-process task execution with durable attempts and bounded retries."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.platform.approval_service import assert_approval_granted
from src.platform.control_service import get_execution_control_state
from src.platform.models import QueueClass, Run, RunStatus, StepRun
from src.platform.run_service import (
    IdempotencyConflictError,
    InvalidRunTransitionError,
    transition_run,
)
from src.platform.step_service import get_or_create_step_run, transition_step_run

TaskOperation = Callable[["TaskAttemptContext"], Awaitable[dict[str, Any]]]
CancellationProbe = Callable[[], Awaitable[bool]]
Sleeper = Callable[[float], Awaitable[None]]


class TaskOperationError(Exception):
    """Base error carrying a safe, persistable task failure code."""

    def __init__(self, code: str) -> None:
        normalized = code.strip()
        if not normalized:
            raise ValueError("task error code must not be empty")
        self.code = normalized
        super().__init__(normalized)


class RetryableTaskError(TaskOperationError):
    """An expected task failure eligible for the configured retry policy."""


class PermanentTaskError(TaskOperationError):
    """An expected task failure that must not be retried."""


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounded timeout and exponential-backoff policy for one logical step."""

    max_attempts: int = 3
    timeout_seconds: float = 30.0
    initial_backoff_seconds: float = 1.0
    backoff_multiplier: float = 2.0
    max_backoff_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.initial_backoff_seconds < 0:
            raise ValueError("initial_backoff_seconds must not be negative")
        if self.backoff_multiplier < 1:
            raise ValueError("backoff_multiplier must be at least 1")
        if self.max_backoff_seconds < 0:
            raise ValueError("max_backoff_seconds must not be negative")

    def backoff_seconds(self, failed_attempt: int) -> float:
        """Return the capped delay after a failed one-based attempt number."""
        if failed_attempt < 1:
            raise ValueError("failed_attempt must be at least 1")
        delay = self.initial_backoff_seconds * (
            self.backoff_multiplier ** (failed_attempt - 1)
        )
        return min(delay, self.max_backoff_seconds)


@dataclass(frozen=True, slots=True)
class TaskStepSpec:
    """Stable identity and routing metadata for one logical task step."""

    name: str
    queue: QueueClass
    ordinal: int
    idempotency_key: str
    input_payload: dict[str, Any]
    required_approval_id: int | None = None
    required_approval_action: str | None = None
    approval_input_payload: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class TaskAttemptContext:
    """Minimal execution context exposed to a task adapter."""

    run_id: int
    step_run_id: int
    attempt: int


@dataclass(frozen=True, slots=True)
class TaskExecutionResult:
    """Durable outcome of a logical task step and all attempts seen."""

    status: RunStatus
    attempt_count: int
    step_run_ids: tuple[int, ...]
    output_payload: dict[str, Any] | None
    error: dict[str, Any] | None
    replayed: bool


async def execute_task_step(
    session: AsyncSession,
    *,
    run: Run,
    spec: TaskStepSpec,
    policy: RetryPolicy,
    operation: TaskOperation,
    cancellation_requested: CancellationProbe | None = None,
    sleep: Sleeper = asyncio.sleep,
) -> TaskExecutionResult:
    """Execute a logical step and commit every observable attempt boundary."""
    if run.id is None:
        raise ValueError("run must be persisted before task execution")
    if RunStatus(run.status) is not RunStatus.RUNNING:
        raise InvalidRunTransitionError("tasks can only execute for a running run")
    if spec.required_approval_id is not None:
        approval_action = spec.required_approval_action or spec.name
        approval_input = spec.approval_input_payload or spec.input_payload
        await assert_approval_granted(
            session,
            approval_id=spec.required_approval_id,
            run_id=run.id,
            action=approval_action,
            step_idempotency_key=spec.idempotency_key,
            input_payload=approval_input,
        )
    elif spec.required_approval_action is not None or spec.approval_input_payload is not None:
        raise ValueError("approval details require required_approval_id")

    attempts = list(
        (
            await session.scalars(
                select(StepRun)
                .where(
                    StepRun.run_id == run.id,
                    StepRun.idempotency_key == spec.idempotency_key,
                )
                .order_by(StepRun.attempt)
            )
        ).all()
    )
    _validate_existing_attempts(attempts, spec)
    if attempts and RunStatus(attempts[-1].status) in {
        RunStatus.SUCCEEDED,
        RunStatus.CANCELLED,
    }:
        return _result(attempts, replayed=True)

    if attempts and RunStatus(attempts[-1].status) is RunStatus.RUNNING:
        interrupted_error = _structured_error(
            code="INTERRUPTED_ATTEMPT",
            exception_type="InterruptedAttempt",
            retryable=True,
            timed_out=False,
        )
        await transition_step_run(
            session,
            attempts[-1],
            RunStatus.FAILED,
            error=interrupted_error,
            note="unfinished local attempt recovered",
        )
        await session.commit()

    if (
        attempts
        and RunStatus(attempts[-1].status) is RunStatus.FAILED
        and attempts[-1].attempt >= policy.max_attempts
    ):
        return _result(attempts, replayed=True)

    if attempts and RunStatus(attempts[-1].status) is RunStatus.QUEUED:
        next_attempt = attempts[-1].attempt
    else:
        next_attempt = attempts[-1].attempt + 1 if attempts else 1
    while next_attempt <= policy.max_attempts:
        creation = await get_or_create_step_run(
            session,
            run=run,
            name=spec.name,
            queue=spec.queue,
            ordinal=spec.ordinal,
            idempotency_key=spec.idempotency_key,
            attempt=next_attempt,
            approval_id=spec.required_approval_id,
            input_payload=spec.input_payload,
        )
        step_run = creation.step_run
        if not attempts or attempts[-1].id != step_run.id:
            attempts.append(step_run)

        control_code = await _execution_block_code(
            session,
            run_id=run.id,
            cancellation_requested=cancellation_requested,
        )
        if control_code is not None:
            await transition_step_run(
                session,
                step_run,
                RunStatus.CANCELLED,
                note=f"execution blocked by {control_code}",
            )
            await transition_run(
                session,
                run,
                RunStatus.CANCELLED,
                note=f"execution blocked by {control_code}",
            )
            await session.commit()
            return _result(attempts, replayed=False)

        await transition_step_run(
            session,
            step_run,
            RunStatus.RUNNING,
            note=f"attempt {next_attempt} started",
        )
        await session.commit()
        context = TaskAttemptContext(
            run_id=run.id,
            step_run_id=_persisted_id(step_run),
            attempt=next_attempt,
        )
        try:
            async with asyncio.timeout(policy.timeout_seconds):
                output = await operation(context)
        except TimeoutError:
            error = _structured_error(
                code="TASK_TIMEOUT",
                exception_type="TimeoutError",
                retryable=True,
                timed_out=True,
            )
        except RetryableTaskError as exc:
            error = _structured_error(
                code=exc.code,
                exception_type=type(exc).__name__,
                retryable=True,
                timed_out=False,
            )
        except PermanentTaskError as exc:
            error = _structured_error(
                code=exc.code,
                exception_type=type(exc).__name__,
                retryable=False,
                timed_out=False,
            )
        except Exception as exc:
            error = _structured_error(
                code="UNEXPECTED_TASK_ERROR",
                exception_type=type(exc).__name__,
                retryable=False,
                timed_out=False,
            )
        else:
            control_code = await _execution_block_code(
                session,
                run_id=run.id,
                cancellation_requested=cancellation_requested,
            )
            if control_code is not None:
                await transition_step_run(
                    session,
                    step_run,
                    RunStatus.CANCELLED,
                    note=f"completion blocked by {control_code}",
                )
                await transition_run(
                    session,
                    run,
                    RunStatus.CANCELLED,
                    note=f"completion blocked by {control_code}",
                )
                await session.commit()
                return _result(attempts, replayed=False)
            await transition_step_run(
                session,
                step_run,
                RunStatus.SUCCEEDED,
                output_payload=output,
                note=f"attempt {next_attempt} completed",
            )
            await session.commit()
            return _result(attempts, replayed=False)

        await transition_step_run(
            session,
            step_run,
            RunStatus.FAILED,
            error=error,
            note=f"attempt {next_attempt} failed",
        )
        await session.commit()
        if not error["retryable"] or next_attempt >= policy.max_attempts:
            return _result(attempts, replayed=False)

        await sleep(policy.backoff_seconds(next_attempt))
        next_attempt += 1

    raise RuntimeError("task runner exhausted without a durable result")


async def _execution_block_code(
    session: AsyncSession,
    *,
    run_id: int,
    cancellation_requested: CancellationProbe | None,
) -> str | None:
    control = await get_execution_control_state(session, run_id=run_id)
    if control.blocked:
        return control.code
    if cancellation_requested is not None and await cancellation_requested():
        return "CANCELLATION_CALLBACK"
    return None


def _validate_existing_attempts(attempts: list[StepRun], spec: TaskStepSpec) -> None:
    expected = (
        spec.name.strip(),
        spec.queue.value,
        spec.ordinal,
        spec.required_approval_id,
        spec.input_payload,
    )
    for attempt in attempts:
        actual = (
            attempt.name,
            attempt.queue,
            attempt.ordinal,
            attempt.approval_id,
            attempt.input_payload,
        )
        if actual != expected:
            raise IdempotencyConflictError(
                "task idempotency key already exists with different inputs"
            )


def _structured_error(
    *,
    code: str,
    exception_type: str,
    retryable: bool,
    timed_out: bool,
) -> dict[str, Any]:
    return {
        "code": code,
        "exception_type": exception_type,
        "retryable": retryable,
        "timed_out": timed_out,
    }


def _persisted_id(step_run: StepRun) -> int:
    if not isinstance(step_run.id, int):
        raise ValueError("step must be persisted")
    return step_run.id


def _result(attempts: list[StepRun], *, replayed: bool) -> TaskExecutionResult:
    if not attempts:
        raise ValueError("task result requires at least one attempt")
    final = attempts[-1]
    return TaskExecutionResult(
        status=RunStatus(final.status),
        attempt_count=len(attempts),
        step_run_ids=tuple(_persisted_id(attempt) for attempt in attempts),
        output_payload=final.output_payload,
        error=final.error,
        replayed=replayed,
    )
