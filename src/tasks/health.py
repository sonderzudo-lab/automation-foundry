"""Periodic health reconciliation task for the local IO worker."""

from __future__ import annotations

import asyncio
from typing import cast

import structlog
from celery import Celery, Task

from src.core.config import settings
from src.core.database import AsyncSessionLocal, engine
from src.operations.health import collect_health_report
from src.operations.health_alerts import reconcile_health_report

logger = structlog.get_logger(__name__)


async def _reconcile(app: Celery) -> None:
    try:
        async with AsyncSessionLocal() as session:
            report = await collect_health_report(
                session,
                celery=app,
                assumed_worker_queues=frozenset({"io"}),
            )
            result = await reconcile_health_report(
                session,
                report,
                configuration=settings,
            )
            await session.commit()
            logger.info(
                "health_reconciled",
                overall_status=report.status.value,
                observed=result.observed,
                occurrences_recorded=result.occurrences_recorded,
                alerts_resolved=result.alerts_resolved,
                inconclusive=result.inconclusive,
                ignored=result.ignored,
                replayed=result.replayed,
            )
    finally:
        await engine.dispose()


def register_health_task(app: Celery) -> Task:
    """Register the retry-safe database-authoritative health reconciliation."""

    @app.task(  # type: ignore[untyped-decorator]
        name="automation_foundry.health.reconcile",
        ignore_result=True,
        autoretry_for=(Exception,),
        retry_backoff=True,
        retry_backoff_max=30,
        retry_jitter=True,
        max_retries=3,
    )
    def health_reconcile() -> None:
        asyncio.run(_reconcile(app))

    return cast(Task, health_reconcile)
