"""Celery tick that turns database schedules into durable dispatches."""

from __future__ import annotations

import asyncio
from typing import cast

from celery import Celery, Task

from src.core.config import settings
from src.core.database import AsyncSessionLocal, engine
from src.platform.models import QueueClass
from src.platform.schedule_dispatcher import dispatch_due_schedules


async def _tick(app: Celery) -> None:
    def publish(dispatch_id: int, delivery_id: str, queue: QueueClass) -> None:
        app.send_task(
            "automation_foundry.dispatch.execute",
            args=[dispatch_id, delivery_id],
            task_id=delivery_id,
            queue=queue.value,
            routing_key=queue.value,
            ignore_result=True,
        )

    try:
        async with AsyncSessionLocal() as session:
            await dispatch_due_schedules(
                session,
                publish=publish,
                batch_size=settings.celery_schedule_batch_size,
            )
    finally:
        await engine.dispose()


def register_schedule_task(app: Celery) -> Task:
    """Register the database-authoritative schedule tick."""

    @app.task(  # type: ignore[untyped-decorator]
        name="automation_foundry.schedule.tick",
        ignore_result=True,
    )
    def schedule_tick() -> None:
        asyncio.run(_tick(app))

    return cast(Task, schedule_tick)
