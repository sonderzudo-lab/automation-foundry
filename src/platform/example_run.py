"""A deterministic, local-only smoke automation for the run kernel."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.platform.models import QueueClass, Run, RunStatus, StepRun
from src.platform.run_service import (
    get_or_create_automation,
    get_or_create_run,
    transition_run,
)
from src.platform.step_service import get_or_create_step_run, transition_step_run

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
) -> ExampleRunResult:
    """Persist and execute one no-op IO step inside the caller's transaction."""
    automation_result = await get_or_create_automation(
        session,
        slug=_AUTOMATION_SLUG,
        name="Platform Smoke Automation",
        owner="local-operator",
    )
    run_result = await get_or_create_run(
        session,
        automation=automation_result.automation,
        idempotency_key=idempotency_key,
        trigger="manual",
        input_payload={"operation": _STEP_NAME},
    )
    run = run_result.run
    step_key = f"{idempotency_key}:{_STEP_NAME}"

    if RunStatus(run.status) is RunStatus.SUCCEEDED:
        existing_step = await session.scalar(
            select(StepRun).where(
                StepRun.run_id == run.id,
                StepRun.idempotency_key == step_key,
                StepRun.attempt == 1,
            )
        )
        if existing_step is None or existing_step.status != RunStatus.SUCCEEDED.value:
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

    step_result = await get_or_create_step_run(
        session,
        run=run,
        name=_STEP_NAME,
        queue=QueueClass.IO,
        ordinal=1,
        idempotency_key=step_key,
        input_payload={"operation": _STEP_NAME},
    )
    step_run = step_result.step_run
    step_status = RunStatus(step_run.status)
    if step_status in {RunStatus.FAILED, RunStatus.CANCELLED}:
        raise ExampleRunNotRunnableError(
            f"existing example step is terminal with status {step_run.status}"
        )
    if step_status is RunStatus.QUEUED:
        await transition_step_run(
            session,
            step_run,
            RunStatus.RUNNING,
            note="local no-op started",
        )
    if RunStatus(step_run.status) is RunStatus.RUNNING:
        await transition_step_run(
            session,
            step_run,
            RunStatus.SUCCEEDED,
            output_payload={"message": "ok"},
            note="local no-op completed",
        )

    await transition_run(
        session,
        run,
        RunStatus.SUCCEEDED,
        output_payload={"step_run_id": step_run.id},
        note="manual smoke completed",
    )
    return _result(run, step_run, created=run_result.created, replayed=False)


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
