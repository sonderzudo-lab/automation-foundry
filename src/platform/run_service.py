"""Transactional services for creating and transitioning durable runs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.platform.models import Automation, Run, RunStatus, RunTransition

_ALLOWED_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.QUEUED: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED}),
    RunStatus.RUNNING: frozenset(
        {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}
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
        if existing.name != name or existing.owner != owner:
            raise IdempotencyConflictError(
                "automation slug already exists with different metadata"
            )
        return AutomationCreationResult(existing, created=False)

    automation = Automation(slug=slug, name=name, owner=owner)
    session.add(automation)
    await session.flush()
    return AutomationCreationResult(automation, created=True)


async def get_or_create_run(
    session: AsyncSession,
    *,
    automation: Automation,
    idempotency_key: str,
    trigger: str = "manual",
    input_payload: dict[str, Any] | None = None,
) -> RunCreationResult:
    """Create one queued run and its initial transition, or return its idempotent match."""
    idempotency_key = _require_text(idempotency_key, "idempotency_key")
    trigger = _require_text(trigger, "trigger")
    payload = dict(input_payload or {})

    if automation.id is None:
        raise ValueError("automation must be persisted before creating a run")

    existing = await session.scalar(
        select(Run).where(
            Run.automation_id == automation.id,
            Run.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        if existing.trigger != trigger or existing.input_payload != payload:
            raise IdempotencyConflictError(
                "run idempotency key already exists with different inputs"
            )
        return RunCreationResult(existing, created=False)

    run = Run(
        automation_id=automation.id,
        idempotency_key=idempotency_key,
        trigger=trigger,
        input_payload=payload,
        status=RunStatus.QUEUED.value,
    )
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
    return RunCreationResult(run, created=True)


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
