"""Fail-closed registry for locally executable automation modules."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from src.platform.example_run import (
    ExampleRunResult,
    execute_example_run,
    prepare_example_run,
)
from src.platform.models import QueueClass
from src.platform.run_service import RunCreationResult

Executor = Callable[..., Awaitable[ExampleRunResult]]
Preparer = Callable[..., Awaitable[RunCreationResult]]


class AutomationExecutorNotFoundError(ValueError):
    """Raised when no local executor is explicitly registered for a slug."""


@dataclass(frozen=True, slots=True)
class AutomationExecutor:
    """Code-owned execution declaration for one automation module."""

    slug: str
    name: str
    supports_manual_start: bool
    supports_retry: bool
    queue: QueueClass
    prepare: Preparer
    execute: Executor


_EXECUTORS = {
    "platform-smoke": AutomationExecutor(
        slug="platform-smoke",
        name="Platform Smoke Automation",
        supports_manual_start=True,
        supports_retry=True,
        queue=QueueClass.IO,
        prepare=prepare_example_run,
        execute=execute_example_run,
    )
}


def list_automation_executors() -> tuple[AutomationExecutor, ...]:
    """Return registered executors in stable display order."""
    return tuple(_EXECUTORS[key] for key in sorted(_EXECUTORS))


def get_automation_executor(slug: str) -> AutomationExecutor:
    """Return one registered executor or fail closed."""
    normalized = slug.strip()
    executor = _EXECUTORS.get(normalized)
    if executor is None:
        raise AutomationExecutorNotFoundError("automation has no registered executor")
    return executor


def automation_supports_retry(slug: str) -> bool:
    """Report retry capability without treating an unknown slug as executable."""
    executor = _EXECUTORS.get(slug.strip())
    return bool(executor and executor.supports_retry)


async def start_registered_automation(
    session: AsyncSession,
    *,
    slug: str,
    idempotency_key: str,
    experiment_id: int | None = None,
) -> ExampleRunResult:
    """Start a registered manual automation inside the caller's session."""
    executor = get_automation_executor(slug)
    if not executor.supports_manual_start:
        raise AutomationExecutorNotFoundError("automation does not support manual start")
    return await executor.execute(
        session,
        idempotency_key=idempotency_key,
        experiment_id=experiment_id,
    )


async def retry_registered_automation(
    session: AsyncSession,
    *,
    slug: str,
    retry_of_run_id: int,
    idempotency_key: str,
    actor: str,
    reason: str,
) -> ExampleRunResult:
    """Create and execute one explicit successor for an eligible failed run."""
    executor = get_automation_executor(slug)
    if not executor.supports_retry:
        raise AutomationExecutorNotFoundError("automation does not support retry")
    return await executor.execute(
        session,
        idempotency_key=idempotency_key,
        retry_of_run_id=retry_of_run_id,
        retry_requested_by=actor,
        retry_reason=reason,
    )
