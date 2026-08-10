"""Safe ownership, state, and Windows lifecycle tests for the runtime supervisor."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from threading import Thread
from unittest.mock import Mock

import pytest

from src.core.config import Settings
from src.runtime import supervisor
from src.runtime.supervisor import (
    RuntimeCommandError,
    RuntimeProcessSpec,
    RuntimeProcessState,
    RuntimeState,
    build_process_specs,
    load_runtime_status,
    run_supervisor,
    runtime_configuration_error,
)
from src.runtime.windows import (
    RuntimeAlreadyRunningError,
    acquire_runtime_handles,
    assign_process_to_job,
    signal_runtime_stop,
    wait_for_stop,
)


def _configuration(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        database_url=(
            "postgresql+asyncpg://foundry:secret@127.0.0.1/automation_foundry"
        ),
        storage_root=str(tmp_path / "storage"),
    )


def test_runtime_preflight_requires_windows_postgresql_password_and_loopback(
    tmp_path: Path,
) -> None:
    valid = _configuration(tmp_path)

    assert runtime_configuration_error(valid, platform="win32") is None
    assert "Windows" in str(runtime_configuration_error(valid, platform="linux"))
    assert "PostgreSQL" in str(
        runtime_configuration_error(
            Settings(_env_file=None, storage_root=str(tmp_path / "storage")),
            platform="win32",
        )
    )
    assert "password" in str(
        runtime_configuration_error(
            Settings(
                _env_file=None,
                database_url=(
                    "postgresql+asyncpg://foundry:SET_IN_LOCAL_ENV@127.0.0.1/db"
                ),
                storage_root=str(tmp_path / "storage"),
            ),
            platform="win32",
        )
    )
    assert "Redis" in str(
        runtime_configuration_error(
            Settings(
                _env_file=None,
                database_url=valid.database_url,
                redis_url="redis://192.168.1.20:6379/0",
                storage_root=str(tmp_path / "storage"),
            ),
            platform="win32",
        )
    )


def test_runtime_process_topology_is_fixed_and_shell_free(tmp_path: Path) -> None:
    specs = build_process_specs(
        _configuration(tmp_path),
        python_executable="C:/foundry/python.exe",
    )

    assert [spec.name for spec in specs] == [
        "worker-gpu",
        "worker-cpu",
        "worker-io",
        "beat",
        "dashboard",
    ]
    assert specs[0].command[-2:] == ("--queue", "gpu")
    assert specs[2].command[-2:] == ("--queue", "io")
    assert all(isinstance(spec.command, tuple) for spec in specs)


def test_runtime_state_roundtrip_and_status_reject_pid_reuse(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    configuration = _configuration(tmp_path)
    state_path = Path(configuration.storage_root) / "runtime" / "runtime-state.json"
    state_path.parent.mkdir(parents=True)
    state = RuntimeState(
        version=1,
        supervisor_pid=10,
        supervisor_created_at=100.0,
        started_at="2026-08-10T12:00:00+00:00",
        managed_infrastructure=("redis",),
        processes=(RuntimeProcessState("worker-io", 20, 200.0),),
    )
    supervisor._write_state(state_path, state)
    monkeypatch.setattr(supervisor, "runtime_mutex_is_owned", lambda: True)
    monkeypatch.setattr(
        supervisor,
        "_process_matches",
        lambda pid, created_at: (pid, created_at) == (10, 100.0),
    )

    status = load_runtime_status(configuration)

    assert status.running is True
    assert status.services == {
        "worker-io": "stopped",
        "postgres": "not-owned",
        "redis": "managed-by-runtime",
    }
    assert "pid" not in repr(status)
    assert supervisor._read_state(state_path) == state


def test_runtime_state_rejects_unknown_service_projection(tmp_path: Path) -> None:
    state_path = tmp_path / "runtime-state.json"
    state_path.write_text(
        '{"version":1,"supervisor_pid":1,"supervisor_created_at":1,'
        '"started_at":"now","managed_infrastructure":[],'
        '"processes":[{"name":"private-secret","pid":2,"created_at":2}]}',
        encoding="utf-8",
    )

    assert supervisor._read_state(state_path) is None


def test_compose_ownership_stops_only_services_started_by_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="")

    monkeypatch.setattr(
        supervisor,
        "_compose_running_services",
        lambda *_args, **_kwargs: frozenset({"postgres"}),
    )
    monkeypatch.setattr(supervisor, "_run_checked", run)

    managed = supervisor._start_infrastructure(tmp_path, timeout_seconds=30)
    supervisor._stop_infrastructure(tmp_path, managed, timeout_seconds=30)

    assert managed == ("redis",)
    assert commands[-1][-1] == "redis"
    assert "postgres" not in commands[-1]
    assert all("down" not in command for command in commands)
    assert all("volume" not in command for command in commands)


def test_partial_compose_failure_rolls_back_only_new_ownership(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    stopped: list[tuple[str, ...]] = []
    calls = 0

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        if "up" in command:
            raise RuntimeCommandError("safe failure")
        return subprocess.CompletedProcess(command, 0, stdout="")

    monkeypatch.setattr(
        supervisor,
        "_compose_running_services",
        lambda *_args, **_kwargs: frozenset({"postgres"}),
    )
    monkeypatch.setattr(supervisor, "_run_checked", run)
    monkeypatch.setattr(
        supervisor,
        "_stop_infrastructure",
        lambda _root, managed, **_kwargs: stopped.append(managed),
    )

    with pytest.raises(RuntimeCommandError):
        supervisor._start_infrastructure(tmp_path, timeout_seconds=30)

    assert calls == 2
    assert stopped == [("redis",)]


def test_supervisor_cleans_started_children_and_infrastructure_after_spawn_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    configuration = _configuration(tmp_path)
    handles = Mock()
    first_child = Mock(pid=123, _handle=456)
    first_child.poll.return_value = None
    spawned = 0
    shutdown_children: list[list[tuple[RuntimeProcessSpec, object]]] = []
    stopped: list[tuple[str, ...]] = []

    def spawn(*_args: object, **_kwargs: object) -> object:
        nonlocal spawned
        spawned += 1
        if spawned == 1:
            return first_child
        raise OSError("private spawn detail")

    monkeypatch.setattr(supervisor, "acquire_runtime_handles", lambda: handles)
    monkeypatch.setattr(
        supervisor,
        "_start_infrastructure",
        lambda *_args, **_kwargs: ("postgres", "redis"),
    )
    monkeypatch.setattr(supervisor, "_run_migrations", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        supervisor,
        "build_process_specs",
        lambda _configuration: (
            RuntimeProcessSpec("worker-io", ("python",)),
            RuntimeProcessSpec("beat", ("python",)),
        ),
    )
    monkeypatch.setattr(supervisor, "_spawn_child", spawn)
    monkeypatch.setattr(supervisor, "assign_process_to_job", lambda *_args: None)
    monkeypatch.setattr(
        supervisor,
        "_shutdown_children",
        lambda children, **_kwargs: shutdown_children.append(list(children)),
    )
    monkeypatch.setattr(
        supervisor,
        "_stop_infrastructure",
        lambda _root, managed, **_kwargs: stopped.append(managed),
    )

    with pytest.raises(OSError, match="private spawn detail"):
        run_supervisor(configuration, repository_root=tmp_path)

    assert len(shutdown_children) == 1 and len(shutdown_children[0]) == 1
    assert stopped == [("postgres", "redis")]
    handles.close.assert_called_once_with()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows kernel primitive")
def test_windows_singleton_stop_event_and_kill_on_close_job() -> None:
    handles = acquire_runtime_handles()
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        creationflags=supervisor._CREATE_NEW_PROCESS_GROUP,
    )
    try:
        with pytest.raises(RuntimeAlreadyRunningError):
            acquire_runtime_handles()
        assign_process_to_job(handles, int(child._handle))  # type: ignore[attr-defined]
        assert signal_runtime_stop() is True
        assert wait_for_stop(handles, timeout_ms=1000) is True
        handles.close()
        child.wait(timeout=5)
        assert child.poll() is not None
    finally:
        handles.close()
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows CTRL_BREAK lifecycle")
def test_windows_shutdown_uses_graceful_process_group_signal(tmp_path: Path) -> None:
    marker = tmp_path / "ctrl-break-received"
    ready = tmp_path / "child-ready"
    code = (
        "import os,time; from pathlib import Path\n"
        "from src.runtime.cooperative import start_stop_event_watcher\n"
        f"marker = Path({str(marker)!r})\n"
        f"ready = Path({str(ready)!r})\n"
        "def stop(*_args):\n"
        "    marker.write_text('received', encoding='utf-8')\n"
        "    os._exit(0)\n"
        "watcher = start_stop_event_watcher(stop)\n"
        "assert watcher is not None\n"
        "ready.write_text('ready', encoding='utf-8')\n"
        "time.sleep(30)\n"
    )
    logs = tmp_path / "logs"
    logs.mkdir()
    child = supervisor._spawn_child(
        RuntimeProcessSpec("worker-io", (sys.executable, "-c", code)),
        root=tmp_path,
        logs_root=logs,
    )
    try:
        for _attempt in range(50):
            if ready.exists():
                break
            supervisor.sleep(0.1)
        assert ready.read_text(encoding="utf-8") == "ready"
        supervisor._shutdown_children(
            [(RuntimeProcessSpec("worker-io", (sys.executable,)), child)],
            timeout_seconds=5,
        )
        assert child.returncode == 0
        assert marker.read_text(encoding="utf-8") == "received"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows supervisor smoke")
def test_windows_supervisor_full_isolated_start_stop_cycle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    configuration = _configuration(tmp_path)
    marker = tmp_path / "supervised-child-stopped"
    ready = tmp_path / "supervised-child-ready"
    code = (
        "import os,time; from pathlib import Path\n"
        "from src.runtime.cooperative import start_stop_event_watcher\n"
        f"marker = Path({str(marker)!r})\n"
        f"ready = Path({str(ready)!r})\n"
        "def stop():\n"
        "    marker.write_text('stopped', encoding='utf-8')\n"
        "    os._exit(0)\n"
        "watcher = start_stop_event_watcher(stop)\n"
        "assert watcher is not None\n"
        "ready.write_text('ready', encoding='utf-8')\n"
        "time.sleep(30)\n"
    )
    monkeypatch.setattr(
        supervisor,
        "_start_infrastructure",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(supervisor, "_run_migrations", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        supervisor,
        "build_process_specs",
        lambda _configuration: (
            RuntimeProcessSpec("worker-io", (sys.executable, "-c", code)),
        ),
    )
    thread_errors: list[BaseException] = []

    def request_stop() -> None:
        try:
            state_path = (
                Path(configuration.storage_root)
                / "runtime"
                / "runtime-state.json"
            )
            for _attempt in range(100):
                if state_path.exists() and ready.exists():
                    break
                supervisor.sleep(0.05)
            assert state_path.exists() and ready.exists()
            assert signal_runtime_stop() is True
        except BaseException as exc:
            thread_errors.append(exc)

    stop_thread = Thread(target=request_stop, daemon=True)
    stop_thread.start()
    run_supervisor(configuration, repository_root=tmp_path)
    stop_thread.join(timeout=5)

    assert thread_errors == []
    assert marker.read_text(encoding="utf-8") == "stopped"
    assert not (
        Path(configuration.storage_root) / "runtime" / "runtime-state.json"
    ).exists()
