"""Persistent, audited lifecycle services for measurable experiments."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.platform.models import (
    Automation,
    Experiment,
    ExperimentEvent,
    ExperimentEventType,
    ExperimentStatus,
)
from src.platform.run_service import IdempotencyConflictError

_EXPERIMENT_KEY = re.compile(r"[a-z0-9][a-z0-9._-]{0,99}")
_ALLOWED_TRANSITIONS: dict[ExperimentStatus, frozenset[ExperimentStatus]] = {
    ExperimentStatus.DRAFT: frozenset(
        {ExperimentStatus.RUNNING, ExperimentStatus.CANCELLED}
    ),
    ExperimentStatus.RUNNING: frozenset(
        {
            ExperimentStatus.PAUSED,
            ExperimentStatus.COMPLETED,
            ExperimentStatus.CANCELLED,
        }
    ),
    ExperimentStatus.PAUSED: frozenset(
        {
            ExperimentStatus.RUNNING,
            ExperimentStatus.COMPLETED,
            ExperimentStatus.CANCELLED,
        }
    ),
    ExperimentStatus.COMPLETED: frozenset(),
    ExperimentStatus.CANCELLED: frozenset(),
}


class ExperimentControlError(ValueError):
    """Raised when an experiment state change violates its lifecycle."""


@dataclass(frozen=True, slots=True)
class ExperimentCreationResult:
    experiment: Experiment
    created: bool


@dataclass(frozen=True, slots=True)
class ExperimentChangeResult:
    experiment: Experiment
    changed: bool


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _require_text(value: str, field: str, *, max_length: int) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > max_length:
        raise ValueError(f"{field} must be 1 to {max_length} characters")
    return normalized


async def get_or_create_experiment(
    session: AsyncSession,
    *,
    automation: Automation,
    key: str,
    name: str,
    hypothesis: str,
    primary_metric: str,
    primary_metric_unit: str,
    control_variant: str,
    candidate_variant: str,
    actor: str,
    reason: str,
) -> ExperimentCreationResult:
    """Create one draft experiment and its initial audit event idempotently."""
    if automation.id is None:
        raise ValueError("automation must be persisted before creating an experiment")
    key = key.strip().lower()
    if not _EXPERIMENT_KEY.fullmatch(key):
        raise ValueError("key must be a lowercase stable identifier")
    name = _require_text(name, "name", max_length=200)
    hypothesis = _require_text(hypothesis, "hypothesis", max_length=2_000)
    primary_metric = _require_text(primary_metric, "primary_metric", max_length=200)
    primary_metric_unit = _require_text(
        primary_metric_unit,
        "primary_metric_unit",
        max_length=30,
    )
    control_variant = _require_text(control_variant, "control_variant", max_length=100)
    candidate_variant = _require_text(
        candidate_variant,
        "candidate_variant",
        max_length=100,
    )
    actor = _require_text(actor, "actor", max_length=200)
    reason = _require_text(reason, "reason", max_length=500)
    if control_variant.casefold() == candidate_variant.casefold():
        raise ValueError("control and candidate variants must differ")

    existing = await session.scalar(
        select(Experiment).where(
            Experiment.automation_id == automation.id,
            Experiment.key == key,
        )
    )
    expected = (
        name,
        hypothesis,
        primary_metric,
        primary_metric_unit,
        control_variant,
        candidate_variant,
    )
    if existing is not None:
        actual = (
            existing.name,
            existing.hypothesis,
            existing.primary_metric,
            existing.primary_metric_unit,
            existing.control_variant,
            existing.candidate_variant,
        )
        if actual != expected:
            raise IdempotencyConflictError(
                "experiment key already exists with different inputs"
            )
        return ExperimentCreationResult(existing, created=False)

    now = _utcnow()
    experiment = Experiment(
        automation_id=automation.id,
        key=key,
        name=name,
        hypothesis=hypothesis,
        primary_metric=primary_metric,
        primary_metric_unit=primary_metric_unit,
        control_variant=control_variant,
        candidate_variant=candidate_variant,
        status=ExperimentStatus.DRAFT.value,
        created_at=now,
    )
    session.add(experiment)
    await session.flush()
    session.add(
        ExperimentEvent(
            experiment_id=experiment.id,
            event_type=ExperimentEventType.CREATED.value,
            from_status=None,
            to_status=ExperimentStatus.DRAFT.value,
            actor=actor,
            reason=reason,
            occurred_at=now,
        )
    )
    await session.flush()
    return ExperimentCreationResult(experiment, created=True)


async def transition_experiment(
    session: AsyncSession,
    *,
    experiment: Experiment,
    target: ExperimentStatus,
    actor: str,
    reason: str,
) -> ExperimentChangeResult:
    """Apply one valid, audited and idempotent experiment state transition."""
    if experiment.id is None:
        raise ValueError("experiment must be persisted before transition")
    actor = _require_text(actor, "actor", max_length=200)
    reason = _require_text(reason, "reason", max_length=500)
    current = ExperimentStatus(experiment.status)
    if current is target:
        return ExperimentChangeResult(experiment, changed=False)
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise ExperimentControlError(
            f"cannot transition experiment from {current.value} to {target.value}"
        )
    automation = await session.get(Automation, experiment.automation_id)
    if automation is None:
        raise ExperimentControlError("experiment automation does not exist")
    if target is ExperimentStatus.RUNNING:
        if not automation.enabled:
            raise ExperimentControlError("cannot run experiment for disabled automation")
        if automation.kill_switch_active:
            raise ExperimentControlError("cannot run experiment while kill switch is active")

    event_type = _event_type(current, target)
    now = _utcnow()
    experiment.status = target.value
    if target is ExperimentStatus.RUNNING and experiment.started_at is None:
        experiment.started_at = now
    if target in {ExperimentStatus.COMPLETED, ExperimentStatus.CANCELLED}:
        experiment.ended_at = now
    session.add(
        ExperimentEvent(
            experiment_id=experiment.id,
            event_type=event_type.value,
            from_status=current.value,
            to_status=target.value,
            actor=actor,
            reason=reason,
            occurred_at=now,
        )
    )
    await session.flush()
    return ExperimentChangeResult(experiment, changed=True)


def _event_type(
    current: ExperimentStatus,
    target: ExperimentStatus,
) -> ExperimentEventType:
    if target is ExperimentStatus.CANCELLED:
        return ExperimentEventType.CANCELLED
    if target is ExperimentStatus.COMPLETED:
        return ExperimentEventType.COMPLETED
    if target is ExperimentStatus.PAUSED:
        return ExperimentEventType.PAUSED
    if current is ExperimentStatus.DRAFT:
        return ExperimentEventType.STARTED
    return ExperimentEventType.RESUMED
