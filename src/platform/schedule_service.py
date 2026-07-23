"""Persistent, operator-controlled cron schedules for local automations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterError, croniter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.platform.models import (
    Automation,
    Schedule,
    ScheduleEvent,
    ScheduleEventType,
    ScheduleStatus,
)
from src.platform.run_service import IdempotencyConflictError


class ScheduleValidationError(ValueError):
    """Raised when a cron definition or timezone is unsafe or invalid."""


class ScheduleControlError(ValueError):
    """Raised when an operator action cannot safely change a schedule."""


@dataclass(frozen=True, slots=True)
class ScheduleCreationResult:
    """Result of an idempotent schedule registration."""

    schedule: Schedule
    created: bool


@dataclass(frozen=True, slots=True)
class ScheduleChangeResult:
    """Result of an idempotent schedule state change."""

    schedule: Schedule
    changed: bool


def _utcnow_aware() -> datetime:
    return datetime.now(UTC)


def _require_text(value: str, field: str, *, max_length: int | None = None) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} must not be empty")
    if max_length is not None and len(normalized) > max_length:
        raise ValueError(f"{field} exceeds {max_length} characters")
    return normalized


def _require_aware(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value


def _as_utc_naive(value: datetime) -> datetime:
    return _require_aware(value, "datetime").astimezone(UTC).replace(tzinfo=None)


def normalize_cron_expression(value: str) -> str:
    """Return one strictly validated, five-field POSIX cron expression."""
    normalized = " ".join(value.split())
    if len(normalized) > 100:
        raise ScheduleValidationError("cron_expression exceeds 100 characters")
    if len(normalized.split(" ")) != 5:
        raise ScheduleValidationError(
            "cron_expression must contain exactly five POSIX fields"
        )
    try:
        valid = croniter.is_valid(normalized, strict=True)
    except CroniterError as exc:
        raise ScheduleValidationError("cron_expression is invalid") from exc
    if not valid:
        raise ScheduleValidationError("cron_expression is invalid")
    return normalized


def normalize_timezone(value: str) -> tuple[str, ZoneInfo]:
    """Return one canonical IANA timezone name and its ZoneInfo instance."""
    normalized = _require_text(value, "timezone", max_length=100)
    try:
        timezone = ZoneInfo(normalized)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ScheduleValidationError("timezone must be a valid IANA name") from exc
    return timezone.key, timezone


def calculate_next_run_at(
    cron_expression: str,
    timezone_name: str,
    *,
    after: datetime,
) -> datetime:
    """Calculate the next occurrence and return it as a naive UTC database value."""
    expression = normalize_cron_expression(cron_expression)
    _, timezone = normalize_timezone(timezone_name)
    local_after = _require_aware(after, "after").astimezone(timezone)
    try:
        next_local = cast(
            datetime,
            croniter(expression, local_after).get_next(datetime),
        )
    except CroniterError as exc:
        raise ScheduleValidationError(
            "cron_expression has no calculable next occurrence"
        ) from exc
    if next_local.tzinfo is None or next_local.utcoffset() is None:
        raise ScheduleValidationError("cron calculation returned a timezone-naive value")
    return next_local.astimezone(UTC).replace(tzinfo=None)


async def get_or_create_schedule(
    session: AsyncSession,
    *,
    automation: Automation,
    name: str,
    cron_expression: str,
    timezone_name: str,
    actor: str,
    reason: str,
    input_payload: dict[str, object] | None = None,
    allow_overlap: bool = False,
    misfire_grace_seconds: int = 300,
    now: datetime | None = None,
) -> ScheduleCreationResult:
    """Persist one validated schedule in the disabled state, or return its match."""
    if automation.id is None:
        raise ValueError("automation must be persisted before creating a schedule")
    name = _require_text(name, "name", max_length=200)
    actor = _require_text(actor, "actor", max_length=200)
    reason = _require_text(reason, "reason")
    expression = normalize_cron_expression(cron_expression)
    timezone_key, _ = normalize_timezone(timezone_name)
    if misfire_grace_seconds < 0:
        raise ValueError("misfire_grace_seconds must not be negative")
    current_time = _require_aware(now or _utcnow_aware(), "now")
    calculate_next_run_at(
        expression,
        timezone_key,
        after=current_time,
    )
    payload = dict(input_payload or {})

    existing = await session.scalar(
        select(Schedule).where(
            Schedule.automation_id == automation.id,
            Schedule.name == name,
        )
    )
    expected = (
        expression,
        timezone_key,
        payload,
        allow_overlap,
        misfire_grace_seconds,
    )
    if existing is not None:
        actual = (
            existing.cron_expression,
            existing.timezone,
            existing.input_payload,
            existing.allow_overlap,
            existing.misfire_grace_seconds,
        )
        if actual != expected:
            raise IdempotencyConflictError(
                "schedule name already exists with different inputs"
            )
        return ScheduleCreationResult(existing, created=False)

    occurred_at = _as_utc_naive(current_time)
    schedule = Schedule(
        automation_id=automation.id,
        name=name,
        cron_expression=expression,
        timezone=timezone_key,
        input_payload=payload,
        status=ScheduleStatus.DISABLED.value,
        allow_overlap=allow_overlap,
        misfire_grace_seconds=misfire_grace_seconds,
        next_run_at=None,
        created_at=occurred_at,
        updated_at=occurred_at,
    )
    session.add(schedule)
    await session.flush()
    session.add(
        ScheduleEvent(
            schedule_id=schedule.id,
            event_type=ScheduleEventType.CREATED.value,
            from_status=None,
            to_status=ScheduleStatus.DISABLED.value,
            actor=actor,
            reason=reason,
            occurred_at=occurred_at,
        )
    )
    await session.flush()
    return ScheduleCreationResult(schedule, created=True)


async def set_schedule_enabled(
    session: AsyncSession,
    *,
    schedule: Schedule,
    enabled: bool,
    actor: str,
    reason: str,
    now: datetime | None = None,
) -> ScheduleChangeResult:
    """Apply one explicit, audited, idempotent schedule state change."""
    if schedule.id is None:
        raise ValueError("schedule must be persisted before changing its state")
    actor = _require_text(actor, "actor", max_length=200)
    reason = _require_text(reason, "reason")
    current = ScheduleStatus(schedule.status)
    target = ScheduleStatus.ENABLED if enabled else ScheduleStatus.DISABLED
    if current is target:
        return ScheduleChangeResult(schedule, changed=False)

    current_time = _require_aware(now or _utcnow_aware(), "now")
    automation = await session.get(Automation, schedule.automation_id)
    if automation is None:
        raise ScheduleControlError("schedule automation does not exist")
    if enabled:
        if not automation.enabled:
            raise ScheduleControlError("cannot enable a schedule for a disabled automation")
        if automation.kill_switch_active:
            raise ScheduleControlError("cannot enable a schedule while kill switch is active")
        next_run_at = calculate_next_run_at(
            schedule.cron_expression,
            schedule.timezone,
            after=current_time,
        )
        event_type = ScheduleEventType.ENABLED
    else:
        next_run_at = None
        event_type = ScheduleEventType.DISABLED

    previous_next_run_at = schedule.next_run_at
    occurred_at = _as_utc_naive(current_time)
    schedule.status = target.value
    schedule.next_run_at = next_run_at
    schedule.updated_at = occurred_at
    session.add(
        ScheduleEvent(
            schedule_id=schedule.id,
            event_type=event_type.value,
            from_status=current.value,
            to_status=target.value,
            actor=actor,
            reason=reason,
            previous_next_run_at=previous_next_run_at,
            next_run_at=next_run_at,
            occurred_at=occurred_at,
        )
    )
    await session.flush()
    return ScheduleChangeResult(schedule, changed=True)
