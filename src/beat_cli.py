"""Singleton local Celery Beat entrypoint for database schedule ticks."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from celery import platforms
from celery.beat import Service
from sqlalchemy.engine import make_url

from src.core.celery_app import celery_app
from src.core.config import Settings, get_settings
from src.runtime.cooperative import start_stop_event_watcher

_WINDOWS_MUTEX_NAME = "Local\\AutomationFoundryCeleryBeat"
_ERROR_ALREADY_EXISTS = 183


class BeatAlreadyRunningError(RuntimeError):
    """Raised when another local Beat process owns the singleton mutex."""


@dataclass(slots=True)
class _WindowsMutex:
    kernel32: Any
    handle: int

    def release(self) -> None:
        """Release the process-lifetime handle once."""
        if self.handle:
            self.kernel32.CloseHandle(self.handle)
            self.handle = 0


def acquire_beat_singleton() -> _WindowsMutex | None:
    """Acquire the Windows named mutex; POSIX continues using Celery's PID lock."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [
        wintypes.LPVOID,
        wintypes.BOOL,
        wintypes.LPCWSTR,
    ]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.CreateMutexW(None, False, _WINDOWS_MUTEX_NAME)
    if not handle:
        error = ctypes.get_last_error()
        raise OSError(error, "could not create Beat singleton mutex")
    if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        raise BeatAlreadyRunningError("another Automation Foundry Beat is running")
    return _WindowsMutex(kernel32=kernel32, handle=int(handle))


def beat_arguments(storage_root: Path, *, log_level: str) -> list[str]:
    """Return fixed Beat arguments with local singleton and state files."""
    return [
        "beat",
        f"--loglevel={log_level.upper()}",
        f"--pidfile={storage_root / 'celerybeat.pid'}",
        f"--schedule={storage_root / 'celerybeat-schedule'}",
    ]


def _beat_configuration_error(configuration: Settings) -> str | None:
    if make_url(configuration.database_url).drivername != "postgresql+asyncpg":
        return "Beat requires PostgreSQL; SQLite is limited to single-process bootstrap"
    return None


def main(argv: Sequence[str] | None = None) -> int:
    """Start one fixed local Beat process guarded by a PID file."""
    if argv:
        print("Beat does not accept runtime overrides", file=sys.stderr)
        return 2
    configuration = get_settings()
    error = _beat_configuration_error(configuration)
    if error is not None:
        print(f"Beat configuration error: {error}", file=sys.stderr)
        return 2
    storage_root = Path(configuration.storage_root).resolve()
    storage_root.mkdir(parents=True, exist_ok=True)
    try:
        singleton = acquire_beat_singleton()
    except BeatAlreadyRunningError as exc:
        print(f"Beat configuration error: {exc}", file=sys.stderr)
        return 2
    try:
        celery_app.log.setup_logging_subsystem(
            loglevel=configuration.log_level.upper(),
        )
        pidfile = storage_root / "celerybeat.pid"
        if singleton is not None:
            # Owning the named mutex proves no other Beat is alive, so a leftover
            # pidfile is orphaned. Celery cannot tell on Windows: os.kill(pid, 0)
            # raises EINVAL for a dead PID and it reports "already running".
            pidfile.unlink(missing_ok=True)
        pidlock = platforms.create_pidlock(str(pidfile))
        service = Service(
            app=celery_app,
            max_interval=1.0,
            schedule_filename=str(storage_root / "celerybeat-schedule"),
        )
        watcher = start_stop_event_watcher(service.stop)
        try:
            service.start()
        finally:
            if watcher is not None:
                watcher.close()
            service.stop()
            pidlock.release()
    finally:
        if singleton is not None:
            singleton.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
