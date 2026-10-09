"""Database-authoritative scheduler tick feeding durable run dispatches."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.platform.dispatch_service import (
    DispatchPublishError,
    Publish,
    prepare_registered_dispatch,
    publish_prepared_dispatch,
)
from src.platform.executor_registry import (
    AutomationExecutorNotFoundError,
    get_automation_executor,
)
from src.platform.models import (
    Automation,
    DispatchStatus,
    Run,
    RunStatus,
    Schedule,
    ScheduleOccurrence,
    ScheduleOccurrenceStatus,
    ScheduleStatus,
)
from src.platform.schedule_service import calculate_next_run_at

_ACTIVE_RUN_STATUSES = (
    RunStatus.QUEUED.value,
    RunStatus.RUNNING.value,
    RunStatus.AWAITING_APPROVAL.value,
)


@dataclass(frozen=True, slots=True)
class ScheduleTickResult:
    due_seen: int
    prepared: int
    skipped: int
    published: int
    publish_failed: int


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _aware_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC)


async def dispatch_due_schedules(
    session: AsyncSession,
    *,
    publish: Publish,
    now: datetime | None = None,
    batch_size: int = 100,
) -> ScheduleTickResult:
    """Prepare due occurrences and retry pending publications idempotently."""
    if batch_size < 1 or batch_size > 1000:
        raise ValueError("batch_size must be between 1 and 1000")
    current = now or _utcnow()
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    now_naive = current.astimezone(UTC).replace(tzinfo=None)

    schedules = list(
        (
            await session.scalars(
                select(Schedule)
                .where(
                    Schedule.status == ScheduleStatus.ENABLED.value,
                    Schedule.next_run_at <= now_naive,
                )
                .order_by(Schedule.next_run_at, Schedule.id)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
        ).all()
    )
    prepared = 0
    skipped = 0
    for schedule in schedules:
        scheduled_for = schedule.next_run_at
        if scheduled_for is None:
            continue
        automation = await session.get(Automation, schedule.automation_id)
        if automation is None:
            raise ValueError("schedule automation does not exist")

        reason = await _skip_reason(
            session,
            schedule=schedule,
            automation=automation,
            scheduled_for=scheduled_for,
            now=now_naive,
        )
        dispatch_id: int | None = None
        run_id: int | None = None
        if reason is None:
            key = f"schedule:{schedule.id}:{scheduled_for.isoformat()}"
            try:
                run_input = _occurrence_input(
                    automation.slug,
                    schedule.input_payload,
                    scheduled_for,
                )
                dispatch = await prepare_registered_dispatch(
                    session,
                    slug=automation.slug,
                    idempotency_key=key,
                    trigger="schedule",
                    input_payload=run_input,
                )
            except AutomationExecutorNotFoundError:
                reason = "EXECUTOR_NOT_REGISTERED"
            except ValueError:
                reason = "INVALID_SCHEDULE_INPUT"
            else:
                dispatch_id = dispatch.dispatch_id
                run_id = dispatch.run_id

        if reason is None:
            occurrence_status = ScheduleOccurrenceStatus.PENDING
            prepared += 1
            schedule.last_enqueued_at = now_naive
        else:
            occurrence_status = ScheduleOccurrenceStatus.SKIPPED
            skipped += 1
        session.add(
            ScheduleOccurrence(
                schedule_id=schedule.id,
                run_id=run_id,
                dispatch_id=dispatch_id,
                scheduled_for=scheduled_for,
                status=occurrence_status.value,
                reason_code=reason,
                created_at=now_naive,
            )
        )
        schedule.next_run_at = calculate_next_run_at(
            schedule.cron_expression,
            schedule.timezone,
            after=_aware_utc(scheduled_for),
        )
        schedule.updated_at = now_naive
    await session.commit()

    published, publish_failed = await _publish_pending(
        session,
        publish=publish,
        now=now_naive,
        batch_size=batch_size,
    )
    return ScheduleTickResult(
        due_seen=len(schedules),
        prepared=prepared,
        skipped=skipped,
        published=published,
        publish_failed=publish_failed,
    )


def _occurrence_input(
    slug: str,
    payload: dict[str, object] | None,
    scheduled_for: datetime,
) -> dict[str, object] | None:
    """Let the executor derive the run input from the occurrence time, if it declares so."""
    resolver = get_automation_executor(slug).resolve_scheduled_input
    if resolver is None:
        return payload
    return resolver(dict(payload or {}), scheduled_for)


async def _skip_reason(
    session: AsyncSession,
    *,
    schedule: Schedule,
    automation: Automation,
    scheduled_for: datetime,
    now: datetime,
) -> str | None:
    if not automation.enabled:
        return "AUTOMATION_DISABLED"
    if automation.kill_switch_active:
        return "KILL_SWITCH_ACTIVE"
    if now > scheduled_for + timedelta(seconds=schedule.misfire_grace_seconds):
        return "MISFIRE_GRACE_EXCEEDED"
    if schedule.allow_overlap:
        return None
    active = await session.scalar(
        select(Run.id)
        .join(ScheduleOccurrence, ScheduleOccurrence.run_id == Run.id)
        .where(
            ScheduleOccurrence.schedule_id == schedule.id,
            Run.status.in_(_ACTIVE_RUN_STATUSES),
        )
        .limit(1)
    )
    return "OVERLAP_BLOCKED" if active is not None else None


async def _publish_pending(
    session: AsyncSession,
    *,
    publish: Publish,
    now: datetime,
    batch_size: int,
) -> tuple[int, int]:
    occurrences = list(
        (
            await session.scalars(
                select(ScheduleOccurrence)
                .where(
                    ScheduleOccurrence.status
                    == ScheduleOccurrenceStatus.PENDING.value
                )
                .order_by(ScheduleOccurrence.created_at, ScheduleOccurrence.id)
                .limit(batch_size)
            )
        ).all()
    )
    published = 0
    failed = 0
    for occurrence in occurrences:
        if occurrence.dispatch_id is None:
            raise ValueError("pending schedule occurrence has no dispatch")
        try:
            status = await publish_prepared_dispatch(
                session,
                dispatch_id=occurrence.dispatch_id,
                publish=publish,
                only_if_pending=True,
            )
        except DispatchPublishError:
            failed += 1
            continue
        if status is DispatchStatus.PENDING:
            continue
        refreshed = await session.get(ScheduleOccurrence, occurrence.id)
        if refreshed is None:
            raise ValueError("schedule occurrence disappeared during publication")
        refreshed.status = ScheduleOccurrenceStatus.PUBLISHED.value
        refreshed.published_at = now
        await session.commit()
        published += 1
    return published, failed
