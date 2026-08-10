"""Opt-in real Redis and Celery worker queue-boundary integration test."""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

import pytest
from celery.contrib.testing.worker import start_worker
from celery.result import allow_join_result

from src.core.celery_app import QUEUE_NAMES, build_celery_app
from src.core.config import Settings
from src.operations.health import HealthStatus, probe_redis, probe_workers


@pytest.mark.redis
def test_each_celery_queue_executes_only_its_probe() -> None:
    broker_url = os.getenv("TEST_REDIS_URL")
    result_backend_url = os.getenv("TEST_REDIS_RESULT_URL")
    if not broker_url or not result_backend_url:
        pytest.skip("TEST_REDIS_URL and TEST_REDIS_RESULT_URL are not configured")

    app = build_celery_app(
        Settings(
            _env_file=None,
            redis_url=broker_url,
            celery_result_backend_url=result_backend_url,
        )
    )
    prefix = f"redis-integration-{uuid4().hex}"

    try:
        for queue in QUEUE_NAMES:
            with start_worker(
                app,
                pool="solo",
                concurrency=1,
                queues=[queue],
                perform_ping_check=False,
                loglevel="WARNING",
            ):
                result = app.send_task(
                    f"automation_foundry.probe.{queue}",
                    args=[f"{prefix}-{queue}"],
                )
                with allow_join_result():
                    payload = result.get(timeout=15)
                result.forget()

            assert payload == {
                "ok": True,
                "queue": queue,
                "nonce": f"{prefix}-{queue}",
            }
    finally:
        app.close()


@pytest.mark.redis
def test_health_probes_confirm_redis_and_all_worker_queue_bindings() -> None:
    broker_url = os.getenv("TEST_REDIS_URL")
    result_backend_url = os.getenv("TEST_REDIS_RESULT_URL")
    if not broker_url or not result_backend_url:
        pytest.skip("TEST_REDIS_URL and TEST_REDIS_RESULT_URL are not configured")

    configuration = Settings(
        _env_file=None,
        redis_url=broker_url,
        celery_result_backend_url=result_backend_url,
        health_probe_timeout_seconds=3,
    )
    app = build_celery_app(configuration)

    async def probe() -> None:
        redis_check = await probe_redis(configuration)
        worker_check = await probe_workers(app, timeout=3)
        assert redis_check.status is HealthStatus.PASS
        assert worker_check.status is HealthStatus.PASS
        assert worker_check.metrics == {
            "gpu_workers": 1,
            "cpu_workers": 1,
            "io_workers": 1,
        }

    try:
        with (
            start_worker(
                app,
                pool="solo",
                concurrency=1,
                queues=["gpu"],
                hostname="gpu@integration",
                perform_ping_check=False,
                loglevel="WARNING",
            ),
            start_worker(
                app,
                pool="solo",
                concurrency=1,
                queues=["cpu"],
                hostname="cpu@integration",
                perform_ping_check=False,
                loglevel="WARNING",
            ),
            start_worker(
                app,
                pool="solo",
                concurrency=1,
                queues=["io"],
                hostname="io@integration",
                perform_ping_check=False,
                loglevel="WARNING",
            ),
        ):
            asyncio.run(probe())
    finally:
        app.close()
