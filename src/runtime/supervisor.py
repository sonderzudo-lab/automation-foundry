"""Foreground Windows supervisor for the complete local Automation Foundry runtime."""

from __future__ import annotations

import ipaddress
import json
import os
import subprocess
import sys
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic, sleep
from typing import Any
from urllib.parse import urlsplit

import psutil
import structlog
from sqlalchemy.engine import make_url

from src.core.config import Settings
from src.runtime.cooperative import STOP_EVENT_ENV, signal_stop_event
from src.runtime.windows import (
    acquire_runtime_handles,
    assign_process_to_job,
    runtime_mutex_is_owned,
    wait_for_stop,
)

logger = structlog.get_logger(__name__)
_STATE_VERSION = 1
_STATE_FILENAME = "runtime-state.json"
_MAX_STATE_BYTES = 64 * 1024
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000
_COMPOSE_SERVICES = ("postgres", "redis")
_PROCESS_NAMES = frozenset({"worker-gpu", "worker-cpu", "worker-io", "beat", "dashboard"})


class RuntimeConfigurationError(ValueError):
    """Raised before mutation when local runtime configuration is unsafe."""


class RuntimeCommandError(RuntimeError):
    """Raised when an infrastructure or migration command fails safely."""


class RuntimeChildExitedError(RuntimeError):
    """Raised when a required host process exits while supervised."""


@dataclass(frozen=True, slots=True)
class RuntimeProcessSpec:
    name: str
    command: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RuntimeProcessState:
    name: str
    pid: int
    created_at: float


@dataclass(frozen=True, slots=True)
class RuntimeState:
    version: int
    supervisor_pid: int
    supervisor_created_at: float
    started_at: str
    managed_infrastructure: tuple[str, ...]
    processes: tuple[RuntimeProcessState, ...]


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    running: bool
    services: dict[str, str]


def runtime_configuration_error(
    configuration: Settings,
    *,
    platform: str = sys.platform,
) -> str | None:
    """Return one redacted preflight error, or none for a safe Windows runtime."""
    if platform != "win32":
        return "runtime supervisor requires Windows"
    database = make_url(configuration.database_url)
    if database.drivername != "postgresql+asyncpg":
        return "runtime supervisor requires PostgreSQL"
    if not database.password or database.password == "SET_IN_LOCAL_ENV":
        return "runtime supervisor requires a configured PostgreSQL password"
    for endpoint in (
        configuration.redis_url,
        configuration.celery_result_backend_url,
    ):
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"redis", "rediss"} or not _is_loopback(parsed.hostname):
            return "runtime Redis endpoints must use loopback"
    storage_root = Path(configuration.storage_root).resolve()
    if storage_root == Path(storage_root.anchor):
        return "runtime storage root must not be a drive root"
    return None


def build_process_specs(
    configuration: Settings,
    *,
    python_executable: str = sys.executable,
) -> tuple[RuntimeProcessSpec, ...]:
    """Build the fixed host process topology without shell interpolation."""
    del configuration
    return (
        RuntimeProcessSpec(
            "worker-gpu",
            (python_executable, "-m", "src.worker_cli", "--queue", "gpu"),
        ),
        RuntimeProcessSpec(
            "worker-cpu",
            (python_executable, "-m", "src.worker_cli", "--queue", "cpu"),
        ),
        RuntimeProcessSpec(
            "worker-io",
            (python_executable, "-m", "src.worker_cli", "--queue", "io"),
        ),
        RuntimeProcessSpec(
            "beat",
            (python_executable, "-m", "src.beat_cli"),
        ),
        RuntimeProcessSpec(
            "dashboard",
            (python_executable, "-m", "src.cli", "dashboard"),
        ),
    )


def run_supervisor(
    configuration: Settings,
    *,
    repository_root: Path | None = None,
) -> None:
    """Run one foreground supervisor until stop, Ctrl+C, or child failure."""
    error = runtime_configuration_error(configuration)
    if error is not None:
        raise RuntimeConfigurationError(error)
    root = (repository_root or Path(__file__).resolve().parents[2]).resolve()
    runtime_root = _runtime_root(configuration)
    logs_root = runtime_root / "logs"
    runtime_root.mkdir(parents=True, exist_ok=True)
    if runtime_root.is_symlink():
        raise RuntimeConfigurationError("runtime state directory must not be a symlink")
    logs_root.mkdir(parents=True, exist_ok=True)
    if logs_root.is_symlink():
        raise RuntimeConfigurationError("runtime log directory must not be a symlink")

    handles = acquire_runtime_handles()
    managed_infrastructure: tuple[str, ...] = ()
    children: list[tuple[RuntimeProcessSpec, subprocess.Popen[bytes]]] = []
    state_path = runtime_root / _STATE_FILENAME
    try:
        managed_infrastructure = _start_infrastructure(
            root,
            timeout_seconds=configuration.runtime_startup_timeout_seconds,
        )
        _run_migrations(
            root,
            timeout_seconds=configuration.runtime_startup_timeout_seconds,
        )
        for spec in build_process_specs(configuration):
            child = _spawn_child(spec, root=root, logs_root=logs_root)
            try:
                process_handle = int(child._handle)  # type: ignore[attr-defined]
                assign_process_to_job(handles, process_handle)
            except Exception:
                _force_end_child(child)
                raise
            children.append((spec, child))

        state = _runtime_state(children, managed_infrastructure)
        _write_state(state_path, state)
        logger.info(
            "runtime_started",
            managed_infrastructure=list(managed_infrastructure),
            services=[spec.name for spec, _child in children],
        )
        while True:
            if wait_for_stop(handles, timeout_ms=500):
                logger.info("runtime_stop_requested")
                break
            exited = [spec.name for spec, child in children if child.poll() is not None]
            if exited:
                raise RuntimeChildExitedError(
                    "required runtime process exited: " + ", ".join(exited)
                )
    except KeyboardInterrupt:
        logger.info("runtime_keyboard_interrupt")
    finally:
        _shutdown_children(
            children,
            timeout_seconds=configuration.runtime_shutdown_timeout_seconds,
        )
        _stop_infrastructure(
            root,
            managed_infrastructure,
            timeout_seconds=configuration.runtime_shutdown_timeout_seconds,
        )
        state_path.unlink(missing_ok=True)
        handles.close()
        logger.info("runtime_stopped")


def load_runtime_status(configuration: Settings) -> RuntimeStatus:
    """Return a redacted projection protected against PID reuse."""
    state = _read_state(_runtime_root(configuration) / _STATE_FILENAME)
    mutex_owned = runtime_mutex_is_owned()
    if state is None:
        return RuntimeStatus(running=mutex_owned, services={})
    supervisor_matches = _process_matches(
        state.supervisor_pid,
        state.supervisor_created_at,
    )
    running = mutex_owned and supervisor_matches
    services: dict[str, str] = {}
    for process in state.processes:
        matches = _process_matches(process.pid, process.created_at)
        if running and matches:
            services[process.name] = "running"
        elif matches:
            services[process.name] = "orphaned"
        else:
            services[process.name] = "stopped"
    for service in _COMPOSE_SERVICES:
        services[service] = (
            "managed-by-runtime"
            if service in state.managed_infrastructure
            else "not-owned"
        )
    return RuntimeStatus(running=running, services=services)


def _runtime_root(configuration: Settings) -> Path:
    return Path(configuration.storage_root).resolve() / "runtime"


def _start_infrastructure(root: Path, *, timeout_seconds: int) -> tuple[str, ...]:
    running_before = _compose_running_services(root, timeout_seconds=timeout_seconds)
    managed = tuple(service for service in _COMPOSE_SERVICES if service not in running_before)
    try:
        _run_checked(
            ["docker", "compose", "config", "--quiet"],
            cwd=root,
            timeout_seconds=timeout_seconds,
        )
        _run_checked(
            [
                "docker",
                "compose",
                "up",
                "-d",
                "--wait",
                "--wait-timeout",
                str(timeout_seconds),
                *_COMPOSE_SERVICES,
            ],
            cwd=root,
            timeout_seconds=timeout_seconds + 30,
        )
    except RuntimeCommandError:
        _stop_infrastructure(
            root,
            managed,
            timeout_seconds=timeout_seconds,
        )
        raise
    return managed


def _stop_infrastructure(
    root: Path,
    managed_services: tuple[str, ...],
    *,
    timeout_seconds: int,
) -> None:
    if not managed_services:
        return
    ordered = tuple(service for service in ("redis", "postgres") if service in managed_services)
    try:
        _run_checked(
            [
                "docker",
                "compose",
                "stop",
                "--timeout",
                str(timeout_seconds),
                *ordered,
            ],
            cwd=root,
            timeout_seconds=timeout_seconds + 30,
        )
    except RuntimeCommandError:
        logger.exception("runtime_infrastructure_stop_failed", services=list(ordered))


def _compose_running_services(root: Path, *, timeout_seconds: int) -> frozenset[str]:
    result = _run_checked(
        ["docker", "compose", "ps", "--status", "running", "--services"],
        cwd=root,
        timeout_seconds=timeout_seconds,
    )
    return frozenset(line.strip() for line in result.stdout.splitlines() if line.strip())


def _run_migrations(root: Path, *, timeout_seconds: int) -> None:
    _run_checked(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=root,
        timeout_seconds=timeout_seconds,
    )


def _run_checked(
    command: list[str],
    *,
    cwd: Path,
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeCommandError("local runtime command failed") from exc


def _spawn_child(
    spec: RuntimeProcessSpec,
    *,
    root: Path,
    logs_root: Path,
) -> subprocess.Popen[bytes]:
    log_path = logs_root / f"{spec.name}.log"
    if log_path.is_symlink():
        raise RuntimeCommandError("runtime log path must not be a symlink")
    child_environment = os.environ.copy()
    child_environment[STOP_EVENT_ENV] = _child_stop_event(spec.name)
    with log_path.open("ab", buffering=0) as log_file:
        return subprocess.Popen(
            list(spec.command),
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            close_fds=True,
            creationflags=_CREATE_NO_WINDOW,
            env=child_environment,
        )


def _shutdown_children(
    children: list[tuple[RuntimeProcessSpec, subprocess.Popen[bytes]]],
    *,
    timeout_seconds: int,
) -> None:
    active = [child for _spec, child in reversed(children) if child.poll() is None]
    pending_signals = [
        (spec, child)
        for spec, child in reversed(children)
        if child.poll() is None
    ]
    signal_deadline = monotonic() + min(2, timeout_seconds)
    while pending_signals and monotonic() < signal_deadline:
        pending_signals = [
            (spec, child)
            for spec, child in pending_signals
            if child.poll() is None
            and not signal_stop_event(_child_stop_event(spec.name))
        ]
        if pending_signals:
            sleep(0.05)
    deadline = monotonic() + timeout_seconds
    while active and monotonic() < deadline:
        active = [child for child in active if child.poll() is None]
        if active:
            sleep(0.1)
    for child in active:
        child.terminate()
    terminate_deadline = monotonic() + 5
    while active and monotonic() < terminate_deadline:
        active = [child for child in active if child.poll() is None]
        if active:
            sleep(0.1)
    for child in active:
        child.kill()
    for _spec, child in children:
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.error("runtime_child_did_not_exit", service=_spec.name)


def _child_stop_event(service_name: str) -> str:
    if service_name not in _PROCESS_NAMES:
        raise ValueError("runtime service name is not allowlisted")
    return f"Local\\AutomationFoundryRuntimeChildStop-{os.getpid()}-{service_name}"


def _force_end_child(child: subprocess.Popen[bytes]) -> None:
    if child.poll() is None:
        child.kill()
    with suppress(subprocess.TimeoutExpired):
        child.wait(timeout=5)


def _runtime_state(
    children: list[tuple[RuntimeProcessSpec, subprocess.Popen[bytes]]],
    managed_infrastructure: tuple[str, ...],
) -> RuntimeState:
    current = psutil.Process(os.getpid())
    processes = tuple(
        RuntimeProcessState(
            name=spec.name,
            pid=child.pid,
            created_at=psutil.Process(child.pid).create_time(),
        )
        for spec, child in children
    )
    return RuntimeState(
        version=_STATE_VERSION,
        supervisor_pid=current.pid,
        supervisor_created_at=current.create_time(),
        started_at=datetime.now(UTC).isoformat(),
        managed_infrastructure=managed_infrastructure,
        processes=processes,
    )


def _write_state(path: Path, state: RuntimeState) -> None:
    payload = asdict(state)
    temporary = path.with_suffix(".tmp")
    if path.is_symlink() or temporary.is_symlink():
        raise RuntimeCommandError("runtime state path must not be a symlink")
    temporary.write_text(
        json.dumps(payload, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    temporary.replace(path)


def _read_state(path: Path) -> RuntimeState | None:
    try:
        if path.stat().st_size > _MAX_STATE_BYTES:
            return None
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("version") != _STATE_VERSION:
            return None
        processes_raw = payload.get("processes")
        managed_raw = payload.get("managed_infrastructure")
        if not isinstance(processes_raw, list) or not isinstance(managed_raw, list):
            return None
        processes = tuple(
            RuntimeProcessState(
                name=str(item["name"]),
                pid=int(item["pid"]),
                created_at=float(item["created_at"]),
            )
            for item in processes_raw
            if isinstance(item, dict)
        )
        if len(processes) != len(processes_raw):
            return None
        process_names = [process.name for process in processes]
        if (
            any(name not in _PROCESS_NAMES for name in process_names)
            or len(process_names) != len(set(process_names))
            or any(process.pid <= 0 or process.created_at < 0 for process in processes)
        ):
            return None
        managed = tuple(str(item) for item in managed_raw)
        if (
            any(item not in _COMPOSE_SERVICES for item in managed)
            or len(managed) != len(set(managed))
        ):
            return None
        supervisor_pid = int(payload["supervisor_pid"])
        supervisor_created_at = float(payload["supervisor_created_at"])
        started_at = str(payload["started_at"])
        if supervisor_pid <= 0 or supervisor_created_at < 0 or len(started_at) > 100:
            return None
        return RuntimeState(
            version=_STATE_VERSION,
            supervisor_pid=supervisor_pid,
            supervisor_created_at=supervisor_created_at,
            started_at=started_at,
            managed_infrastructure=managed,
            processes=processes,
        )
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def _process_matches(pid: int, created_at: float) -> bool:
    try:
        process = psutil.Process(pid)
        return bool(process.is_running()) and abs(process.create_time() - created_at) < 0.01
    except (psutil.Error, OSError):
        return False


def _is_loopback(host: str | None) -> bool:
    if host is None:
        return False
    if host.casefold() == "localhost":
        return True
    try:
        return bool(ipaddress.ip_address(host).is_loopback)
    except ValueError:
        return False
