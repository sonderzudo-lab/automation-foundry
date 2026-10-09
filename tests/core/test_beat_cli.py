"""Tests for the fixed singleton Celery Beat entrypoint."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from src.beat_cli import (
    BeatAlreadyRunningError,
    _beat_configuration_error,
    acquire_beat_singleton,
    beat_arguments,
    main,
)
from src.core.config import Settings


def test_beat_arguments_use_fixed_local_pid_and_schedule_files() -> None:
    arguments = beat_arguments(Path("C:/foundry/storage"), log_level="info")

    assert arguments == [
        "beat",
        "--loglevel=INFO",
        "--pidfile=C:\\foundry\\storage\\celerybeat.pid",
        "--schedule=C:\\foundry\\storage\\celerybeat-schedule",
    ]


def test_beat_requires_postgresql() -> None:
    assert _beat_configuration_error(Settings(_env_file=None)) is not None
    assert (
        _beat_configuration_error(
            Settings(
                _env_file=None,
                database_url="postgresql+asyncpg://user:pass@127.0.0.1/foundry",
            )
        )
        is None
    )


def test_windows_mutex_rejects_a_second_live_beat() -> None:
    first = acquire_beat_singleton()
    if first is None:
        pytest.skip("Windows named mutex is only used on Windows")
    try:
        with pytest.raises(BeatAlreadyRunningError):
            acquire_beat_singleton()
    finally:
        first.release()


def test_beat_service_uses_cooperative_stop_and_releases_resources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from src import beat_cli

    configuration = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://user:pass@127.0.0.1/foundry",
        storage_root=str(tmp_path),
    )
    singleton = Mock()
    pidlock = Mock()
    service = Mock()
    watcher = Mock()
    monkeypatch.setattr(beat_cli, "get_settings", lambda: configuration)
    monkeypatch.setattr(beat_cli, "acquire_beat_singleton", lambda: singleton)
    monkeypatch.setattr(beat_cli.platforms, "create_pidlock", lambda _path: pidlock)
    monkeypatch.setattr(beat_cli, "Service", Mock(return_value=service))
    monkeypatch.setattr(
        beat_cli,
        "start_stop_event_watcher",
        lambda callback: watcher,
    )

    assert main([]) == 0
    service.start.assert_called_once_with()
    service.stop.assert_called_once_with()
    watcher.close.assert_called_once_with()
    pidlock.release.assert_called_once_with()
    singleton.release.assert_called_once_with()


def _run_main_observing_pidfile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    singleton: Mock | None,
) -> list[bool]:
    from src import beat_cli

    configuration = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://user:pass@127.0.0.1/foundry",
        storage_root=str(tmp_path),
    )
    pidfile = tmp_path / "celerybeat.pid"
    pidfile.write_text("40348", encoding="utf-8")
    seen: list[bool] = []

    def create_pidlock(path: str) -> Mock:
        assert Path(path) == pidfile
        seen.append(pidfile.exists())
        return Mock()

    monkeypatch.setattr(beat_cli, "get_settings", lambda: configuration)
    monkeypatch.setattr(beat_cli, "acquire_beat_singleton", lambda: singleton)
    monkeypatch.setattr(beat_cli.platforms, "create_pidlock", create_pidlock)
    monkeypatch.setattr(beat_cli, "Service", Mock(return_value=Mock()))
    monkeypatch.setattr(beat_cli, "start_stop_event_watcher", lambda callback: None)

    assert main([]) == 0
    return seen


def test_beat_discards_orphan_pidfile_once_the_mutex_is_owned(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Celery treats EINVAL from os.kill on Windows as "already running", so an
    # unclean exit would block every later start even though the mutex is free.
    seen = _run_main_observing_pidfile(monkeypatch, tmp_path, singleton=Mock())

    assert seen == [False]


def test_beat_keeps_pidfile_for_celery_when_no_mutex_guards_the_singleton(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seen = _run_main_observing_pidfile(monkeypatch, tmp_path, singleton=None)

    assert seen == [True]
