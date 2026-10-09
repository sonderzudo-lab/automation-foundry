"""Redacted, read-only health probes for the local control plane."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

import psutil
from celery import Celery
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import Settings, settings

type HealthMetric = str | int | float | bool | None

_CELERY_INSPECT_TIMEOUT_MARGIN_SECONDS = 2.0


class HealthStatus(StrEnum):
    PASS = "pass"
    DEGRADED = "degraded"
    FAIL = "fail"
    SKIP = "skip"


@dataclass(frozen=True, slots=True)
class HealthCheck:
    name: str
    status: HealthStatus
    summary: str
    metrics: dict[str, HealthMetric]


@dataclass(frozen=True, slots=True)
class HealthReport:
    status: HealthStatus
    checked_at: datetime
    checks: tuple[HealthCheck, ...]


async def collect_health_report(
    session: AsyncSession,
    *,
    configuration: Settings = settings,
    celery: Celery | None = None,
    assumed_worker_queues: frozenset[str] = frozenset(),
) -> HealthReport:
    """Collect independent local probes concurrently without exposing endpoints."""
    if celery is None:
        from src.core.celery_app import celery_app

        celery = celery_app
    timeout = configuration.health_probe_timeout_seconds
    machine, database, redis, beat, gpu = await asyncio.gather(
        _safe_machine_probe(probe_machine(configuration)),
        _safe_probe("database", probe_database(session, timeout=timeout)),
        _safe_probe("redis", probe_redis(configuration)),
        _safe_probe("beat", probe_beat(configuration)),
        _safe_probe("gpu", probe_gpu(timeout=timeout, configuration=configuration)),
    )
    if redis.status is HealthStatus.PASS:
        workers = await _safe_probe(
            "workers",
            probe_workers(
                celery,
                timeout=timeout,
                assumed_queues=assumed_worker_queues,
            ),
        )
    else:
        workers = HealthCheck(
            name="workers",
            status=HealthStatus.SKIP,
            summary="não consultado porque Redis está indisponível",
            metrics={"gpu_workers": 0, "cpu_workers": 0, "io_workers": 0},
        )
    checks = (*machine, database, redis, workers, beat, gpu)
    statuses = {check.status for check in checks}
    if HealthStatus.FAIL in statuses:
        overall = HealthStatus.FAIL
    elif HealthStatus.DEGRADED in statuses:
        overall = HealthStatus.DEGRADED
    else:
        overall = HealthStatus.PASS
    return HealthReport(
        status=overall,
        checked_at=datetime.now(UTC).replace(tzinfo=None),
        checks=checks,
    )


async def _safe_probe(
    name: str,
    probe: Awaitable[HealthCheck],
) -> HealthCheck:
    try:
        return await probe
    except Exception:
        return HealthCheck(
            name=name,
            status=HealthStatus.FAIL,
            summary="probe local falhou",
            metrics={},
        )


async def _safe_machine_probe(
    probe: Awaitable[tuple[HealthCheck, ...]],
) -> tuple[HealthCheck, ...]:
    try:
        return await probe
    except Exception:
        return (
            HealthCheck(
                name="machine",
                status=HealthStatus.FAIL,
                summary="probe local falhou",
                metrics={},
            ),
        )


async def probe_machine(configuration: Settings) -> tuple[HealthCheck, ...]:
    """Read CPU, memory, and storage capacity without enumerating processes."""

    def read() -> tuple[float, int, float, float, int, int]:
        cpu_percent = psutil.cpu_percent(interval=0.1)
        memory = psutil.virtual_memory()
        storage_path = Path(configuration.storage_root).resolve()
        disk = shutil.disk_usage(storage_path)
        disk_percent = (disk.used / disk.total * 100) if disk.total else 100.0
        return (
            cpu_percent,
            os.cpu_count() or 1,
            memory.percent,
            disk_percent,
            memory.available,
            disk.free,
        )

    cpu, logical_cpus, memory, disk, available_memory, free_disk = await asyncio.to_thread(
        read
    )
    return (
        HealthCheck(
            name="cpu",
            status=_capacity_status(cpu, configuration),
            summary="uso atual da CPU",
            metrics={"percent": round(cpu, 1), "logical_cpus": logical_cpus},
        ),
        HealthCheck(
            name="memory",
            status=_capacity_status(memory, configuration),
            summary="uso atual da memória",
            metrics={
                "percent": round(memory, 1),
                "available_gib": round(available_memory / 1024**3, 2),
            },
        ),
        HealthCheck(
            name="disk",
            status=_capacity_status(disk, configuration),
            summary="uso do volume de storage",
            metrics={
                "percent": round(disk, 1),
                "free_gib": round(free_disk / 1024**3, 2),
            },
        ),
    )


async def probe_database(session: AsyncSession, *, timeout: float) -> HealthCheck:
    """Verify the configured SQL database through the request session."""
    started = asyncio.get_running_loop().time()
    async with asyncio.timeout(timeout):
        value = await session.scalar(text("SELECT 1"))
    if value != 1:
        raise RuntimeError("database probe returned an unexpected value")
    latency = (asyncio.get_running_loop().time() - started) * 1000
    return HealthCheck(
        name="database",
        status=HealthStatus.PASS,
        summary="consulta local concluída",
        metrics={"latency_ms": round(latency, 1)},
    )


async def probe_redis(configuration: Settings) -> HealthCheck:
    """Ping Redis with bounded socket timeouts and no URL disclosure."""
    timeout = configuration.health_probe_timeout_seconds
    client = Redis.from_url(
        configuration.redis_url,
        socket_connect_timeout=timeout,
        socket_timeout=timeout,
        decode_responses=True,
    )
    started = asyncio.get_running_loop().time()
    try:
        async with asyncio.timeout(timeout):
            pong = await client.ping()
    finally:
        await client.aclose()
    if pong is not True:
        raise RuntimeError("redis probe did not return pong")
    latency = (asyncio.get_running_loop().time() - started) * 1000
    return HealthCheck(
        name="redis",
        status=HealthStatus.PASS,
        summary="broker respondeu ao ping",
        metrics={"latency_ms": round(latency, 1)},
    )


async def probe_workers(
    celery: Celery,
    *,
    timeout: float,
    assumed_queues: frozenset[str] = frozenset(),
) -> HealthCheck:
    """Inspect active queue bindings without exposing worker hostnames."""

    def inspect() -> object:
        return celery.control.inspect(timeout=timeout).active_queues()

    replies = await asyncio.wait_for(
        asyncio.to_thread(inspect),
        timeout=timeout + _CELERY_INSPECT_TIMEOUT_MARGIN_SECONDS,
    )
    counts = _worker_queue_counts(replies, assumed_queues=assumed_queues)
    missing = [queue for queue in ("gpu", "cpu", "io") if counts[queue] == 0]
    status = HealthStatus.PASS if not missing else HealthStatus.FAIL
    return HealthCheck(
        name="workers",
        status=status,
        summary=(
            "todas as filas possuem worker"
            if not missing
            else "filas sem worker: " + ", ".join(missing)
        ),
        metrics={f"{queue}_workers": counts[queue] for queue in ("gpu", "cpu", "io")},
    )


def _worker_queue_counts(
    replies: object,
    *,
    assumed_queues: frozenset[str] = frozenset(),
) -> dict[str, int]:
    counts = {"gpu": 0, "cpu": 0, "io": 0}
    if not isinstance(replies, dict):
        return counts
    for queues in replies.values():
        if not isinstance(queues, list):
            continue
        seen: set[str] = set()
        for queue in queues:
            if isinstance(queue, dict) and isinstance(queue.get("name"), str):
                seen.add(queue["name"])
        for name in counts.keys() & seen:
            counts[name] += 1
    for name in counts.keys() & assumed_queues:
        counts[name] = max(counts[name], 1)
    return counts


async def probe_beat(configuration: Settings) -> HealthCheck:
    """Check the Windows mutex or the Celery PID file without changing Beat state."""
    if sys.platform == "win32":
        from src.beat_cli import BeatAlreadyRunningError, acquire_beat_singleton

        try:
            mutex = acquire_beat_singleton()
        except BeatAlreadyRunningError:
            return HealthCheck(
                name="beat",
                status=HealthStatus.PASS,
                summary="singleton Beat ativo",
                metrics={"singleton_owned": True},
            )
        if mutex is not None:
            mutex.release()
        return HealthCheck(
            name="beat",
            status=HealthStatus.FAIL,
            summary="singleton Beat não está ativo",
            metrics={"singleton_owned": False},
        )

    pidfile = Path(configuration.storage_root).resolve() / "celerybeat.pid"
    try:
        pid = int(pidfile.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
    except (OSError, ValueError):
        return HealthCheck(
            name="beat",
            status=HealthStatus.FAIL,
            summary="PID do Beat não está ativo",
            metrics={"pid_active": False},
        )
    return HealthCheck(
        name="beat",
        status=HealthStatus.PASS,
        summary="PID do Beat está ativo",
        metrics={"pid_active": True},
    )


async def probe_gpu(*, timeout: float, configuration: Settings) -> HealthCheck:
    """Read allowlisted NVIDIA capacity fields with a bounded subprocess."""
    try:
        process = await asyncio.create_subprocess_exec(
            "nvidia-smi",
            "--query-gpu=name,memory.total,memory.used,utilization.gpu",
            "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return HealthCheck(
            name="gpu",
            status=HealthStatus.SKIP,
            summary="nvidia-smi não encontrado",
            metrics={"detected": False},
        )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    if process.returncode != 0:
        raise RuntimeError("nvidia-smi probe failed")
    rows = _parse_gpu_rows(stdout.decode("utf-8", errors="replace"))
    if not rows:
        return HealthCheck(
            name="gpu",
            status=HealthStatus.SKIP,
            summary="nenhuma GPU NVIDIA detectada",
            metrics={"detected": False},
        )
    total = sum(row[1] for row in rows)
    used = sum(row[2] for row in rows)
    memory_percent = used / total * 100 if total else 100.0
    return HealthCheck(
        name="gpu",
        status=_capacity_status(memory_percent, configuration),
        summary="capacidade NVIDIA disponível",
        metrics={
            "detected": True,
            "count": len(rows),
            "model": rows[0][0] if len(rows) == 1 else "multiple",
            "memory_percent": round(memory_percent, 1),
            "memory_used_mib": used,
            "memory_total_mib": total,
            "utilization_percent": max(row[3] for row in rows),
        },
    )


def _parse_gpu_rows(output: str) -> list[tuple[str, int, int, int]]:
    rows: list[tuple[str, int, int, int]] = []
    for line in output.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 4:
            continue
        try:
            rows.append((parts[0][:100], int(parts[1]), int(parts[2]), int(parts[3])))
        except ValueError:
            continue
    return rows


def _capacity_status(percent: float, configuration: Settings) -> HealthStatus:
    if percent >= configuration.health_critical_percent:
        return HealthStatus.FAIL
    if percent >= configuration.health_warning_percent:
        return HealthStatus.DEGRADED
    return HealthStatus.PASS
