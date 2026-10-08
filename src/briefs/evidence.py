"""Read-only, redacted evidence about the platform's own operation.

The evidence contains counts, slugs, IDs, states, durations, ages, allowlisted error
codes and exact ledger totals. It never carries run payloads, error messages, review
text, content topics, or ledger/metric ``source``, ``category`` and ``dimensions``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.platform.models import (
    AlertStatus,
    Approval,
    ApprovalStatus,
    Automation,
    ConnectorObservation,
    LedgerEntry,
    LedgerEntryType,
    PlatformAlert,
    Run,
    RunStatus,
)

EVIDENCE_SCHEMA_VERSION = 1
# Stored in the evidence so an older brief is always re-rendered the way it was written.
# 2: the mean approval decision time is shown as days, hours and minutes.
RENDERER_VERSION = 2
ALLOWED_WINDOW_DAYS = (1, 7, 14, 30)
MAX_RUN_ROWS = 10_000
MAX_LISTED_FAILURES = 10
MAX_LISTED_APPROVALS = 20
MAX_LISTED_CONNECTORS = 20

_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{2,63}")
_RATE_QUANTUM = Decimal("0.0001")
_SECONDS_QUANTUM = Decimal("0.001")


class OperationsEvidenceError(ValueError):
    """Raised when the requested window cannot be observed unambiguously."""


@dataclass(frozen=True, slots=True)
class EvidenceWindow:
    """Half-open UTC window ``[start, end)`` covering whole days."""

    start: datetime
    end: datetime
    days: int


def build_window(*, window_end: date, window_days: int) -> EvidenceWindow:
    """Return the window whose last full day is ``window_end``."""
    if window_days not in ALLOWED_WINDOW_DAYS:
        raise OperationsEvidenceError("window_days is not an allowed window")
    end = datetime.combine(window_end + timedelta(days=1), time.min)
    return EvidenceWindow(start=end - timedelta(days=window_days), end=end, days=window_days)


async def collect_operations_evidence(
    session: AsyncSession,
    *,
    window: EvidenceWindow,
    collected_at: datetime,
    exclude_run_id: int | None,
) -> dict[str, Any]:
    """Collect the redacted evidence document for one window."""
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "renderer_version": RENDERER_VERSION,
        "window": {
            "start": _iso(window.start),
            "end_exclusive": _iso(window.end),
            "days": window.days,
        },
        "collected_at": _iso(collected_at),
        "runs": await _collect_runs(session, window=window, exclude_run_id=exclude_run_id),
        "approvals": await _collect_approvals(session, window=window, collected_at=collected_at),
        "alerts": await _collect_alerts(session, window=window),
        "ledger": await _collect_ledger(session, window=window),
        "connectors": await _collect_connectors(session, collected_at=collected_at),
    }


async def _collect_runs(
    session: AsyncSession,
    *,
    window: EvidenceWindow,
    exclude_run_id: int | None,
) -> dict[str, Any]:
    statement = (
        select(Run, Automation.slug)
        .join(Automation, Automation.id == Run.automation_id)
        .where(Run.queued_at >= window.start, Run.queued_at < window.end)
        .order_by(Run.id)
        .limit(MAX_RUN_ROWS + 1)
    )
    if exclude_run_id is not None:
        statement = statement.where(Run.id != exclude_run_id)
    rows = list((await session.execute(statement)).all())
    truncated = len(rows) > MAX_RUN_ROWS
    rows = rows[:MAX_RUN_ROWS]

    by_slug: dict[str, dict[str, Any]] = {}
    failures: list[dict[str, Any]] = []
    concluded_total = 0
    concluded_with_timing = 0
    for run, slug in rows:
        bucket = by_slug.setdefault(
            slug,
            {
                "slug": slug,
                "total": 0,
                "by_status": {status.value: 0 for status in RunStatus},
                "durations": [],
            },
        )
        bucket["total"] += 1
        bucket["by_status"][run.status] += 1
        if run.status in {RunStatus.SUCCEEDED.value, RunStatus.FAILED.value}:
            concluded_total += 1
            seconds = _duration_seconds(run.started_at, run.finished_at)
            if seconds is not None:
                concluded_with_timing += 1
                bucket["durations"].append(seconds)
        if run.status == RunStatus.FAILED.value:
            failures.append(
                {
                    "run_id": run.id,
                    "automation": slug,
                    "error_code": _error_code(run.error),
                }
            )

    automations: list[dict[str, Any]] = []
    for slug in sorted(by_slug):
        bucket = by_slug[slug]
        statuses = bucket["by_status"]
        concluded = statuses[RunStatus.SUCCEEDED.value] + statuses[RunStatus.FAILED.value]
        durations: list[Decimal] = bucket["durations"]
        automations.append(
            {
                "slug": slug,
                "total": bucket["total"],
                "by_status": statuses,
                "success_rate": (
                    None
                    if concluded == 0
                    else _decimal_text(
                        Decimal(statuses[RunStatus.SUCCEEDED.value]) / Decimal(concluded),
                        _RATE_QUANTUM,
                    )
                ),
                "mean_duration_seconds": (
                    None
                    if not durations
                    else _decimal_text(
                        sum(durations, Decimal(0)) / Decimal(len(durations)),
                        _SECONDS_QUANTUM,
                    )
                ),
            }
        )
    failures.sort(key=lambda item: item["run_id"], reverse=True)
    return {
        "total": len(rows),
        "truncated": truncated,
        "by_automation": automations,
        "failed_total": len(failures),
        "concluded_total": concluded_total,
        "concluded_with_timing": concluded_with_timing,
        "recent_failures": failures[:MAX_LISTED_FAILURES],
    }


async def _collect_approvals(
    session: AsyncSession,
    *,
    window: EvidenceWindow,
    collected_at: datetime,
) -> dict[str, Any]:
    pending_rows = (
        await session.execute(
            select(Approval, Automation.slug)
            .join(Run, Run.id == Approval.run_id)
            .join(Automation, Automation.id == Run.automation_id)
            .where(Approval.status == ApprovalStatus.PENDING.value)
            .order_by(Approval.requested_at, Approval.id)
        )
    ).all()
    pending = [
        {
            "approval_id": approval.id,
            "run_id": approval.run_id,
            "automation": slug,
            "action": approval.action,
            "age_seconds": max(0, int((collected_at - approval.requested_at).total_seconds())),
        }
        for approval, slug in pending_rows
    ]
    decided = (
        await session.scalars(
            select(Approval).where(
                Approval.decided_at >= window.start,
                Approval.decided_at < window.end,
            )
        )
    ).all()
    latencies = [
        seconds
        for approval in decided
        if (seconds := _duration_seconds(approval.requested_at, approval.decided_at)) is not None
    ]
    return {
        "pending_count": len(pending),
        "pending": pending[:MAX_LISTED_APPROVALS],
        "decided_in_window": len(decided),
        "approved_in_window": sum(
            1 for item in decided if item.status == ApprovalStatus.APPROVED.value
        ),
        "rejected_in_window": sum(
            1 for item in decided if item.status == ApprovalStatus.REJECTED.value
        ),
        "mean_decision_seconds": (
            None
            if not latencies
            else _decimal_text(
                sum(latencies, Decimal(0)) / Decimal(len(latencies)),
                _SECONDS_QUANTUM,
            )
        ),
    }


async def _collect_alerts(
    session: AsyncSession,
    *,
    window: EvidenceWindow,
) -> dict[str, Any]:
    alerts = (await session.scalars(select(PlatformAlert))).all()
    active = [alert for alert in alerts if alert.status != AlertStatus.RESOLVED.value]
    by_severity: dict[str, int] = {}
    for alert in active:
        by_severity[alert.severity] = by_severity.get(alert.severity, 0) + 1
    return {
        "active_count": len(active),
        "active_by_severity": dict(sorted(by_severity.items())),
        "opened_in_window": sum(
            1 for alert in alerts if window.start <= alert.first_seen_at < window.end
        ),
    }


async def _collect_ledger(
    session: AsyncSession,
    *,
    window: EvidenceWindow,
) -> list[dict[str, str]]:
    entries = (
        await session.scalars(
            select(LedgerEntry)
            .where(LedgerEntry.observed_at >= window.start, LedgerEntry.observed_at < window.end)
            .order_by(LedgerEntry.currency, LedgerEntry.id)
        )
    ).all()
    totals: dict[str, dict[LedgerEntryType, Decimal]] = {}
    for entry in entries:
        bucket = totals.setdefault(entry.currency, {kind: Decimal(0) for kind in LedgerEntryType})
        bucket[LedgerEntryType(entry.entry_type)] += entry.amount
    return [
        {
            "currency": currency,
            "cost": format(bucket[LedgerEntryType.COST], "f"),
            "revenue": format(bucket[LedgerEntryType.REVENUE], "f"),
            "attributed_value": format(bucket[LedgerEntryType.ATTRIBUTED_VALUE], "f"),
            "net_revenue": format(
                bucket[LedgerEntryType.REVENUE] - bucket[LedgerEntryType.COST], "f"
            ),
        }
        for currency, bucket in sorted(totals.items())
    ]


async def _collect_connectors(
    session: AsyncSession,
    *,
    collected_at: datetime,
) -> dict[str, Any]:
    rows = (
        await session.execute(
            select(ConnectorObservation, Automation.slug)
            .join(Automation, Automation.id == ConnectorObservation.automation_id)
            .order_by(ConnectorObservation.observed_at.desc(), ConnectorObservation.id.desc())
        )
    ).all()
    latest: dict[tuple[int, str], tuple[ConnectorObservation, str]] = {}
    for observation, slug in rows:
        latest.setdefault(
            (observation.automation_id, observation.connector_key), (observation, slug)
        )
    ordered = sorted(latest.values(), key=lambda item: (item[1], item[0].connector_key))
    listed = [
        {
            "automation": slug,
            "connector_key": observation.connector_key,
            "status": observation.status,
            "quality_status": observation.quality_status,
            "stale": _is_stale(observation, collected_at),
        }
        for observation, slug in ordered
    ]
    return {
        "total": len(listed),
        "truncated": len(listed) > MAX_LISTED_CONNECTORS,
        "latest": listed[:MAX_LISTED_CONNECTORS],
    }


def _is_stale(observation: ConnectorObservation, collected_at: datetime) -> bool:
    if observation.last_success_at is None:
        return True
    age = (collected_at - observation.last_success_at).total_seconds()
    return age > observation.freshness_slo_seconds


def _error_code(error: object) -> str:
    if isinstance(error, dict):
        code = error.get("code")
        if isinstance(code, str) and _ERROR_CODE.fullmatch(code):
            return code
    return "UNKNOWN"


def _duration_seconds(start: datetime | None, end: datetime | None) -> Decimal | None:
    if start is None or end is None or end < start:
        return None
    return Decimal(str((end - start).total_seconds()))


def _decimal_text(value: Decimal, quantum: Decimal) -> str:
    return format(value.quantize(quantum), "f")


def _iso(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat() + "Z"
