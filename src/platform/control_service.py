"""Persistent operator controls for automation state, cancellation, and kill switches."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.platform.models import (
    Approval,
    ApprovalEvent,
    ApprovalStatus,
    Automation,
    ControlEvent,
    ControlEventType,
    Run,
    RunStatus,
)
from src.platform.run_service import InvalidRunTransitionError, transition_run


@dataclass(frozen=True, slots=True)
class ControlChangeResult:
    """Outcome of an idempotent operator control mutation."""

    changed: bool
    event: ControlEvent | None


@dataclass(frozen=True, slots=True)
class ExecutionControlState:
    """Current database-backed controls affecting one run."""

    cancellation_requested: bool
    kill_switch_active: bool

    @property
    def blocked(self) -> bool:
        return self.cancellation_requested or self.kill_switch_active

    @property
    def code(self) -> str | None:
        if self.kill_switch_active:
            return "AUTOMATION_KILL_SWITCH"
        if self.cancellation_requested:
            return "RUN_CANCELLATION_REQUESTED"
        return None


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _require_reason(reason: str) -> str:
    normalized = reason.strip()
    if not normalized:
        raise ValueError("reason must not be empty")
    return normalized


def _require_actor(actor: str) -> str:
    normalized = actor.strip()
    if not normalized:
        raise ValueError("actor must not be empty")
    return normalized


async def set_automation_kill_switch(
    session: AsyncSession,
    *,
    automation: Automation,
    active: bool,
    actor: str,
    reason: str,
) -> ControlChangeResult:
    """Set an automation kill switch idempotently and append audit evidence."""
    if automation.id is None:
        raise ValueError("automation must be persisted before changing kill switch")
    reason = _require_reason(reason)
    actor = _require_actor(actor)
    current = await session.scalar(
        select(Automation.kill_switch_active).where(Automation.id == automation.id)
    )
    if current is None:
        raise ValueError("automation does not exist")
    if current is active:
        return ControlChangeResult(changed=False, event=None)

    automation.kill_switch_active = active
    automation.kill_switch_reason = reason
    automation.kill_switch_changed_at = _utcnow()
    event = ControlEvent(
        automation_id=automation.id,
        event_type=(
            ControlEventType.KILL_SWITCH_ENABLED.value
            if active
            else ControlEventType.KILL_SWITCH_DISABLED.value
        ),
        actor=actor,
        reason=reason,
    )
    session.add(event)
    await session.flush()
    return ControlChangeResult(changed=True, event=event)


async def set_automation_enabled(
    session: AsyncSession,
    *,
    automation: Automation,
    enabled: bool,
    actor: str,
    reason: str,
) -> ControlChangeResult:
    """Set administrative admission for new runs and append audit evidence."""
    if automation.id is None:
        raise ValueError("automation must be persisted before changing enabled state")
    reason = _require_reason(reason)
    actor = _require_actor(actor)
    current = await session.scalar(
        select(Automation.enabled).where(Automation.id == automation.id)
    )
    if current is None:
        raise ValueError("automation does not exist")
    if current is enabled:
        return ControlChangeResult(changed=False, event=None)

    automation.enabled = enabled
    event = ControlEvent(
        automation_id=automation.id,
        event_type=(
            ControlEventType.AUTOMATION_ENABLED.value
            if enabled
            else ControlEventType.AUTOMATION_DISABLED.value
        ),
        actor=actor,
        reason=reason,
    )
    session.add(event)
    await session.flush()
    return ControlChangeResult(changed=True, event=event)


async def request_run_cancellation(
    session: AsyncSession,
    *,
    run: Run,
    reason: str,
) -> ControlChangeResult:
    """Persist one idempotent cancellation request for a non-terminal run."""
    if run.id is None:
        raise ValueError("run must be persisted before requesting cancellation")
    reason = _require_reason(reason)
    requested_at = await session.scalar(
        select(Run.cancellation_requested_at).where(Run.id == run.id)
    )
    if requested_at is not None:
        return ControlChangeResult(changed=False, event=None)
    if RunStatus(run.status) in {
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    }:
        raise InvalidRunTransitionError(
            f"cannot request cancellation for terminal run {run.status}"
        )

    run.cancellation_requested_at = _utcnow()
    run.cancellation_reason = reason
    event = ControlEvent(
        run_id=run.id,
        event_type=ControlEventType.CANCELLATION_REQUESTED.value,
        reason=reason,
    )
    session.add(event)
    await session.flush()
    if RunStatus(run.status) is RunStatus.AWAITING_APPROVAL:
        pending_approvals = list(
            (
                await session.scalars(
                    select(Approval).where(
                        Approval.run_id == run.id,
                        Approval.status == ApprovalStatus.PENDING.value,
                    )
                )
            ).all()
        )
        now = _utcnow()
        for approval in pending_approvals:
            approval.status = ApprovalStatus.CANCELLED.value
            approval.decided_at = now
            approval.decided_by = "system"
            approval.decision_reason = reason
            session.add(
                ApprovalEvent(
                    approval_id=approval.id,
                    from_status=ApprovalStatus.PENDING.value,
                    to_status=ApprovalStatus.CANCELLED.value,
                    actor="system",
                    reason=reason,
                )
            )
    if RunStatus(run.status) in {
        RunStatus.QUEUED,
        RunStatus.AWAITING_APPROVAL,
    }:
        await transition_run(
            session,
            run,
            RunStatus.CANCELLED,
            note="queued run cancelled by operator request",
        )
    return ControlChangeResult(changed=True, event=event)


async def get_execution_control_state(
    session: AsyncSession,
    *,
    run_id: int,
) -> ExecutionControlState:
    """Read fresh cancellation and kill-switch state directly from the database."""
    row = (
        await session.execute(
            select(
                Run.cancellation_requested_at,
                Automation.kill_switch_active,
            )
            .join(Automation, Automation.id == Run.automation_id)
            .where(Run.id == run_id)
        )
    ).one_or_none()
    if row is None:
        raise ValueError("run does not exist")
    return ExecutionControlState(
        cancellation_requested=row.cancellation_requested_at is not None,
        kill_switch_active=bool(row.kill_switch_active),
    )
