"""Side-effect-free worker probes for validating each local queue."""

from __future__ import annotations

from typing import cast

from celery import Celery, Task

_MAX_NONCE_LENGTH = 100


def _probe_payload(queue: str, nonce: str) -> dict[str, str | bool]:
    normalized = nonce.strip()
    if not normalized or len(normalized) > _MAX_NONCE_LENGTH:
        raise ValueError("probe nonce must contain between 1 and 100 characters")
    return {"ok": True, "queue": queue, "nonce": normalized}


def _probe_gpu(nonce: str) -> dict[str, str | bool]:
    return _probe_payload("gpu", nonce)


def _probe_cpu(nonce: str) -> dict[str, str | bool]:
    return _probe_payload("cpu", nonce)


def _probe_io(nonce: str) -> dict[str, str | bool]:
    return _probe_payload("io", nonce)


def register_probe_tasks(app: Celery) -> dict[str, Task]:
    """Register one explicit ephemeral-result probe per worker queue."""
    return {
        "gpu": cast(
            Task,
            app.task(name="automation_foundry.probe.gpu", ignore_result=False)(
                _probe_gpu
            ),
        ),
        "cpu": cast(
            Task,
            app.task(name="automation_foundry.probe.cpu", ignore_result=False)(
                _probe_cpu
            ),
        ),
        "io": cast(
            Task,
            app.task(name="automation_foundry.probe.io", ignore_result=False)(
                _probe_io
            ),
        ),
    }
