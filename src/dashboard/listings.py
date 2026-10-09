"""Bounded, redacted listings behind the dashboard pages that are not the overview.

The overview reads one snapshot (`service.load_dashboard_snapshot`). The pages for runs,
approvals and alerts need more history than that snapshot carries, and every page needs the
small navigation summary shown in the sidebar. Everything here exposes the same redacted
fields the snapshot already shows: no inputs, outputs, errors, summaries or actor names.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.platform.models import (
    AlertStatus,
    Approval,
    ApprovalStatus,
    Automation,
    PlatformAlert,
    Run,
    RunStatus,
)

RUN_PAGE_SIZE = 25
HISTORY_LIMIT = 30

RUN_STATUS_ORDER: tuple[str, ...] = (
    RunStatus.RUNNING.value,
    RunStatus.QUEUED.value,
    RunStatus.AWAITING_APPROVAL.value,
    RunStatus.SUCCEEDED.value,
    RunStatus.FAILED.value,
    RunStatus.CANCELLED.value,
)


@dataclass(frozen=True, slots=True)
class NavAutomation:
    """One automation entry in the sidebar."""

    slug: str
    name: str


@dataclass(frozen=True, slots=True)
class NavSummary:
    """Counters and automation list shown by the sidebar on every page."""

    pending_approvals: int
    active_alerts: int
    automations: tuple[NavAutomation, ...]


@dataclass(frozen=True, slots=True)
class RunListItem:
    """Redacted run row for the runs page."""

    id: int
    automation_slug: str
    status: str
    trigger: str
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    duration_seconds: int | None
    retry_of_run_id: int | None


@dataclass(frozen=True, slots=True)
class RunListPage:
    """One page of runs plus the per-status totals used by the filter chips."""

    items: tuple[RunListItem, ...]
    total: int
    page: int
    page_size: int
    status_counts: tuple[tuple[str, int], ...]
    status: str | None
    automation_slug: str | None

    @property
    def page_count(self) -> int:
        return max(1, -(-self.total // self.page_size))

    @property
    def has_previous(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.page_count


@dataclass(frozen=True, slots=True)
class DecidedApproval:
    """Redacted decided-approval row for the approvals history."""

    id: int
    run_id: int
    automation_slug: str
    action: str
    status: str
    requested_at: datetime
    decided_at: datetime | None


@dataclass(frozen=True, slots=True)
class ResolvedAlert:
    """Redacted resolved-alert row for the alerts history."""

    id: int
    automation_slug: str
    title: str
    severity: str
    occurrence_count: int
    last_seen_at: datetime
    resolved_at: datetime | None


def valid_run_status(value: str | None) -> bool:
    """Return True for a run status the runs page can filter by."""
    return value is None or value in RUN_STATUS_ORDER


async def load_nav_summary(session: AsyncSession) -> NavSummary:
    """Load the three cheap facts the sidebar needs."""
    pending = await session.scalar(
        select(func.count())
        .select_from(Approval)
        .where(Approval.status == ApprovalStatus.PENDING.value)
    )
    alerts = await session.scalar(
        select(func.count())
        .select_from(PlatformAlert)
        .where(PlatformAlert.status != AlertStatus.RESOLVED.value)
    )
    automations = (
        await session.execute(select(Automation.slug, Automation.name).order_by(Automation.slug))
    ).all()
    return NavSummary(
        pending_approvals=int(pending or 0),
        active_alerts=int(alerts or 0),
        automations=tuple(NavAutomation(slug=slug, name=name) for slug, name in automations),
    )


async def load_run_list(
    session: AsyncSession,
    *,
    status: str | None = None,
    automation_slug: str | None = None,
    page: int = 1,
    page_size: int = RUN_PAGE_SIZE,
) -> RunListPage:
    """Load one filtered page of runs, newest first."""
    if page < 1 or page_size < 1:
        raise ValueError("run list page and size must be positive")
    if not valid_run_status(status):
        raise ValueError("unknown run status filter")

    scope = []
    if automation_slug:
        scope.append(Automation.slug == automation_slug)

    counts_rows = (
        await session.execute(
            select(Run.status, func.count())
            .join(Automation, Automation.id == Run.automation_id)
            .where(*scope)
            .group_by(Run.status)
        )
    ).all()
    counts = {row_status: int(count) for row_status, count in counts_rows}
    status_counts = tuple((value, counts.get(value, 0)) for value in RUN_STATUS_ORDER)

    filters = list(scope)
    if status:
        filters.append(Run.status == status)
    total = int(
        await session.scalar(
            select(func.count())
            .select_from(Run)
            .join(Automation, Automation.id == Run.automation_id)
            .where(*filters)
        )
        or 0
    )
    rows = (
        await session.execute(
            select(Run, Automation.slug)
            .join(Automation, Automation.id == Run.automation_id)
            .where(*filters)
            .order_by(Run.queued_at.desc(), Run.id.desc())
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
    ).all()
    items = tuple(
        RunListItem(
            id=run.id,
            automation_slug=slug,
            status=run.status,
            trigger=run.trigger,
            queued_at=run.queued_at,
            started_at=run.started_at,
            finished_at=run.finished_at,
            duration_seconds=_duration(run.started_at, run.finished_at),
            retry_of_run_id=run.retry_of_run_id,
        )
        for run, slug in rows
    )
    return RunListPage(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        status_counts=status_counts,
        status=status,
        automation_slug=automation_slug or None,
    )


async def load_approval_history(
    session: AsyncSession,
    *,
    limit: int = HISTORY_LIMIT,
) -> tuple[DecidedApproval, ...]:
    """Load the most recently decided approvals without summaries or actors."""
    if limit < 1:
        raise ValueError("history limit must be positive")
    rows = (
        await session.execute(
            select(Approval, Automation.slug)
            .join(Run, Run.id == Approval.run_id)
            .join(Automation, Automation.id == Run.automation_id)
            .where(Approval.status != ApprovalStatus.PENDING.value)
            .order_by(
                Approval.decided_at.desc().nulls_last(),
                Approval.id.desc(),
            )
            .limit(limit)
        )
    ).all()
    return tuple(
        DecidedApproval(
            id=approval.id,
            run_id=approval.run_id,
            automation_slug=slug,
            action=approval.action,
            status=approval.status,
            requested_at=approval.requested_at,
            decided_at=approval.decided_at,
        )
        for approval, slug in rows
    )


async def load_resolved_alerts(
    session: AsyncSession,
    *,
    limit: int = HISTORY_LIMIT,
) -> tuple[ResolvedAlert, ...]:
    """Load the most recently resolved alerts without summaries, sources or actors."""
    if limit < 1:
        raise ValueError("history limit must be positive")
    rows = (
        await session.execute(
            select(PlatformAlert, Automation.slug)
            .join(Automation, Automation.id == PlatformAlert.automation_id)
            .where(PlatformAlert.status == AlertStatus.RESOLVED.value)
            .order_by(
                PlatformAlert.resolved_at.desc().nulls_last(),
                PlatformAlert.id.desc(),
            )
            .limit(limit)
        )
    ).all()
    return tuple(
        ResolvedAlert(
            id=alert.id,
            automation_slug=slug,
            title=alert.title,
            severity=alert.severity,
            occurrence_count=alert.occurrence_count,
            last_seen_at=alert.last_seen_at,
            resolved_at=alert.resolved_at,
        )
        for alert, slug in rows
    )


def _duration(started_at: datetime | None, finished_at: datetime | None) -> int | None:
    if started_at is None or finished_at is None:
        return None
    start = (
        started_at if started_at.tzinfo is None else started_at.astimezone(UTC).replace(tzinfo=None)
    )
    end = (
        finished_at
        if finished_at.tzinfo is None
        else finished_at.astimezone(UTC).replace(tzinfo=None)
    )
    seconds = int((end - start).total_seconds())
    return seconds if seconds >= 0 else None
