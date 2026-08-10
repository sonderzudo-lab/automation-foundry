"""Tests for the fail-closed local Celery queue topology."""

from __future__ import annotations

import pytest

from src.core.celery_app import QUEUE_NAMES, build_celery_app
from src.core.config import Settings
from src.tasks import health as health_tasks


def test_celery_app_declares_only_the_three_supported_queues() -> None:
    app = build_celery_app(Settings(_env_file=None))

    assert tuple(queue.name for queue in app.conf.task_queues) == QUEUE_NAMES
    assert app.conf.task_default_queue == "io"
    assert app.conf.task_create_missing_queues is False
    assert app.conf.task_acks_late is True
    assert app.conf.task_reject_on_worker_lost is True
    assert app.conf.worker_prefetch_multiplier == 1
    assert app.conf.task_ignore_result is True
    route = app.amqp.router.route(
        {}, "automation_foundry.dispatch.execute", args=(), kwargs={}
    )
    assert route["queue"].name == "io"
    assert "automation_foundry.dispatch.execute" in app.tasks
    schedule_route = app.amqp.router.route(
        {}, "automation_foundry.schedule.tick", args=(), kwargs={}
    )
    assert schedule_route["queue"].name == "io"
    assert app.conf.beat_schedule["database-schedule-tick"] == {
        "task": "automation_foundry.schedule.tick",
        "schedule": 30.0,
        "options": {"queue": "io", "routing_key": "io", "expires": 30},
    }
    health_route = app.amqp.router.route(
        {}, "automation_foundry.health.reconcile", args=(), kwargs={}
    )
    assert health_route["queue"].name == "io"
    assert "automation_foundry.health.reconcile" in app.tasks
    assert app.conf.beat_schedule["health-alert-reconciliation"] == {
        "task": "automation_foundry.health.reconcile",
        "schedule": 60.0,
        "options": {"queue": "io", "routing_key": "io", "expires": 60},
    }


@pytest.mark.parametrize("queue", QUEUE_NAMES)
def test_probe_tasks_route_and_return_only_redacted_ephemeral_data(queue: str) -> None:
    app = build_celery_app(Settings(_env_file=None))
    task_name = f"automation_foundry.probe.{queue}"
    route = app.amqp.router.route({}, task_name, args=(), kwargs={})

    result = app.tasks[task_name].apply(args=[" probe-001 "])

    assert route["queue"].name == queue
    assert route["routing_key"] == queue
    assert result.successful()
    assert result.result == {"ok": True, "queue": queue, "nonce": "probe-001"}


@pytest.mark.parametrize("nonce", ["", " ", "x" * 101])
def test_probe_rejects_invalid_nonce(nonce: str) -> None:
    app = build_celery_app(Settings(_env_file=None))

    result = app.tasks["automation_foundry.probe.io"].apply(args=[nonce])

    assert result.failed()
    assert isinstance(result.result, ValueError)


def test_health_task_executes_the_registered_async_reconciler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def reconcile(app: object) -> None:
        calls.append(str(getattr(app, "main", "")))

    monkeypatch.setattr(health_tasks, "_reconcile", reconcile)
    app = build_celery_app(Settings(_env_file=None))

    result = app.tasks["automation_foundry.health.reconcile"].apply()

    assert result.successful()
    assert calls == ["automation_foundry"]


@pytest.mark.parametrize(
    ("soft_limit", "hard_limit", "visibility_timeout", "message"),
    [
        (30, 30, 3600, "hard time limit"),
        (59, 60, 60, "visibility timeout"),
    ],
)
def test_celery_app_rejects_unsafe_timeout_relationships(
    soft_limit: int,
    hard_limit: int,
    visibility_timeout: int,
    message: str,
) -> None:
    settings = Settings(
        _env_file=None,
        celery_soft_time_limit_seconds=soft_limit,
        celery_hard_time_limit_seconds=hard_limit,
        celery_visibility_timeout_seconds=visibility_timeout,
    )

    with pytest.raises(ValueError, match=message):
        build_celery_app(settings)


def test_celery_app_rejects_dispatch_lease_not_longer_than_hard_limit() -> None:
    configuration = Settings(
        _env_file=None,
        celery_soft_time_limit_seconds=30,
        celery_hard_time_limit_seconds=60,
        celery_dispatch_lease_seconds=60,
    )

    with pytest.raises(ValueError, match="dispatch lease"):
        build_celery_app(configuration)
