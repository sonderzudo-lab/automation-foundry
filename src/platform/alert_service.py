"""Deduplicated, audited operational alerts for the local control plane."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.platform.models import (
    Alert,
    AlertEvent,
    AlertEventType,
    AlertSeverity,
    AlertStatus,
    Automation,
    MetricPoint,
    Run,
    StepRun,
)
from src.platform.run_service import IdempotencyConflictError

_SEVERITY_RANK = {
    AlertSeverity.INFO: 0,
    AlertSeverity.WARNING: 1,
    AlertSeverity.ERROR: 2,
    AlertSeverity.CRITICAL: 3,
}


class AlertValidationError(ValueError):
    """Raised when alert input or attribution is unsafe or inconsistent."""


class AlertTransitionError(ValueError):
    """Raised when an operator requests an invalid alert transition."""


class StaleAlertOccurrenceError(ValueError):
    """Raised when an old occurrence would incorrectly reopen a treated alert."""


@dataclass(frozen=True, slots=True)
class AlertOccurrenceResult:
    """Result of recording or replaying one alert occurrence."""

    alert: Alert
    event: AlertEvent
    created: bool
    recorded: bool


@dataclass(frozen=True, slots=True)
class AlertChangeResult:
    """Result of one idempotent operator alert state change."""

    alert: Alert
    event: AlertEvent | None
    changed: bool


def _utcnow_aware() -> datetime:
    return datetime.now(UTC)


def _require_text(value: str, field: str, *, max_length: int | None = None) -> str:
    normalized = value.strip()
    if not normalized:
        raise AlertValidationError(f"{field} must not be empty")
    if max_length is not None and len(normalized) > max_length:
        raise AlertValidationError(f"{field} exceeds {max_length} characters")
    return normalized


def _as_utc_naive(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise AlertValidationError(f"{field} must be timezone-aware")
    return value.astimezone(UTC).replace(tzinfo=None)


def _scope_ids(
    *,
    automation: Automation,
    run: Run | None,
    step_run: StepRun | None,
    metric_point: MetricPoint | None,
) -> tuple[int, int | None, int | None, int | None]:
    if automation.id is None:
        raise AlertValidationError(
            "automation must be persisted before recording an alert"
        )
    run_id = None
    step_run_id = None
    metric_point_id = None
    if run is not None:
        if run.id is None:
            raise AlertValidationError("run must be persisted before recording an alert")
        if run.automation_id != automation.id:
            raise AlertValidationError("run does not belong to alert automation")
        run_id = run.id
    if step_run is not None:
        if run is None:
            raise AlertValidationError("step alert requires its parent run")
        if step_run.id is None:
            raise AlertValidationError(
                "step run must be persisted before recording an alert"
            )
        if step_run.run_id != run.id:
            raise AlertValidationError("step run does not belong to alert run")
        step_run_id = step_run.id
    if metric_point is not None:
        if metric_point.id is None:
            raise AlertValidationError(
                "metric point must be persisted before recording an alert"
            )
        if metric_point.automation_id != automation.id:
            raise AlertValidationError(
                "metric point does not belong to alert automation"
            )
        if run_id is not None and metric_point.run_id != run_id:
            raise AlertValidationError("metric point does not belong to alert run")
        if step_run_id is not None and metric_point.step_run_id != step_run_id:
            raise AlertValidationError("metric point does not belong to alert step run")
        run_id = metric_point.run_id
        step_run_id = metric_point.step_run_id
        metric_point_id = metric_point.id
    return automation.id, run_id, step_run_id, metric_point_id


def _higher_severity(current: str, incoming: AlertSeverity) -> str:
    current_severity = AlertSeverity(current)
    if _SEVERITY_RANK[incoming] > _SEVERITY_RANK[current_severity]:
        return incoming.value
    return current_severity.value


async def record_alert_occurrence(
    session: AsyncSession,
    *,
    automation: Automation,
    deduplication_key: str,
    idempotency_key: str,
    title: str,
    summary: str,
    severity: AlertSeverity,
    source: str,
    run: Run | None = None,
    step_run: StepRun | None = None,
    metric_point: MetricPoint | None = None,
    observed_at: datetime | None = None,
    now: datetime | None = None,
) -> AlertOccurrenceResult:
    """Record one retry-safe occurrence, creating or reopening its alert."""
    automation_id, run_id, step_run_id, metric_point_id = _scope_ids(
        automation=automation,
        run=run,
        step_run=step_run,
        metric_point=metric_point,
    )
    deduplication_key = _require_text(
        deduplication_key,
        "deduplication_key",
        max_length=255,
    )
    idempotency_key = _require_text(
        idempotency_key,
        "idempotency_key",
        max_length=255,
    )
    title = _require_text(title, "title", max_length=200)
    summary = _require_text(summary, "summary")
    source = _require_text(source, "source", max_length=200)
    try:
        alert_severity = AlertSeverity(severity)
    except ValueError as exc:
        raise AlertValidationError("severity is not supported") from exc
    recorded_at = _as_utc_naive(now or _utcnow_aware(), "now")
    occurrence_at = (
        _as_utc_naive(observed_at, "observed_at")
        if observed_at is not None
        else recorded_at
    )
    if occurrence_at > recorded_at:
        raise AlertValidationError("observed_at cannot be later than the recording time")

    alert = await session.scalar(
        select(Alert).where(
            Alert.automation_id == automation_id,
            Alert.deduplication_key == deduplication_key,
        )
    )
    if alert is None:
        alert = Alert(
            automation_id=automation_id,
            run_id=run_id,
            step_run_id=step_run_id,
            metric_point_id=metric_point_id,
            deduplication_key=deduplication_key,
            title=title,
            summary=summary,
            severity=alert_severity.value,
            status=AlertStatus.OPEN.value,
            source=source,
            occurrence_count=1,
            first_seen_at=occurrence_at,
            last_seen_at=occurrence_at,
            created_at=recorded_at,
            updated_at=recorded_at,
        )
        session.add(alert)
        await session.flush()
        event = AlertEvent(
            alert_id=alert.id,
            idempotency_key=idempotency_key,
            event_type=AlertEventType.OPENED.value,
            from_status=None,
            to_status=AlertStatus.OPEN.value,
            severity=alert_severity.value,
            actor=source,
            reason=summary,
            occurred_at=occurrence_at,
        )
        session.add(event)
        await session.flush()
        return AlertOccurrenceResult(alert, event, created=True, recorded=True)

    stable_expected = (
        run_id,
        step_run_id,
        metric_point_id,
        title,
        source,
    )
    stable_actual = (
        alert.run_id,
        alert.step_run_id,
        alert.metric_point_id,
        alert.title,
        alert.source,
    )
    if stable_actual != stable_expected:
        raise IdempotencyConflictError(
            "alert deduplication key already exists with different attribution"
        )

    existing_event = await session.scalar(
        select(AlertEvent).where(
            AlertEvent.alert_id == alert.id,
            AlertEvent.idempotency_key == idempotency_key,
        )
    )
    if existing_event is not None:
        expected = (alert_severity.value, source, summary)
        actual = (
            existing_event.severity,
            existing_event.actor,
            existing_event.reason,
        )
        if actual != expected or (
            observed_at is not None and existing_event.occurred_at != occurrence_at
        ):
            raise IdempotencyConflictError(
                "alert occurrence idempotency key already exists with different inputs"
            )
        return AlertOccurrenceResult(
            alert,
            existing_event,
            created=False,
            recorded=False,
        )

    current_status = AlertStatus(alert.status)
    if current_status is AlertStatus.ACKNOWLEDGED:
        if alert.acknowledged_at is None:
            raise AlertValidationError("acknowledged alert is missing its timestamp")
        if occurrence_at <= alert.acknowledged_at:
            raise StaleAlertOccurrenceError(
                "occurrence predates the alert acknowledgement"
            )
    elif current_status is AlertStatus.RESOLVED:
        if alert.resolved_at is None:
            raise AlertValidationError("resolved alert is missing its timestamp")
        if occurrence_at <= alert.resolved_at:
            raise StaleAlertOccurrenceError("occurrence predates the alert resolution")

    if current_status is AlertStatus.OPEN:
        event_type = AlertEventType.OCCURRED
        next_severity = _higher_severity(alert.severity, alert_severity)
    else:
        event_type = AlertEventType.REOPENED
        next_severity = alert_severity.value
        alert.status = AlertStatus.OPEN.value
        alert.acknowledged_at = None
        alert.acknowledged_by = None
        alert.acknowledgement_reason = None
        alert.resolved_at = None
        alert.resolved_by = None
        alert.resolution_reason = None

    if occurrence_at >= alert.last_seen_at:
        alert.summary = summary
    alert.severity = next_severity
    alert.occurrence_count += 1
    alert.first_seen_at = min(alert.first_seen_at, occurrence_at)
    alert.last_seen_at = max(alert.last_seen_at, occurrence_at)
    alert.updated_at = recorded_at
    event = AlertEvent(
        alert_id=alert.id,
        idempotency_key=idempotency_key,
        event_type=event_type.value,
        from_status=current_status.value,
        to_status=AlertStatus.OPEN.value,
        severity=alert_severity.value,
        actor=source,
        reason=summary,
        occurred_at=occurrence_at,
    )
    session.add(event)
    await session.flush()
    return AlertOccurrenceResult(alert, event, created=False, recorded=True)


async def transition_alert(
    session: AsyncSession,
    *,
    alert: Alert,
    target: AlertStatus,
    actor: str,
    reason: str,
    now: datetime | None = None,
) -> AlertChangeResult:
    """Acknowledge or resolve one alert with append-only operator evidence."""
    if alert.id is None:
        raise AlertValidationError("alert must be persisted before transition")
    actor = _require_text(actor, "actor", max_length=200)
    reason = _require_text(reason, "reason")
    try:
        target_status = AlertStatus(target)
    except ValueError as exc:
        raise AlertTransitionError("target alert status is not supported") from exc
    if target_status is AlertStatus.OPEN:
        raise AlertTransitionError("alerts reopen only after a new occurrence")
    current_status = AlertStatus(alert.status)
    if current_status is target_status:
        return AlertChangeResult(alert, event=None, changed=False)
    if current_status is AlertStatus.RESOLVED:
        raise AlertTransitionError("resolved alert cannot transition without a new occurrence")
    if target_status is AlertStatus.ACKNOWLEDGED and current_status is not AlertStatus.OPEN:
        raise AlertTransitionError("only an open alert can be acknowledged")

    occurred_at = _as_utc_naive(now or _utcnow_aware(), "now")
    if occurred_at < alert.last_seen_at:
        raise AlertTransitionError("alert transition cannot predate its last occurrence")
    if target_status is AlertStatus.ACKNOWLEDGED:
        alert.acknowledged_at = occurred_at
        alert.acknowledged_by = actor
        alert.acknowledgement_reason = reason
        event_type = AlertEventType.ACKNOWLEDGED
    else:
        alert.resolved_at = occurred_at
        alert.resolved_by = actor
        alert.resolution_reason = reason
        event_type = AlertEventType.RESOLVED
    alert.status = target_status.value
    alert.updated_at = occurred_at
    event = AlertEvent(
        alert_id=alert.id,
        idempotency_key=None,
        event_type=event_type.value,
        from_status=current_status.value,
        to_status=target_status.value,
        severity=alert.severity,
        actor=actor,
        reason=reason,
        occurred_at=occurred_at,
    )
    session.add(event)
    await session.flush()
    return AlertChangeResult(alert, event=event, changed=True)
