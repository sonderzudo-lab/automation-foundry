"""Durable reconciliation from redacted health snapshots to platform alerts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import Settings, settings
from src.operations.health import HealthReport, HealthStatus
from src.platform.alert_service import record_alert_occurrence, transition_alert
from src.platform.models import (
    Alert,
    AlertSeverity,
    AlertStatus,
    Automation,
    HealthCondition,
)
from src.platform.run_service import get_or_create_automation

_MONITOR_SLUG = "platform-health"
_MONITOR_NAME = "Platform Health Monitor"
_MONITOR_OWNER = "local-system"
_MONITOR_SOURCE = "health-monitor"
_EPOCH = datetime(1970, 1, 1)
_ALERT_TITLES = {
    "cpu": "CPU health degraded",
    "memory": "Memory health degraded",
    "disk": "Storage health degraded",
    "machine": "Machine health degraded",
    "database": "Database health degraded",
    "redis": "Redis health degraded",
    "workers": "Worker health degraded",
    "beat": "Beat health degraded",
    "gpu": "GPU health degraded",
}


@dataclass(frozen=True, slots=True)
class HealthReconciliationResult:
    """Bounded summary of one idempotent health reconciliation."""

    observed: int
    occurrences_recorded: int
    alerts_resolved: int
    inconclusive: int
    ignored: int
    replayed: int


async def reconcile_health_report(
    session: AsyncSession,
    report: HealthReport,
    *,
    configuration: Settings = settings,
) -> HealthReconciliationResult:
    """Persist consecutive states and reconcile allowlisted platform alerts."""
    observed_at = _as_utc_naive(report.checked_at)
    automation = (
        await get_or_create_automation(
            session,
            slug=_MONITOR_SLUG,
            name=_MONITOR_NAME,
            owner=_MONITOR_OWNER,
        )
    ).automation
    counters = {
        "observed": 0,
        "occurrences_recorded": 0,
        "alerts_resolved": 0,
        "inconclusive": 0,
        "ignored": 0,
        "replayed": 0,
    }

    for check in report.checks:
        title = _ALERT_TITLES.get(check.name)
        if title is None:
            counters["ignored"] += 1
            continue
        condition = await _lock_or_create_condition(
            session,
            check_name=check.name,
            now=observed_at,
        )
        if observed_at <= condition.last_observed_at:
            counters["replayed"] += 1
            continue

        status = HealthStatus(check.status)
        condition.last_status = status.value
        condition.last_observed_at = observed_at
        condition.updated_at = observed_at
        counters["observed"] += 1

        if status is HealthStatus.SKIP:
            counters["inconclusive"] += 1
            continue
        if status is HealthStatus.PASS:
            condition.consecutive_unhealthy = 0
            condition.first_unhealthy_at = None
            condition.consecutive_healthy = min(
                condition.consecutive_healthy + 1,
                configuration.health_alert_recovery_threshold,
            )
            if (
                condition.consecutive_healthy
                >= configuration.health_alert_recovery_threshold
                and await _resolve_active_alert(
                    session,
                    automation=automation,
                    check_name=check.name,
                    observed_at=observed_at,
                    recovery_threshold=configuration.health_alert_recovery_threshold,
                )
            ):
                counters["alerts_resolved"] += 1
            continue

        condition.consecutive_healthy = 0
        if condition.consecutive_unhealthy == 0:
            condition.first_unhealthy_at = observed_at
        condition.consecutive_unhealthy = min(
            condition.consecutive_unhealthy + 1,
            configuration.health_alert_failure_threshold,
        )
        if (
            condition.consecutive_unhealthy
            >= configuration.health_alert_failure_threshold
        ):
            occurrence = await record_alert_occurrence(
                session,
                automation=automation,
                deduplication_key=_deduplication_key(check.name),
                idempotency_key=_occurrence_key(check.name, observed_at),
                title=title,
                summary=(
                    f"{check.name} reported {status.value} for "
                    f"{configuration.health_alert_failure_threshold} consecutive "
                    "observations."
                ),
                severity=(
                    AlertSeverity.WARNING
                    if status is HealthStatus.DEGRADED
                    else AlertSeverity.ERROR
                ),
                source=_MONITOR_SOURCE,
                observed_at=observed_at.replace(tzinfo=UTC),
                now=observed_at.replace(tzinfo=UTC),
            )
            if occurrence.recorded:
                counters["occurrences_recorded"] += 1

    return HealthReconciliationResult(**counters)


async def _lock_or_create_condition(
    session: AsyncSession,
    *,
    check_name: str,
    now: datetime,
) -> HealthCondition:
    statement = (
        select(HealthCondition)
        .where(HealthCondition.check_name == check_name)
        .with_for_update()
    )
    condition = await session.scalar(statement)
    if condition is not None:
        return condition

    candidate = HealthCondition(
        check_name=check_name,
        last_status=HealthStatus.SKIP.value,
        consecutive_unhealthy=0,
        consecutive_healthy=0,
        first_unhealthy_at=None,
        last_observed_at=_EPOCH,
        updated_at=now,
    )
    try:
        async with session.begin_nested():
            session.add(candidate)
            await session.flush()
        return candidate
    except IntegrityError:
        condition = await session.scalar(statement)
        if condition is None:
            raise
        assert isinstance(condition, HealthCondition)
        return condition


async def _resolve_active_alert(
    session: AsyncSession,
    *,
    automation: Automation,
    check_name: str,
    observed_at: datetime,
    recovery_threshold: int,
) -> bool:
    alert = await session.scalar(
        select(Alert)
        .where(
            Alert.automation_id == automation.id,
            Alert.deduplication_key == _deduplication_key(check_name),
            Alert.status != AlertStatus.RESOLVED.value,
        )
        .with_for_update()
    )
    if alert is None:
        return False
    result = await transition_alert(
        session,
        alert=alert,
        target=AlertStatus.RESOLVED,
        actor=_MONITOR_SOURCE,
        reason=(
            f"{check_name} recovered for {recovery_threshold} consecutive "
            "observations."
        ),
        now=observed_at.replace(tzinfo=UTC),
    )
    return result.changed


def _as_utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _deduplication_key(check_name: str) -> str:
    return f"health:{check_name}"


def _occurrence_key(check_name: str, observed_at: datetime) -> str:
    stamp = observed_at.strftime("%Y%m%dT%H%M%S.%f")
    return f"health:{check_name}:{stamp}"
