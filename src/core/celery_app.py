"""Local Celery topology for isolated GPU, CPU, and IO workers."""

from __future__ import annotations

from celery import Celery
from kombu import Exchange, Queue

from src.core.config import Settings, settings
from src.platform.models import QueueClass
from src.tasks.dispatch import register_dispatch_task
from src.tasks.health import register_health_task
from src.tasks.probes import register_probe_tasks
from src.tasks.schedules import register_schedule_task

QUEUE_NAMES = ("gpu", "cpu", "io")


def build_celery_app(configuration: Settings) -> Celery:
    """Build a fail-closed local Celery app without contacting Redis."""
    if (
        configuration.celery_hard_time_limit_seconds
        <= configuration.celery_soft_time_limit_seconds
    ):
        raise ValueError("Celery hard time limit must exceed the soft time limit")
    if (
        configuration.celery_visibility_timeout_seconds
        <= configuration.celery_hard_time_limit_seconds
    ):
        raise ValueError("Celery visibility timeout must exceed the hard time limit")
    if (
        configuration.celery_dispatch_lease_seconds
        <= configuration.celery_hard_time_limit_seconds
    ):
        raise ValueError("Celery dispatch lease must exceed the hard time limit")

    app = Celery(
        "automation_foundry",
        broker=configuration.redis_url,
        backend=configuration.celery_result_backend_url,
    )
    queues = tuple(
        Queue(
            name,
            Exchange(name, type="direct", durable=True),
            routing_key=name,
            durable=True,
        )
        for name in QUEUE_NAMES
    )
    app.conf.update(
        accept_content=["json"],
        broker_connection_retry_on_startup=True,
        broker_transport_options={
            "visibility_timeout": configuration.celery_visibility_timeout_seconds,
        },
        beat_schedule={
            "database-schedule-tick": {
                "task": "automation_foundry.schedule.tick",
                "schedule": float(configuration.celery_schedule_tick_seconds),
                "options": {
                    "queue": "io",
                    "routing_key": "io",
                    "expires": configuration.celery_schedule_tick_seconds,
                },
            },
            "health-alert-reconciliation": {
                "task": "automation_foundry.health.reconcile",
                "schedule": float(configuration.celery_health_tick_seconds),
                "options": {
                    "queue": "io",
                    "routing_key": "io",
                    "expires": configuration.celery_health_tick_seconds,
                },
            },
        },
        beat_sync_every=1,
        enable_utc=True,
        result_accept_content=["json"],
        result_expires=configuration.celery_result_expires_seconds,
        task_acks_late=True,
        task_create_missing_queues=False,
        task_default_delivery_mode="persistent",
        task_default_exchange="io",
        task_default_exchange_type="direct",
        task_default_queue="io",
        task_default_routing_key="io",
        task_ignore_result=True,
        task_publish_retry=True,
        task_publish_retry_policy={
            "max_retries": 3,
            "interval_start": 0,
            "interval_step": 0.5,
            "interval_max": 2,
        },
        task_queues=queues,
        task_reject_on_worker_lost=True,
        task_routes={
            "automation_foundry.probe.gpu": {"queue": "gpu", "routing_key": "gpu"},
            "automation_foundry.probe.cpu": {"queue": "cpu", "routing_key": "cpu"},
            "automation_foundry.probe.io": {"queue": "io", "routing_key": "io"},
            "automation_foundry.dispatch.execute": {
                "queue": "io",
                "routing_key": "io",
            },
            "automation_foundry.schedule.tick": {
                "queue": "io",
                "routing_key": "io",
            },
            "automation_foundry.health.reconcile": {
                "queue": "io",
                "routing_key": "io",
            },
        },
        task_serializer="json",
        task_send_sent_event=True,
        task_soft_time_limit=configuration.celery_soft_time_limit_seconds,
        task_time_limit=configuration.celery_hard_time_limit_seconds,
        task_track_started=True,
        timezone="UTC",
        worker_hijack_root_logger=False,
        worker_prefetch_multiplier=1,
        worker_send_task_events=True,
    )
    register_probe_tasks(app)
    register_dispatch_task(app)
    register_schedule_task(app)
    register_health_task(app)
    return app


celery_app = build_celery_app(settings)


def publish_dispatch_message(
    dispatch_id: int,
    delivery_id: str,
    queue: QueueClass,
) -> None:
    """Publish a previously committed dispatch with its stable broker identity."""
    celery_app.send_task(
        "automation_foundry.dispatch.execute",
        args=[dispatch_id, delivery_id],
        task_id=delivery_id,
        queue=queue.value,
        routing_key=queue.value,
        ignore_result=True,
    )
