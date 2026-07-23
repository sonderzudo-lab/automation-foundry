"""Transactional services for durable step execution state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.platform.models import QueueClass, Run, RunStatus, StepRun, StepRunTransition
from src.platform.run_service import IdempotencyConflictError, InvalidRunTransitionError

_ALLOWED_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.QUEUED: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED}),
    RunStatus.RUNNING: frozenset(
        {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}
    ),
    RunStatus.SUCCEEDED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class StepRunCreationResult:
    step_run: StepRun
    created: bool


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _require_text(value: str, field: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} must not be empty")
    return normalized


async def get_or_create_step_run(
    session: AsyncSession,
    *,
    run: Run,
    name: str,
    queue: QueueClass,
    ordinal: int,
    idempotency_key: str,
    attempt: int = 1,
    input_payload: dict[str, Any] | None = None,
) -> StepRunCreationResult:
    """Create a queued step with initial evidence, or return its exact match."""
    if run.id is None:
        raise ValueError("run must be persisted before creating a step")
    if RunStatus(run.status) is not RunStatus.RUNNING:
        raise InvalidRunTransitionError("steps can only be created for a running run")
    if ordinal < 1:
        raise ValueError("ordinal must be at least 1")
    if attempt < 1:
        raise ValueError("attempt must be at least 1")

    name = _require_text(name, "name")
    idempotency_key = _require_text(idempotency_key, "idempotency_key")
    payload = dict(input_payload or {})
    existing = await session.scalar(
        select(StepRun).where(
            StepRun.run_id == run.id,
            StepRun.idempotency_key == idempotency_key,
            StepRun.attempt == attempt,
        )
    )
    if existing is not None:
        expected = (name, queue.value, ordinal, payload)
        actual = (
            existing.name,
            existing.queue,
            existing.ordinal,
            existing.input_payload,
        )
        if actual != expected:
            raise IdempotencyConflictError(
                "step idempotency key already exists with different inputs"
            )
        return StepRunCreationResult(existing, created=False)

    step_run = StepRun(
        run_id=run.id,
        name=name,
        queue=queue.value,
        ordinal=ordinal,
        attempt=attempt,
        idempotency_key=idempotency_key,
        input_payload=payload,
        status=RunStatus.QUEUED.value,
    )
    session.add(step_run)
    await session.flush()
    session.add(
        StepRunTransition(
            step_run_id=step_run.id,
            from_status=None,
            to_status=RunStatus.QUEUED.value,
            note="step created",
        )
    )
    await session.flush()
    return StepRunCreationResult(step_run, created=True)


async def transition_step_run(
    session: AsyncSession,
    step_run: StepRun,
    target: RunStatus,
    *,
    output_payload: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    note: str | None = None,
) -> StepRunTransition:
    """Apply one valid step transition inside the caller's transaction."""
    if step_run.id is None:
        raise ValueError("step must be persisted before transition")
    parent_status = await session.scalar(
        select(Run.status).where(Run.id == step_run.run_id)
    )
    if parent_status is None:
        raise ValueError("step parent run does not exist")
    if RunStatus(parent_status) is not RunStatus.RUNNING:
        raise InvalidRunTransitionError(
            "steps can only transition while their parent run is running"
        )

    current = RunStatus(step_run.status)
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise InvalidRunTransitionError(
            f"cannot transition step from {current} to {target}"
        )
    if target is RunStatus.FAILED and not error:
        raise InvalidRunTransitionError("failed transition requires structured error")
    if target is not RunStatus.FAILED and error is not None:
        raise InvalidRunTransitionError("structured error is only valid for failed transition")
    if target is RunStatus.SUCCEEDED:
        output = dict(output_payload or {})
    elif output_payload is not None:
        raise InvalidRunTransitionError("output is only valid for succeeded transition")
    else:
        output = None

    now = _utcnow()
    step_run.status = target.value
    if target is RunStatus.RUNNING and step_run.started_at is None:
        step_run.started_at = now
    if target in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}:
        step_run.finished_at = now
    step_run.output_payload = output
    step_run.error = dict(error) if error is not None else None

    transition = StepRunTransition(
        step_run_id=step_run.id,
        from_status=current.value,
        to_status=target.value,
        error=dict(error) if error is not None else None,
        note=note.strip() if note and note.strip() else None,
    )
    session.add(transition)
    await session.flush()
    return transition
