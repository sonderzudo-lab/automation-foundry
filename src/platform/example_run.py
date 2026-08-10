"""A deterministic, local-only smoke automation for the run kernel."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.platform.models import QueueClass, Run, RunStatus, StepRun
from src.platform.run_service import (
    RunCreationResult,
    get_or_create_automation,
    get_or_create_run,
    transition_run,
)
from src.platform.task_runner import (
    RetryPolicy,
    TaskAttemptContext,
    TaskStepSpec,
    execute_task_step,
)

_AUTOMATION_SLUG = "platform-smoke"
_STEP_NAME = "noop"


class ExampleRunNotRunnableError(ValueError):
    """Raised when an existing example run cannot be safely resumed."""


@dataclass(frozen=True, slots=True)
class ExampleRunResult:
    automation_id: int
    run_id: int
    step_run_id: int
    run_status: str
    step_status: str
    created: bool
    replayed: bool


async def execute_example_run(
    session: AsyncSession,
    *,
    idempotency_key: str,
    retry_of_run_id: int | None = None,
    retry_requested_by: str | None = None,
    retry_reason: str | None = None,
    experiment_id: int | None = None,
    trigger: str = "manual",
    input_payload: dict[str, object] | None = None,
) -> ExampleRunResult:
    """Persist and execute one no-op IO step inside the caller's transaction."""
    run_result = await prepare_example_run(
        session,
        idempotency_key=idempotency_key,
        retry_of_run_id=retry_of_run_id,
        retry_requested_by=retry_requested_by,
        retry_reason=retry_reason,
        experiment_id=experiment_id,
        trigger=trigger,
        input_payload=input_payload,
    )
    run = run_result.run
    step_key = f"{idempotency_key}:{_STEP_NAME}"

    if RunStatus(run.status) is RunStatus.SUCCEEDED:
        existing_step = await session.scalar(
            select(StepRun)
            .where(
                StepRun.run_id == run.id,
                StepRun.idempotency_key == step_key,
                StepRun.status == RunStatus.SUCCEEDED.value,
            )
            .order_by(StepRun.attempt.desc())
        )
        if existing_step is None:
            raise ExampleRunNotRunnableError(
                "succeeded example run is missing its succeeded step"
            )
        return _result(run, existing_step, created=False, replayed=True)

    if RunStatus(run.status) in {RunStatus.FAILED, RunStatus.CANCELLED}:
        raise ExampleRunNotRunnableError(
            f"existing example run is terminal with status {run.status}"
        )
    if RunStatus(run.status) is RunStatus.QUEUED:
        await transition_run(session, run, RunStatus.RUNNING, note="manual smoke started")

    task_result = await execute_task_step(
        session,
        run=run,
        spec=TaskStepSpec(
            name=_STEP_NAME,
            queue=QueueClass.IO,
            ordinal=1,
            idempotency_key=step_key,
            input_payload={"operation": _STEP_NAME},
        ),
        policy=RetryPolicy(),
        operation=_noop_operation,
    )
    if task_result.status is not RunStatus.SUCCEEDED:
        raise ExampleRunNotRunnableError(
            f"example step ended with status {task_result.status.value}"
        )
    step_run = await session.get(StepRun, task_result.step_run_ids[-1])
    if step_run is None:
        raise ExampleRunNotRunnableError("example step result was not persisted")

    await transition_run(
        session,
        run,
        RunStatus.SUCCEEDED,
        output_payload={"step_run_id": step_run.id},
        note="manual smoke completed",
    )
    return _result(run, step_run, created=run_result.created, replayed=False)


async def prepare_example_run(
    session: AsyncSession,
    *,
    idempotency_key: str,
    retry_of_run_id: int | None = None,
    retry_requested_by: str | None = None,
    retry_reason: str | None = None,
    experiment_id: int | None = None,
    trigger: str = "manual",
    input_payload: dict[str, object] | None = None,
) -> RunCreationResult:
    """Persist the smoke run without executing it or contacting the broker."""
    automation_result = await get_or_create_automation(
        session,
        slug=_AUTOMATION_SLUG,
        name="Platform Smoke Automation",
        owner="local-operator",
    )
    return await get_or_create_run(
        session,
        automation=automation_result.automation,
        idempotency_key=idempotency_key,
        trigger="retry" if retry_of_run_id is not None else trigger,
        input_payload={"operation": _STEP_NAME, **dict(input_payload or {})},
        retry_of_run_id=retry_of_run_id,
        retry_requested_by=retry_requested_by,
        retry_reason=retry_reason,
        experiment_id=experiment_id,
    )


async def _noop_operation(_context: TaskAttemptContext) -> dict[str, str]:
    return {"message": "ok"}


def _result(
    run: Run,
    step_run: StepRun,
    *,
    created: bool,
    replayed: bool,
) -> ExampleRunResult:
    if not isinstance(run.id, int) or not isinstance(run.automation_id, int):
        raise ValueError("example run must be persisted")
    if not isinstance(step_run.id, int):
        raise ValueError("example step must be persisted")
    return ExampleRunResult(
        automation_id=run.automation_id,
        run_id=run.id,
        step_run_id=step_run.id,
        run_status=run.status,
        step_status=step_run.status,
        created=created,
        replayed=replayed,
    )
