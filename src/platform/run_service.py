"""Transactional services for creating and transitioning durable runs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.platform.models import (
    Automation,
    Experiment,
    ExperimentStatus,
    Run,
    RunStatus,
    RunTransition,
)

_ALLOWED_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.QUEUED: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED}),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.AWAITING_APPROVAL,
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.AWAITING_APPROVAL: frozenset(
        {RunStatus.RUNNING, RunStatus.CANCELLED}
    ),
    RunStatus.SUCCEEDED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}


class InvalidRunTransitionError(ValueError):
    """Raised when a requested run transition violates the lifecycle contract."""


class IdempotencyConflictError(ValueError):
    """Raised when an idempotency key is reused with different inputs."""


@dataclass(frozen=True, slots=True)
class AutomationCreationResult:
    automation: Automation
    created: bool


@dataclass(frozen=True, slots=True)
class RunCreationResult:
    run: Run
    created: bool


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _require_text(value: str, field: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} must not be empty")
    return normalized


async def get_or_create_automation(
    session: AsyncSession,
    *,
    slug: str,
    name: str,
    owner: str,
) -> AutomationCreationResult:
    """Register an automation idempotently by stable slug."""
    slug = _require_text(slug, "slug")
    name = _require_text(name, "name")
    owner = _require_text(owner, "owner")

    existing = await session.scalar(select(Automation).where(Automation.slug == slug))
    if existing is not None:
        _validate_automation_metadata(existing, name=name, owner=owner)
        return AutomationCreationResult(existing, created=False)

    automation = Automation(slug=slug, name=name, owner=owner)
    try:
        async with session.begin_nested():
            session.add(automation)
            await session.flush()
    except IntegrityError:
        # Another PostgreSQL session may have inserted the same stable slug after
        # our initial read. The unique constraint serializes that race safely.
        existing = await session.scalar(
            select(Automation).where(Automation.slug == slug)
        )
        if existing is None:
            raise
        _validate_automation_metadata(existing, name=name, owner=owner)
        return AutomationCreationResult(existing, created=False)
    return AutomationCreationResult(automation, created=True)


def _validate_automation_metadata(
    automation: Automation,
    *,
    name: str,
    owner: str,
) -> None:
    if automation.name != name or automation.owner != owner:
        raise IdempotencyConflictError(
            "automation slug already exists with different metadata"
        )


async def get_or_create_run(
    session: AsyncSession,
    *,
    automation: Automation,
    idempotency_key: str,
    trigger: str = "manual",
    input_payload: dict[str, Any] | None = None,
    retry_of_run_id: int | None = None,
    retry_requested_by: str | None = None,
    retry_reason: str | None = None,
    experiment_id: int | None = None,
) -> RunCreationResult:
    """Create one queued run and its initial transition, or return its idempotent match."""
    idempotency_key = _require_text(idempotency_key, "idempotency_key")
    trigger = _require_text(trigger, "trigger")
    payload = dict(input_payload or {})

    if retry_of_run_id is None:
        if retry_requested_by is not None or retry_reason is not None:
            raise ValueError("retry metadata requires retry_of_run_id")
        retry_actor = None
        normalized_retry_reason = None
    else:
        retry_actor = _require_text(retry_requested_by or "", "retry_requested_by")
        normalized_retry_reason = _require_text(retry_reason or "", "retry_reason")
        if len(retry_actor) > 200 or len(normalized_retry_reason) > 500:
            raise ValueError("retry metadata exceeds its maximum length")

    if automation.id is None:
        raise ValueError("automation must be persisted before creating a run")

    retry_source: Run | None = None
    if retry_of_run_id is not None:
        retry_source = await session.get(Run, retry_of_run_id)
        if retry_source is None or retry_source.automation_id != automation.id:
            raise InvalidRunTransitionError("retry source does not belong to automation")
        if RunStatus(retry_source.status) is not RunStatus.FAILED:
            raise InvalidRunTransitionError("only a failed run can be retried")
        if (
            not isinstance(retry_source.error, dict)
            or retry_source.error.get("retryable") is not True
        ):
            raise InvalidRunTransitionError("failed run is not marked retryable")
        if experiment_id is None:
            experiment_id = retry_source.experiment_id

    if experiment_id is not None:
        experiment = await session.get(Experiment, experiment_id)
        if experiment is None or experiment.automation_id != automation.id:
            raise InvalidRunTransitionError("experiment does not belong to automation")
        if ExperimentStatus(experiment.status) is not ExperimentStatus.RUNNING:
            raise InvalidRunTransitionError("experiment must be running for run attribution")

    existing = await session.scalar(
        select(Run).where(
            Run.automation_id == automation.id,
            Run.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        _validate_run_inputs(
            existing,
            trigger=trigger,
            payload=payload,
            retry_of_run_id=retry_of_run_id,
            retry_actor=retry_actor,
            retry_reason=normalized_retry_reason,
            experiment_id=experiment_id,
        )
        return RunCreationResult(existing, created=False)

    if retry_of_run_id is not None:
        successor = await session.scalar(
            select(Run).where(Run.retry_of_run_id == retry_of_run_id)
        )
        if successor is not None:
            raise IdempotencyConflictError("failed run already has a retry successor")

    run = Run(
        automation_id=automation.id,
        retry_of_run_id=retry_of_run_id,
        retry_requested_by=retry_actor,
        retry_reason=normalized_retry_reason,
        experiment_id=experiment_id,
        idempotency_key=idempotency_key,
        trigger=trigger,
        input_payload=payload,
        status=RunStatus.QUEUED.value,
    )
    try:
        async with session.begin_nested():
            session.add(run)
            await session.flush()
            session.add(
                RunTransition(
                    run_id=run.id,
                    from_status=None,
                    to_status=RunStatus.QUEUED.value,
                    note="run created",
                )
            )
            await session.flush()
    except IntegrityError as exc:
        existing = await session.scalar(
            select(Run).where(
                Run.automation_id == automation.id,
                Run.idempotency_key == idempotency_key,
            )
        )
        if existing is None:
            if retry_of_run_id is not None:
                successor = await session.scalar(
                    select(Run).where(Run.retry_of_run_id == retry_of_run_id)
                )
                if successor is not None:
                    raise IdempotencyConflictError(
                        "failed run already has a retry successor"
                    ) from exc
            raise
        _validate_run_inputs(
            existing,
            trigger=trigger,
            payload=payload,
            retry_of_run_id=retry_of_run_id,
            retry_actor=retry_actor,
            retry_reason=normalized_retry_reason,
            experiment_id=experiment_id,
        )
        return RunCreationResult(existing, created=False)
    return RunCreationResult(run, created=True)


def _validate_run_inputs(
    run: Run,
    *,
    trigger: str,
    payload: dict[str, Any],
    retry_of_run_id: int | None,
    retry_actor: str | None,
    retry_reason: str | None,
    experiment_id: int | None,
) -> None:
    if (
        run.trigger != trigger
        or run.input_payload != payload
        or run.retry_of_run_id != retry_of_run_id
        or run.retry_requested_by != retry_actor
        or run.retry_reason != retry_reason
        or run.experiment_id != experiment_id
    ):
        raise IdempotencyConflictError(
            "run idempotency key already exists with different inputs"
        )


async def transition_run(
    session: AsyncSession,
    run: Run,
    target: RunStatus,
    *,
    output_payload: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
    note: str | None = None,
) -> RunTransition:
    """Apply and persist one valid run transition inside the caller's transaction."""
    if run.id is None:
        raise ValueError("run must be persisted before transition")

    current = RunStatus(run.status)
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise InvalidRunTransitionError(f"cannot transition run from {current} to {target}")

    if target is RunStatus.RUNNING:
        control = (
            await session.execute(
                select(
                    Automation.kill_switch_active,
                    Run.cancellation_requested_at,
                )
                .join(Run, Run.automation_id == Automation.id)
                .where(Run.id == run.id)
            )
        ).one_or_none()
        if control is None:
            raise ValueError("run does not exist")
        if control.kill_switch_active:
            raise InvalidRunTransitionError(
                "cannot start run while automation kill switch is active"
            )
        if control.cancellation_requested_at is not None:
            raise InvalidRunTransitionError(
                "cannot start run after cancellation was requested"
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
    run.status = target.value
    if target is RunStatus.RUNNING and run.started_at is None:
        run.started_at = now
    if target in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}:
        run.finished_at = now
    run.output_payload = output
    run.error = dict(error) if error is not None else None

    transition = RunTransition(
        run_id=run.id,
        from_status=current.value,
        to_status=target.value,
        error=dict(error) if error is not None else None,
        note=note.strip() if note and note.strip() else None,
    )
    session.add(transition)
    await session.flush()
    return transition
