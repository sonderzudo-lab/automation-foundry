"""Read-only projections for the local server-rendered dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.platform.models import (
    AlertStatus,
    Approval,
    ApprovalStatus,
    Automation,
    LedgerEntry,
    LedgerEntryType,
    PlatformAlert,
    Run,
)


@dataclass(frozen=True, slots=True)
class AutomationSummary:
    """Safe automation fields rendered by the initial dashboard."""

    id: int
    slug: str
    name: str
    enabled: bool
    kill_switch_active: bool


@dataclass(frozen=True, slots=True)
class RunSummary:
    """Redacted recent-run projection without inputs, outputs, or errors."""

    id: int
    automation_slug: str
    status: str
    trigger: str
    queued_at: datetime
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class ApprovalSummary:
    """Redacted pending-approval projection without protected summaries."""

    id: int
    automation_slug: str
    run_id: int
    action: str
    requested_at: datetime


@dataclass(frozen=True, slots=True)
class AlertSummary:
    """Redacted active-alert projection without summaries or sources."""

    id: int
    automation_slug: str
    title: str
    status: str
    severity: str
    occurrence_count: int
    last_seen_at: datetime


@dataclass(frozen=True, slots=True)
class LedgerCurrencySummary:
    """Exact observed ledger totals for one currency, without conversion."""

    currency: str
    cost: Decimal
    revenue: Decimal
    attributed_value: Decimal
    net_revenue: Decimal


@dataclass(frozen=True, slots=True)
class DashboardSnapshot:
    """Complete read-only state rendered on the dashboard home page."""

    automations: tuple[AutomationSummary, ...]
    recent_runs: tuple[RunSummary, ...]
    pending_approvals: tuple[ApprovalSummary, ...]
    active_alerts: tuple[AlertSummary, ...]
    ledger_totals: tuple[LedgerCurrencySummary, ...]


async def load_dashboard_snapshot(
    session: AsyncSession,
    *,
    recent_run_limit: int = 10,
    pending_limit: int = 20,
    alert_limit: int = 20,
) -> DashboardSnapshot:
    """Load a bounded, redacted dashboard snapshot from durable state."""
    if recent_run_limit < 1 or pending_limit < 1 or alert_limit < 1:
        raise ValueError("dashboard query limits must be positive")

    automations = tuple(
        AutomationSummary(
            id=automation.id,
            slug=automation.slug,
            name=automation.name,
            enabled=automation.enabled,
            kill_switch_active=automation.kill_switch_active,
        )
        for automation in await session.scalars(
            select(Automation).order_by(Automation.slug)
        )
    )

    run_rows = (
        await session.execute(
            select(Run, Automation.slug)
            .join(Automation, Automation.id == Run.automation_id)
            .order_by(Run.queued_at.desc(), Run.id.desc())
            .limit(recent_run_limit)
        )
    ).all()
    recent_runs = tuple(
        RunSummary(
            id=run.id,
            automation_slug=automation_slug,
            status=run.status,
            trigger=run.trigger,
            queued_at=run.queued_at,
            finished_at=run.finished_at,
        )
        for run, automation_slug in run_rows
    )

    approval_rows = (
        await session.execute(
            select(Approval, Automation.slug)
            .join(Run, Run.id == Approval.run_id)
            .join(Automation, Automation.id == Run.automation_id)
            .where(Approval.status == ApprovalStatus.PENDING.value)
            .order_by(Approval.requested_at, Approval.id)
            .limit(pending_limit)
        )
    ).all()
    pending_approvals = tuple(
        ApprovalSummary(
            id=approval.id,
            automation_slug=automation_slug,
            run_id=approval.run_id,
            action=approval.action,
            requested_at=approval.requested_at,
        )
        for approval, automation_slug in approval_rows
    )

    alert_rows = (
        await session.execute(
            select(PlatformAlert, Automation.slug)
            .join(Automation, Automation.id == PlatformAlert.automation_id)
            .where(PlatformAlert.status != AlertStatus.RESOLVED.value)
            .order_by(PlatformAlert.last_seen_at.desc(), PlatformAlert.id.desc())
            .limit(alert_limit)
        )
    ).all()
    active_alerts = tuple(
        AlertSummary(
            id=alert.id,
            automation_slug=automation_slug,
            title=alert.title,
            status=alert.status,
            severity=alert.severity,
            occurrence_count=alert.occurrence_count,
            last_seen_at=alert.last_seen_at,
        )
        for alert, automation_slug in alert_rows
    )

    totals: dict[str, dict[LedgerEntryType, Decimal]] = {}
    ledger_entries = await session.scalars(
        select(LedgerEntry).order_by(LedgerEntry.id)
    )
    for entry in ledger_entries:
        currency_totals = totals.setdefault(
            entry.currency,
            {entry_type: Decimal(0) for entry_type in LedgerEntryType},
        )
        currency_totals[LedgerEntryType(entry.entry_type)] += entry.amount
    ledger_totals = tuple(
        LedgerCurrencySummary(
            currency=currency,
            cost=currency_totals[LedgerEntryType.COST],
            revenue=currency_totals[LedgerEntryType.REVENUE],
            attributed_value=currency_totals[LedgerEntryType.ATTRIBUTED_VALUE],
            net_revenue=(
                currency_totals[LedgerEntryType.REVENUE]
                - currency_totals[LedgerEntryType.COST]
            ),
        )
        for currency, currency_totals in sorted(totals.items())
    )

    return DashboardSnapshot(
        automations=automations,
        recent_runs=recent_runs,
        pending_approvals=pending_approvals,
        active_alerts=active_alerts,
        ledger_totals=ledger_totals,
    )
