"""Fail-closed registry for locally executable automation modules."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from src.briefs.executor import (
    execute_operations_brief_run,
    finalize_operations_brief_approval,
    load_operations_brief_review,
    parse_operations_brief_form,
    prepare_operations_brief_run,
    resolve_operations_brief_schedule_input,
)
from src.pipeline.a1_executor import (
    execute_content_script_run,
    finalize_content_script_approval,
    load_content_approval_review,
    parse_content_script_form,
    prepare_content_script_run,
)
from src.platform.example_run import execute_example_run, prepare_example_run
from src.platform.models import Approval, Automation, QueueClass, Run
from src.platform.run_service import RunCreationResult


class AutomationRunResult(Protocol):
    """Minimum result returned by every registered automation executor."""

    @property
    def run_id(self) -> int: ...

    @property
    def run_status(self) -> str: ...


Executor = Callable[..., Awaitable[AutomationRunResult]]
Preparer = Callable[..., Awaitable[RunCreationResult]]
ManualInputParser = Callable[[dict[str, str]], dict[str, object]]
ScheduledInputResolver = Callable[[dict[str, object], datetime], dict[str, object]]


class ApprovalFinalizer(Protocol):
    async def __call__(
        self,
        session: AsyncSession,
        *,
        approval: Approval,
    ) -> bool: ...


class ApprovalReviewLoader(Protocol):
    async def __call__(
        self,
        session: AsyncSession,
        *,
        approval: Approval,
    ) -> object: ...


class AutomationExecutorNotFoundError(ValueError):
    """Raised when no local executor is explicitly registered for a slug."""


@dataclass(frozen=True, slots=True)
class ManualInputField:
    """Allowlisted dashboard field declared by a local executor."""

    name: str
    label: str
    control: Literal["text", "textarea", "select"]
    required: bool
    max_length: int
    options: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class AutomationExecutor:
    """Code-owned execution declaration for one automation module."""

    slug: str
    name: str
    supports_manual_start: bool
    supports_retry: bool
    runs_in_background: bool
    queue: QueueClass
    manual_input_fields: tuple[ManualInputField, ...]
    parse_manual_input: ManualInputParser
    prepare: Preparer
    execute: Executor
    finalize_approval: ApprovalFinalizer | None = None
    load_approval_review: ApprovalReviewLoader | None = None
    # Derives the run input of one schedule occurrence from the schedule's stored
    # payload and the occurrence time. It must be a pure function of its arguments so
    # a replayed tick reproduces exactly the input already persisted.
    resolve_scheduled_input: ScheduledInputResolver | None = None


_EXECUTORS = {
    "platform-smoke": AutomationExecutor(
        slug="platform-smoke",
        name="Platform Smoke Automation",
        supports_manual_start=True,
        supports_retry=True,
        runs_in_background=False,
        queue=QueueClass.IO,
        manual_input_fields=(),
        parse_manual_input=lambda values: _parse_empty_manual_input(values),
        prepare=prepare_example_run,
        execute=execute_example_run,
    ),
    "content-engine": AutomationExecutor(
        slug="content-engine",
        name="Content Engine · roteiro A1",
        supports_manual_start=True,
        supports_retry=True,
        runs_in_background=True,
        queue=QueueClass.GPU,
        manual_input_fields=(
            ManualInputField("topic", "Tema ou pauta", "textarea", True, 500),
            ManualInputField("persona", "Persona editorial", "textarea", True, 1_000),
            ManualInputField(
                "format",
                "Formato",
                "select",
                True,
                5,
                (("short", "Short · até 60 s"), ("long", "Longo · 10+ min")),
            ),
            ManualInputField(
                "recent_openings",
                "Aberturas recentes, uma por linha (opcional)",
                "textarea",
                False,
                2_000,
            ),
        ),
        parse_manual_input=parse_content_script_form,
        prepare=prepare_content_script_run,
        execute=execute_content_script_run,
        finalize_approval=finalize_content_script_approval,
        load_approval_review=load_content_approval_review,
    ),
    "operations-brief": AutomationExecutor(
        slug="operations-brief",
        name="Operations Brief",
        supports_manual_start=True,
        supports_retry=True,
        runs_in_background=True,
        queue=QueueClass.IO,
        manual_input_fields=(
            ManualInputField(
                "window_days",
                "Janela do brief",
                "select",
                True,
                2,
                (
                    ("1", "Último dia"),
                    ("7", "Últimos 7 dias"),
                    ("14", "Últimos 14 dias"),
                    ("30", "Últimos 30 dias"),
                ),
            ),
        ),
        parse_manual_input=parse_operations_brief_form,
        prepare=prepare_operations_brief_run,
        execute=execute_operations_brief_run,
        finalize_approval=finalize_operations_brief_approval,
        load_approval_review=load_operations_brief_review,
        resolve_scheduled_input=resolve_operations_brief_schedule_input,
    ),
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


def automation_has_executor(slug: str) -> bool:
    """Report whether a slug has an explicitly registered local executor."""
    return slug.strip() in _EXECUTORS


def parse_registered_manual_input(
    *,
    slug: str,
    values: dict[str, str],
) -> dict[str, object]:
    """Validate allowlisted manual fields through the executor-owned schema."""
    executor = get_automation_executor(slug)
    expected = {field.name for field in executor.manual_input_fields}
    if set(values) != expected:
        raise ValueError("manual input fields do not match executor declaration")
    return executor.parse_manual_input(values)


async def start_registered_automation(
    session: AsyncSession,
    *,
    slug: str,
    idempotency_key: str,
    experiment_id: int | None = None,
    input_payload: dict[str, object] | None = None,
) -> AutomationRunResult:
    """Start a registered manual automation inside the caller's session."""
    executor = get_automation_executor(slug)
    if not executor.supports_manual_start:
        raise AutomationExecutorNotFoundError("automation does not support manual start")
    return await executor.execute(
        session,
        idempotency_key=idempotency_key,
        experiment_id=experiment_id,
        input_payload=input_payload,
    )


async def retry_registered_automation(
    session: AsyncSession,
    *,
    slug: str,
    retry_of_run_id: int,
    idempotency_key: str,
    actor: str,
    reason: str,
) -> AutomationRunResult:
    """Create and execute one explicit successor for an eligible failed run."""
    executor = get_automation_executor(slug)
    if not executor.supports_retry:
        raise AutomationExecutorNotFoundError("automation does not support retry")
    source = await session.get(Run, retry_of_run_id)
    if source is None:
        raise ValueError("retry source does not exist")
    return await executor.execute(
        session,
        idempotency_key=idempotency_key,
        retry_of_run_id=retry_of_run_id,
        retry_requested_by=actor,
        retry_reason=reason,
        input_payload=source.input_payload,
    )


async def finalize_registered_approval(
    session: AsyncSession,
    *,
    approval: Approval,
) -> bool:
    """Run an executor-owned post-approval transition, or safely do nothing."""
    executor = await _executor_for_approval(session, approval=approval)
    if executor is None or executor.finalize_approval is None:
        return False
    return await executor.finalize_approval(session, approval=approval)


async def load_registered_approval_review(
    session: AsyncSession,
    *,
    approval: Approval,
) -> object:
    """Load an executor-owned complete review projection for one approval."""
    executor = await _executor_for_approval(session, approval=approval)
    if executor is None or executor.load_approval_review is None:
        raise AutomationExecutorNotFoundError(
            "approval has no registered review projection"
        )
    return await executor.load_approval_review(session, approval=approval)


async def _executor_for_approval(
    session: AsyncSession,
    *,
    approval: Approval,
) -> AutomationExecutor | None:
    run = await session.get(Run, approval.run_id)
    if run is None:
        raise ValueError("approval run does not exist")
    automation = await session.get(Automation, run.automation_id)
    if automation is None:
        raise ValueError("approval automation does not exist")
    return _EXECUTORS.get(automation.slug)


def _parse_empty_manual_input(values: dict[str, str]) -> dict[str, object]:
    if values:
        raise ValueError("platform smoke does not accept manual inputs")
    return {}
