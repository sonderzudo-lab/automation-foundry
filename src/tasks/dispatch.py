"""Celery entrypoint for durable database-backed run dispatches."""

from __future__ import annotations

import asyncio
from typing import cast

from celery import Celery, Task

from src.core.config import settings
from src.core.database import AsyncSessionLocal, engine
from src.platform.dispatch_service import execute_claimed_dispatch


async def _execute(
    dispatch_id: int,
    delivery_id: str,
    worker_id: str,
) -> int | None:
    try:
        async with AsyncSessionLocal() as session:
            result = await execute_claimed_dispatch(
                session,
                dispatch_id=dispatch_id,
                delivery_id=delivery_id,
                worker_id=worker_id,
                lease_seconds=settings.celery_dispatch_lease_seconds,
            )
            return result.retry_after_seconds
    finally:
        # Celery invokes a fresh asyncio loop per synchronous task. Disposing prevents
        # asyncpg pool connections from leaking across loop boundaries.
        await engine.dispose()


def register_dispatch_task(app: Celery) -> Task:
    """Register the durable dispatcher without contacting the broker."""

    @app.task(  # type: ignore[untyped-decorator]
        bind=True,
        name="automation_foundry.dispatch.execute",
        ignore_result=True,
        max_retries=None,
    )
    def dispatch_task(task: Task, dispatch_id: int, delivery_id: str) -> None:
        worker_id = str(getattr(task.request, "hostname", None) or "unknown-worker")
        retry_after = asyncio.run(_execute(dispatch_id, delivery_id, worker_id))
        if retry_after is not None:
            raise task.retry(countdown=retry_after)

    return cast(Task, dispatch_task)
