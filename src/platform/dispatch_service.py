"""Durable broker dispatch with stable delivery identity and leased claims."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.platform.executor_registry import get_automation_executor
from src.platform.models import (
    Automation,
    DispatchEventType,
    DispatchStatus,
    QueueClass,
    Run,
    RunDispatch,
    RunDispatchEvent,
    RunStatus,
)

DISPATCH_TASK_NAME = "automation_foundry.dispatch.execute"
Publish = Callable[[int, str, QueueClass], None]


class DispatchPublishError(RuntimeError):
    """Raised after a broker publish failure has been safely persisted."""


class DispatchClaimError(ValueError):
    """Raised when claim ownership does not match a terminal update."""


@dataclass(frozen=True, slots=True)
class DispatchPreparationResult:
    run_id: int
    dispatch_id: int
    delivery_id: str
    queue: QueueClass
    created: bool


@dataclass(frozen=True, slots=True)
class DispatchClaimResult:
    acquired: bool
    terminal: bool
    claim_token: str | None
    retry_after_seconds: int | None
    run_id: int


@dataclass(frozen=True, slots=True)
class DispatchRequeueResult:
    """Durable wake-up state for a run that gained approved continuation work."""

    dispatch_id: int
    queue: QueueClass
    should_publish: bool
    changed: bool


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _persisted_id(value: int | None, kind: str) -> int:
    if not isinstance(value, int):
        raise ValueError(f"{kind} must be persisted")
    return value


def _event(
    dispatch: RunDispatch,
    event_type: DispatchEventType,
    *,
    from_status: DispatchStatus | None,
    to_status: DispatchStatus,
    actor: str,
    reason_code: str | None = None,
) -> RunDispatchEvent:
    return RunDispatchEvent(
        dispatch_id=_persisted_id(dispatch.id, "dispatch"),
        event_type=event_type.value,
        from_status=from_status.value if from_status is not None else None,
        to_status=to_status.value,
        actor=actor,
        reason_code=reason_code,
    )


async def prepare_registered_dispatch(
    session: AsyncSession,
    *,
    slug: str,
    idempotency_key: str,
    experiment_id: int | None = None,
    trigger: str = "manual",
    input_payload: dict[str, object] | None = None,
    retry_of_run_id: int | None = None,
    retry_requested_by: str | None = None,
    retry_reason: str | None = None,
) -> DispatchPreparationResult:
    """Prepare a run and its durable delivery before any broker side effect."""
    executor = get_automation_executor(slug)
    if not executor.supports_manual_start:
        raise ValueError("automation does not support manual dispatch")
    run_result = await executor.prepare(
        session,
        idempotency_key=idempotency_key,
        experiment_id=experiment_id,
        trigger=trigger,
        input_payload=input_payload,
        retry_of_run_id=retry_of_run_id,
        retry_requested_by=retry_requested_by,
        retry_reason=retry_reason,
    )
    run_id = _persisted_id(run_result.run.id, "run")
    existing = await session.scalar(
        select(RunDispatch).where(RunDispatch.run_id == run_id)
    )
    if existing is not None:
        return _preparation_result(existing, executor.queue, created=False)

    delivery_id = str(uuid5(NAMESPACE_URL, f"automation-foundry:run:{run_id}"))
    dispatch = RunDispatch(
        run_id=run_id,
        queue=executor.queue.value,
        task_name=DISPATCH_TASK_NAME,
        delivery_id=delivery_id,
        status=DispatchStatus.PENDING.value,
    )
    try:
        async with session.begin_nested():
            session.add(dispatch)
            await session.flush()
            session.add(
                _event(
                    dispatch,
                    DispatchEventType.PREPARED,
                    from_status=None,
                    to_status=DispatchStatus.PENDING,
                    actor="control-plane",
                )
            )
            await session.flush()
    except IntegrityError:
        existing = await session.scalar(
            select(RunDispatch).where(RunDispatch.run_id == run_id)
        )
        if existing is None:
            raise
        return _preparation_result(existing, executor.queue, created=False)
    return _preparation_result(dispatch, executor.queue, created=True)


def _preparation_result(
    dispatch: RunDispatch,
    queue: QueueClass,
    *,
    created: bool,
) -> DispatchPreparationResult:
    if dispatch.queue != queue.value or dispatch.task_name != DISPATCH_TASK_NAME:
        raise ValueError("run dispatch exists with different routing metadata")
    return DispatchPreparationResult(
        run_id=dispatch.run_id,
        dispatch_id=_persisted_id(dispatch.id, "dispatch"),
        delivery_id=dispatch.delivery_id,
        queue=queue,
        created=created,
    )


async def publish_registered_dispatch(
    session: AsyncSession,
    *,
    slug: str,
    idempotency_key: str,
    publish: Publish,
    experiment_id: int | None = None,
    trigger: str = "manual",
    input_payload: dict[str, object] | None = None,
    retry_of_run_id: int | None = None,
    retry_requested_by: str | None = None,
    retry_reason: str | None = None,
) -> DispatchPreparationResult:
    """Commit preparation, publish, then persist the observable outcome."""
    prepared = await prepare_registered_dispatch(
        session,
        slug=slug,
        idempotency_key=idempotency_key,
        experiment_id=experiment_id,
        trigger=trigger,
        input_payload=input_payload,
        retry_of_run_id=retry_of_run_id,
        retry_requested_by=retry_requested_by,
        retry_reason=retry_reason,
    )
    await session.commit()
    await publish_prepared_dispatch(
        session,
        dispatch_id=prepared.dispatch_id,
        publish=publish,
        only_if_pending=False,
    )
    return prepared


async def publish_prepared_dispatch(
    session: AsyncSession,
    *,
    dispatch_id: int,
    publish: Publish,
    only_if_pending: bool = True,
) -> DispatchStatus:
    """Publish an already durable dispatch and persist the broker outcome."""
    await session.commit()
    dispatch = await _locked_dispatch(session, dispatch_id)
    current = DispatchStatus(dispatch.status)
    if only_if_pending and current is not DispatchStatus.PENDING:
        return current
    delivery_id = dispatch.delivery_id
    queue = QueueClass(dispatch.queue)
    await session.commit()
    try:
        publish(dispatch_id, delivery_id, queue)
    except Exception as exc:
        dispatch = await _locked_dispatch(session, dispatch_id)
        current = DispatchStatus(dispatch.status)
        dispatch.publish_attempts += 1
        dispatch.last_error_code = "BROKER_PUBLISH_FAILED"
        dispatch.updated_at = _utcnow()
        session.add(
            _event(
                dispatch,
                DispatchEventType.PUBLISH_FAILED,
                from_status=current,
                to_status=current,
                actor="control-plane",
                reason_code="BROKER_PUBLISH_FAILED",
            )
        )
        await session.commit()
        raise DispatchPublishError("broker publish failed; dispatch remains retryable") from exc

    dispatch = await _locked_dispatch(session, dispatch_id)
    current = DispatchStatus(dispatch.status)
    dispatch.publish_attempts += 1
    dispatch.last_error_code = None
    dispatch.published_at = dispatch.published_at or _utcnow()
    if current is DispatchStatus.PENDING:
        dispatch.status = DispatchStatus.PUBLISHED.value
        target = DispatchStatus.PUBLISHED
    else:
        target = current
    dispatch.updated_at = _utcnow()
    session.add(
        _event(
            dispatch,
            DispatchEventType.PUBLISH_SUCCEEDED,
            from_status=current,
            to_status=target,
            actor="control-plane",
        )
    )
    await session.commit()
    return target


async def requeue_completed_run_dispatch(
    session: AsyncSession,
    *,
    run_id: int,
    actor: str = "control-plane",
) -> DispatchRequeueResult:
    """Wake a completed run dispatch after a durable human approval."""
    run = await session.get(Run, run_id)
    if run is None:
        raise ValueError("dispatch run does not exist")
    if RunStatus(run.status) is not RunStatus.RUNNING:
        raise ValueError("only a running run can requeue approved continuation work")
    dispatch = await session.scalar(
        select(RunDispatch)
        .where(RunDispatch.run_id == run_id)
        .with_for_update()
    )
    if dispatch is None:
        raise ValueError("run has no durable dispatch to requeue")
    current = DispatchStatus(dispatch.status)
    queue = QueueClass(dispatch.queue)
    if current is DispatchStatus.PENDING:
        return DispatchRequeueResult(
            _persisted_id(dispatch.id, "dispatch"),
            queue,
            should_publish=True,
            changed=False,
        )
    if current in {DispatchStatus.PUBLISHED, DispatchStatus.CLAIMED}:
        return DispatchRequeueResult(
            _persisted_id(dispatch.id, "dispatch"),
            queue,
            should_publish=False,
            changed=False,
        )
    if current is DispatchStatus.FAILED:
        raise ValueError("failed run dispatch cannot be requeued as an approval continuation")

    dispatch.status = DispatchStatus.PENDING.value
    dispatch.claimed_by = None
    dispatch.claim_token = None
    dispatch.lease_expires_at = None
    dispatch.finished_at = None
    dispatch.last_error_code = None
    dispatch.updated_at = _utcnow()
    session.add(
        _event(
            dispatch,
            DispatchEventType.REQUEUED,
            from_status=DispatchStatus.COMPLETED,
            to_status=DispatchStatus.PENDING,
            actor=actor.strip() or "control-plane",
            reason_code="APPROVED_CONTINUATION",
        )
    )
    await session.flush()
    return DispatchRequeueResult(
        _persisted_id(dispatch.id, "dispatch"),
        queue,
        should_publish=True,
        changed=True,
    )


async def claim_dispatch(
    session: AsyncSession,
    *,
    dispatch_id: int,
    delivery_id: str,
    worker_id: str,
    lease_seconds: int,
) -> DispatchClaimResult:
    """Acquire one delivery, or report terminal/active ownership safely."""
    if lease_seconds < 1:
        raise ValueError("lease_seconds must be positive")
    dispatch = await _locked_dispatch(session, dispatch_id)
    if dispatch.delivery_id != delivery_id:
        raise DispatchClaimError("delivery identity does not match dispatch")
    current = DispatchStatus(dispatch.status)
    run_id = dispatch.run_id
    if current in {DispatchStatus.COMPLETED, DispatchStatus.FAILED}:
        return DispatchClaimResult(False, True, None, None, run_id)

    now = _utcnow()
    if (
        current is DispatchStatus.CLAIMED
        and dispatch.lease_expires_at is not None
        and dispatch.lease_expires_at > now
    ):
        remaining = max(1, int((dispatch.lease_expires_at - now).total_seconds()) + 1)
        return DispatchClaimResult(False, False, None, remaining, run_id)

    reclaimed = current is DispatchStatus.CLAIMED
    token = uuid4().hex
    dispatch.status = DispatchStatus.CLAIMED.value
    dispatch.claimed_by = worker_id.strip() or "unknown-worker"
    dispatch.claim_token = token
    dispatch.claimed_at = now
    dispatch.lease_expires_at = now + timedelta(seconds=lease_seconds)
    dispatch.updated_at = now
    session.add(
        _event(
            dispatch,
            DispatchEventType.LEASE_RECLAIMED if reclaimed else DispatchEventType.CLAIMED,
            from_status=current,
            to_status=DispatchStatus.CLAIMED,
            actor=dispatch.claimed_by,
            reason_code="LEASE_EXPIRED" if reclaimed else None,
        )
    )
    await session.commit()
    return DispatchClaimResult(True, False, token, None, run_id)


async def finish_dispatch(
    session: AsyncSession,
    *,
    dispatch_id: int,
    claim_token: str,
    succeeded: bool,
    reason_code: str | None = None,
) -> None:
    """Finish a claimed dispatch only when the lease owner proves ownership."""
    dispatch = await _locked_dispatch(session, dispatch_id)
    if DispatchStatus(dispatch.status) is not DispatchStatus.CLAIMED:
        raise DispatchClaimError("dispatch is not claimed")
    if dispatch.claim_token != claim_token:
        raise DispatchClaimError("dispatch claim token does not match")
    target = DispatchStatus.COMPLETED if succeeded else DispatchStatus.FAILED
    now = _utcnow()
    dispatch.status = target.value
    dispatch.finished_at = now
    dispatch.lease_expires_at = None
    dispatch.last_error_code = None if succeeded else (reason_code or "EXECUTION_FAILED")
    dispatch.updated_at = now
    session.add(
        _event(
            dispatch,
            DispatchEventType.COMPLETED if succeeded else DispatchEventType.FAILED,
            from_status=DispatchStatus.CLAIMED,
            to_status=target,
            actor=dispatch.claimed_by or "unknown-worker",
            reason_code=dispatch.last_error_code,
        )
    )
    await session.commit()


async def execute_claimed_dispatch(
    session: AsyncSession,
    *,
    dispatch_id: int,
    delivery_id: str,
    worker_id: str,
    lease_seconds: int,
) -> DispatchClaimResult:
    """Claim and execute a registered run; duplicate deliveries do no work."""
    claim = await claim_dispatch(
        session,
        dispatch_id=dispatch_id,
        delivery_id=delivery_id,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
    )
    if not claim.acquired:
        return claim
    token = claim.claim_token
    if token is None:
        raise DispatchClaimError("acquired claim is missing its token")
    row = (
        await session.execute(
            select(Run, Automation.slug)
            .join(Automation, Automation.id == Run.automation_id)
            .where(Run.id == claim.run_id)
        )
    ).one_or_none()
    if row is None:
        await finish_dispatch(
            session,
            dispatch_id=dispatch_id,
            claim_token=token,
            succeeded=False,
            reason_code="RUN_NOT_FOUND",
        )
        raise ValueError("dispatch run does not exist")
    run, slug = row
    executor = get_automation_executor(slug)
    try:
        result = await executor.execute(
            session,
            idempotency_key=run.idempotency_key,
            trigger=run.trigger,
            input_payload=run.input_payload,
            retry_of_run_id=run.retry_of_run_id,
            retry_requested_by=run.retry_requested_by,
            retry_reason=run.retry_reason,
            experiment_id=run.experiment_id,
        )
        if result.run_id != claim.run_id:
            raise ValueError("executor returned a different run")
    except Exception:
        await session.rollback()
        await finish_dispatch(
            session,
            dispatch_id=dispatch_id,
            claim_token=token,
            succeeded=False,
            reason_code="EXECUTION_FAILED",
        )
        raise
    await finish_dispatch(
        session,
        dispatch_id=dispatch_id,
        claim_token=token,
        succeeded=True,
    )
    return claim


async def _locked_dispatch(session: AsyncSession, dispatch_id: int) -> RunDispatch:
    dispatch = await session.scalar(
        select(RunDispatch)
        .where(RunDispatch.id == dispatch_id)
        .with_for_update()
    )
    if dispatch is None:
        raise DispatchClaimError("dispatch does not exist")
    return dispatch
