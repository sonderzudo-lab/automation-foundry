"""Local alerts for schedule occurrences that did not run.

A skipped occurrence is already recorded with a reason code, but nothing told the
operator that a recurring job silently did not happen. This module turns that
evidence into one deduplicated alert per schedule and resolves it once a later
occurrence is prepared again. It sends nothing outside the local database.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.platform.alert_service import record_alert_occurrence, transition_alert
from src.platform.models import (
    AlertSeverity,
    AlertStatus,
    Automation,
    PlatformAlert,
    Schedule,
)

SCHEDULE_ALERT_SOURCE = "schedule-dispatcher"
_MAX_NAME_IN_TITLE = 120

# Closed set of reason codes the dispatcher can record, with operator-facing text and a
# severity. Operator-initiated pauses are informational; configuration faults are errors.
_REASONS: dict[str, tuple[str, AlertSeverity]] = {
    "AUTOMATION_DISABLED": (
        "o módulo está desabilitado administrativamente",
        AlertSeverity.INFO,
    ),
    "KILL_SWITCH_ACTIVE": ("o kill switch do módulo está ativo", AlertSeverity.INFO),
    "OVERLAP_BLOCKED": (
        "uma run anterior do mesmo schedule ainda está aberta ou aguardando approval",
        AlertSeverity.WARNING,
    ),
    "MISFIRE_GRACE_EXCEEDED": (
        "o horário passou da tolerância, normalmente porque o runtime estava parado",
        AlertSeverity.WARNING,
    ),
    "EXECUTOR_NOT_REGISTERED": (
        "o módulo não tem executor local registrado",
        AlertSeverity.ERROR,
    ),
    "INVALID_SCHEDULE_INPUT": (
        "a entrada configurada no schedule não é válida para o módulo",
        AlertSeverity.ERROR,
    ),
}
_UNKNOWN_REASON = ("motivo desconhecido; consulte a ocorrência", AlertSeverity.ERROR)


def schedule_alert_deduplication_key(schedule_id: int) -> str:
    """Return the single alert identity shared by every skipped occurrence of a schedule."""
    return f"schedule-occurrence-skipped:{schedule_id}"


async def record_skipped_occurrence_alert(
    session: AsyncSession,
    *,
    schedule: Schedule,
    automation: Automation,
    scheduled_for: datetime,
    reason_code: str,
    occurrence_key: str,
    now: datetime,
) -> None:
    """Open or update the schedule's alert for one skipped occurrence, idempotently."""
    if schedule.id is None:
        raise ValueError("schedule must be persisted before recording an alert")
    description, severity = _REASONS.get(reason_code, _UNKNOWN_REASON)
    name = schedule.name[:_MAX_NAME_IN_TITLE]
    await record_alert_occurrence(
        session,
        automation=automation,
        deduplication_key=schedule_alert_deduplication_key(schedule.id),
        idempotency_key=f"{occurrence_key}:skipped",
        title=f"Schedule '{name}' não executou uma ocorrência",
        summary=(
            f"A ocorrência de {scheduled_for.replace(microsecond=0).isoformat()}Z do schedule "
            f"'{schedule.name}' não foi executada: {description} ({reason_code})."
        ),
        severity=severity,
        source=SCHEDULE_ALERT_SOURCE,
        observed_at=scheduled_for.replace(tzinfo=UTC),
        now=now,
    )


async def resolve_skipped_occurrence_alert(
    session: AsyncSession,
    *,
    schedule: Schedule,
    automation: Automation,
    scheduled_for: datetime,
    now: datetime,
) -> bool:
    """Resolve the schedule's open alert after a later occurrence was prepared normally."""
    if schedule.id is None or automation.id is None:
        raise ValueError("schedule and automation must be persisted")
    alert = await session.scalar(
        select(PlatformAlert).where(
            PlatformAlert.automation_id == automation.id,
            PlatformAlert.deduplication_key == schedule_alert_deduplication_key(schedule.id),
        )
    )
    if alert is None or alert.status == AlertStatus.RESOLVED.value:
        return False
    change = await transition_alert(
        session,
        alert=alert,
        target=AlertStatus.RESOLVED,
        actor=SCHEDULE_ALERT_SOURCE,
        reason=(
            f"A ocorrência de {scheduled_for.replace(microsecond=0).isoformat()}Z foi "
            "preparada normalmente."
        ),
        now=now,
    )
    return change.changed
