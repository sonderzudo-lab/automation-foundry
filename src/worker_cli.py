"""Safe local worker entrypoint with one serial Windows worker per queue."""

from __future__ import annotations

import argparse
import socket
import sys
from collections.abc import Sequence

from sqlalchemy.engine import make_url

from src.core.celery_app import QUEUE_NAMES, celery_app
from src.core.config import Settings, get_settings
from src.runtime.cooperative import start_stop_event_watcher


def worker_arguments(queue: str, *, log_level: str) -> list[str]:
    """Return the fixed worker arguments for one isolated local queue."""
    if queue not in QUEUE_NAMES:
        raise ValueError("worker queue must be gpu, cpu, or io")
    return [
        "worker",
        f"--queues={queue}",
        f"--hostname={queue}@%h",
        "--pool=solo",
        "--concurrency=1",
        f"--loglevel={log_level.upper()}",
    ]


def _worker_configuration_error(configuration: Settings) -> str | None:
    if make_url(configuration.database_url).drivername != "postgresql+asyncpg":
        return "workers require PostgreSQL; SQLite is limited to single-process bootstrap"
    return None


def main(argv: Sequence[str] | None = None) -> int:
    """Start one queue-isolated worker without exposing pool or concurrency overrides."""
    parser = argparse.ArgumentParser(prog="automation-foundry-worker")
    parser.add_argument("--queue", required=True, choices=QUEUE_NAMES)
    args = parser.parse_args(list(argv) if argv is not None else None)
    configuration = get_settings()
    error = _worker_configuration_error(configuration)
    if error is not None:
        print(f"Worker configuration error: {error}", file=sys.stderr)
        return 2

    worker = celery_app.Worker(
        hostname=f"{args.queue}@{socket.gethostname()}",
        queues=[args.queue],
        pool="solo",
        concurrency=1,
        loglevel=configuration.log_level.upper(),
    )
    watcher = start_stop_event_watcher(worker.stop)
    try:
        worker.start()
    finally:
        if watcher is not None:
            watcher.close()
    return int(worker.exitcode or 0)


if __name__ == "__main__":
    raise SystemExit(main())
