"""Tests for the constrained local Celery worker entrypoint."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from src import worker_cli
from src.core.config import Settings


@pytest.mark.parametrize("queue", ["gpu", "cpu", "io"])
def test_worker_arguments_isolate_and_serialize_every_queue(queue: str) -> None:
    arguments = worker_cli.worker_arguments(queue, log_level="info")

    assert f"--queues={queue}" in arguments
    assert f"--hostname={queue}@%h" in arguments
    assert "--pool=solo" in arguments
    assert "--concurrency=1" in arguments
    assert "--loglevel=INFO" in arguments


def test_worker_rejects_sqlite_before_starting(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    worker_factory = Mock()
    monkeypatch.setattr(worker_cli.celery_app, "Worker", worker_factory)
    monkeypatch.setattr(
        worker_cli,
        "get_settings",
        lambda: Settings(_env_file=None),
    )

    exit_code = worker_cli.main(["--queue", "io"])

    assert exit_code == 2
    assert "require PostgreSQL" in capsys.readouterr().err
    worker_factory.assert_not_called()


def test_worker_starts_with_fixed_arguments_for_postgresql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker = Mock(exitcode=0)
    worker_factory = Mock(return_value=worker)
    watcher = Mock()
    monkeypatch.setattr(worker_cli.celery_app, "Worker", worker_factory)
    monkeypatch.setattr(worker_cli.socket, "gethostname", lambda: "private-host")
    monkeypatch.setattr(worker_cli, "start_stop_event_watcher", lambda callback: watcher)
    monkeypatch.setattr(
        worker_cli,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            database_url=(
                "postgresql+asyncpg://foundry:private@127.0.0.1/automation_foundry"
            ),
            log_level="warning",
        ),
    )

    exit_code = worker_cli.main(["--queue", "gpu"])

    assert exit_code == 0
    worker_factory.assert_called_once_with(
        hostname="gpu@private-host",
        queues=["gpu"],
        pool="solo",
        concurrency=1,
        loglevel="WARNING",
    )
    worker.start.assert_called_once_with()
    watcher.close.assert_called_once_with()
