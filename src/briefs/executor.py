"""Operations brief executor: manual, local, redacted, with one human approval gate."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.briefs.evidence import (
    ALLOWED_WINDOW_DAYS,
    EvidenceWindow,
    build_window,
    collect_operations_evidence,
)
from src.briefs.render import OperationsBriefRenderError, render_operations_brief
from src.core.config import settings
from src.operations.retention_policy import resolve_retention_days
from src.platform.approval_service import approval_payload_digest, request_approval
from src.platform.artifact_service import ArtifactStorageError, register_local_artifact
from src.platform.connector_service import record_connector_observation
from src.platform.control_service import get_execution_control_state
from src.platform.ledger_service import record_ledger_entry
from src.platform.metric_service import record_metric_point
from src.platform.models import (
    Approval,
    ApprovalStatus,
    Artifact,
    ArtifactSensitivity,
    Automation,
    ConnectorStatus,
    DataQualityStatus,
    LedgerEntryType,
    MetricKind,
    QueueClass,
    Run,
    RunStatus,
    StepRun,
)
from src.platform.run_service import (
    RunCreationResult,
    get_or_create_automation,
    get_or_create_run,
    transition_run,
)
from src.platform.task_runner import (
    PermanentTaskError,
    RetryableTaskError,
    RetryPolicy,
    TaskAttemptContext,
    TaskStepSpec,
    execute_task_step,
)

AUTOMATION_SLUG = "operations-brief"
STEP_NAME = "build-operations-brief"
APPROVAL_ACTION = "review_operations_brief"
EVIDENCE_ARTIFACT_TYPE = "operations_brief_evidence"
BRIEF_ARTIFACT_TYPE = "operations_brief"
CONNECTOR_KEY = "platform-database"

_SOURCE = "operations-brief"
_MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
_CONNECTOR_STATUS_SLO_SECONDS = 3_600
_CONNECTOR_FRESHNESS_SLO_SECONDS = 7 * 86_400
_QUALITY_QUANTUM = Decimal("0.0001")

Clock = Callable[[], datetime]


class OperationsBriefInput(BaseModel):
    """Validated boundary for one manually requested brief."""

    window_days: int
    window_end: date

    @field_validator("window_days")
    @classmethod
    def window_must_be_allowed(cls, value: int) -> int:
        if value not in ALLOWED_WINDOW_DAYS:
            raise ValueError("window_days is not an allowed window")
        return value


class OperationsBriefRunNotRunnableError(ValueError):
    """Raised when a brief run cannot be resumed without an explicit retry."""


@dataclass(frozen=True, slots=True)
class OperationsBriefRunResult:
    """Observable identifiers produced by one brief run or replay."""

    automation_id: int
    run_id: int
    step_run_id: int | None
    evidence_artifact_id: int | None
    brief_artifact_id: int | None
    approval_id: int | None
    run_status: str
    step_status: str | None
    created: bool
    replayed: bool


@dataclass(frozen=True, slots=True)
class OperationsBriefReview:
    """Verified, complete projection of one reviewed brief."""

    approval_id: int
    run_id: int
    evidence_artifact_id: int
    brief_artifact_id: int
    evidence_sha256: str
    brief_sha256: str
    window_days: int
    window_start: str
    window_end_exclusive: str
    markdown: str


def parse_operations_brief_form(
    values: dict[str, str],
    *,
    today: date | None = None,
) -> dict[str, object]:
    """Normalize dashboard fields; the window ends on the current UTC day."""
    if set(values) != {"window_days"}:
        raise ValueError("unsupported operations brief input field")
    try:
        days = int(values["window_days"])
    except ValueError as exc:
        raise ValueError("window_days must be an integer") from exc
    parsed = OperationsBriefInput(
        window_days=days,
        window_end=today or datetime.now(UTC).date(),
    )
    return parsed.model_dump(mode="json")


async def prepare_operations_brief_run(
    session: AsyncSession,
    *,
    idempotency_key: str,
    retry_of_run_id: int | None = None,
    retry_requested_by: str | None = None,
    retry_reason: str | None = None,
    experiment_id: int | None = None,
    trigger: str = "manual",
    input_payload: dict[str, object] | None = None,
) -> RunCreationResult:
    """Persist a validated brief run without reading evidence or touching the broker."""
    input_data = OperationsBriefInput.model_validate(input_payload or {})
    automation = (
        await get_or_create_automation(
            session,
            slug=AUTOMATION_SLUG,
            name="Operations Brief",
            owner="local-operator",
        )
    ).automation
    return await get_or_create_run(
        session,
        automation=automation,
        idempotency_key=idempotency_key,
        trigger="retry" if retry_of_run_id is not None else trigger,
        input_payload=input_data.model_dump(mode="json"),
        retry_of_run_id=retry_of_run_id,
        retry_requested_by=retry_requested_by,
        retry_reason=retry_reason,
        experiment_id=experiment_id,
    )


async def execute_operations_brief_run(
    session: AsyncSession,
    *,
    idempotency_key: str,
    retry_of_run_id: int | None = None,
    retry_requested_by: str | None = None,
    retry_reason: str | None = None,
    experiment_id: int | None = None,
    trigger: str = "manual",
    input_payload: dict[str, object] | None = None,
    storage_root: Path | None = None,
    clock: Clock | None = None,
) -> OperationsBriefRunResult:
    """Build the brief as one IO step and open its review approval."""
    input_data = OperationsBriefInput.model_validate(input_payload or {})
    creation = await prepare_operations_brief_run(
        session,
        idempotency_key=idempotency_key,
        retry_of_run_id=retry_of_run_id,
        retry_requested_by=retry_requested_by,
        retry_reason=retry_reason,
        experiment_id=experiment_id,
        trigger=trigger,
        input_payload=input_data.model_dump(mode="json"),
    )
    run = creation.run
    run_id = _persisted_id(run.id, "run")
    root = Path(settings.storage_root) if storage_root is None else storage_root
    now = clock or _utcnow
    current = RunStatus(run.status)

    if current in {RunStatus.AWAITING_APPROVAL, RunStatus.SUCCEEDED}:
        completed_step = await _required_step(session, run=run)
        evidence_artifact, brief_artifact = await _required_artifacts(session, run=run)
        _load_verified_documents(
            root, evidence_artifact=evidence_artifact, brief_artifact=brief_artifact
        )
        approval = await _required_approval(session, run=run)
        return _result(
            run,
            completed_step,
            evidence_artifact,
            brief_artifact,
            approval_id=_persisted_id(approval.id, "approval"),
            created=False,
            replayed=True,
        )
    if current is RunStatus.CANCELLED:
        return _result(
            run, None, None, None, approval_id=None, created=creation.created, replayed=True
        )
    if current is RunStatus.FAILED:
        raise OperationsBriefRunNotRunnableError(
            "failed brief run requires an explicit successor retry"
        )

    if current is RunStatus.QUEUED:
        control = await get_execution_control_state(session, run_id=run_id)
        if control.blocked:
            await transition_run(
                session,
                run,
                RunStatus.CANCELLED,
                note=f"operations brief start blocked by {control.code}",
            )
            return _result(
                run, None, None, None, approval_id=None, created=creation.created, replayed=False
            )
        await transition_run(session, run, RunStatus.RUNNING, note="operations brief started")

    window = build_window(window_end=input_data.window_end, window_days=input_data.window_days)
    evidence_path = _document_path(root, run_id=run_id, name="evidence.json")
    brief_path = _document_path(root, run_id=run_id, name="brief.md")

    async def build_operation(_context: TaskAttemptContext) -> dict[str, object]:
        return await _build_documents(
            session,
            window=window,
            run_id=run_id,
            collected_at=now(),
            root=root,
            evidence_path=evidence_path,
            brief_path=brief_path,
        )

    task_result = await execute_task_step(
        session,
        run=run,
        spec=TaskStepSpec(
            name=STEP_NAME,
            queue=QueueClass.IO,
            ordinal=1,
            idempotency_key=f"{idempotency_key}:{STEP_NAME}",
            input_payload=input_data.model_dump(mode="json"),
        ),
        policy=RetryPolicy(max_attempts=2, timeout_seconds=60.0),
        operation=build_operation,
    )
    step_run = await session.get(StepRun, task_result.step_run_ids[-1])
    if step_run is None:
        raise OperationsBriefRunNotRunnableError("brief step result was not persisted")

    if task_result.status is RunStatus.CANCELLED:
        return _result(
            run,
            step_run,
            None,
            None,
            approval_id=None,
            created=creation.created,
            replayed=task_result.replayed,
        )
    if task_result.status is RunStatus.FAILED:
        if task_result.error is None:
            raise OperationsBriefRunNotRunnableError("failed brief step is missing its error")
        if RunStatus(run.status) is RunStatus.RUNNING:
            await transition_run(
                session,
                run,
                RunStatus.FAILED,
                error=task_result.error,
                note="operations brief failed",
            )
        return _result(
            run,
            step_run,
            None,
            None,
            approval_id=None,
            created=creation.created,
            replayed=task_result.replayed,
        )

    output = task_result.output_payload or {}
    try:
        async with session.begin_nested():
            automation = await session.get(Automation, run.automation_id)
            if automation is None:
                raise OperationsBriefRunNotRunnableError("brief automation was not persisted")
            evidence_artifact = await _register(
                session,
                run=run,
                step_run=step_run,
                root=root,
                path=evidence_path,
                key=f"{idempotency_key}:evidence",
                artifact_type=EVIDENCE_ARTIFACT_TYPE,
                media_type="application/json",
            )
            brief_artifact = await _register(
                session,
                run=run,
                step_run=step_run,
                root=root,
                path=brief_path,
                key=f"{idempotency_key}:brief",
                artifact_type=BRIEF_ARTIFACT_TYPE,
                media_type="text/markdown",
            )
            evidence, markdown = _load_verified_documents(
                root, evidence_artifact=evidence_artifact, brief_artifact=brief_artifact
            )
            await _record_evidence(
                session,
                automation=automation,
                run=run,
                step_run=step_run,
                idempotency_key=idempotency_key,
                evidence=evidence,
                markdown_bytes=brief_artifact.size_bytes,
                collected_at=_collected_at(evidence),
            )
            approval = (
                await request_approval(
                    session,
                    run=run,
                    idempotency_key=f"{idempotency_key}:brief-review",
                    action=APPROVAL_ACTION,
                    summary="Revisar o brief operacional antes de concluí-lo",
                    input_payload=_approval_input(
                        run=run,
                        step_run=step_run,
                        evidence_artifact=evidence_artifact,
                        brief_artifact=brief_artifact,
                    ),
                    review_payload=_approval_review_payload(
                        evidence=evidence,
                        brief_artifact=brief_artifact,
                        output=output,
                    ),
                )
            ).approval
    except Exception as exc:
        evidence_error = {
            "code": "BRIEF_EVIDENCE_PERSIST_FAILED",
            "exception_type": type(exc).__name__,
            "retryable": True,
            "timed_out": False,
        }
        if RunStatus(run.status) is RunStatus.RUNNING:
            await transition_run(
                session,
                run,
                RunStatus.FAILED,
                error=evidence_error,
                note="operations brief evidence persistence failed",
            )
        return _result(
            run,
            step_run,
            None,
            None,
            approval_id=None,
            created=creation.created,
            replayed=task_result.replayed,
        )
    return _result(
        run,
        step_run,
        evidence_artifact,
        brief_artifact,
        approval_id=_persisted_id(approval.id, "approval"),
        created=creation.created,
        replayed=task_result.replayed,
    )


async def load_operations_brief_review(
    session: AsyncSession,
    *,
    approval: Approval,
    storage_root: Path | None = None,
) -> OperationsBriefReview:
    """Load the complete brief after re-verifying hashes, content and approval digest."""
    if approval.action != APPROVAL_ACTION:
        raise OperationsBriefRunNotRunnableError("approval is not an operations brief review")
    run = await session.get(Run, approval.run_id)
    if run is None:
        raise OperationsBriefRunNotRunnableError("approval run does not exist")
    root = Path(settings.storage_root) if storage_root is None else storage_root
    step_run = await _required_step(session, run=run)
    evidence_artifact, brief_artifact = await _required_artifacts(session, run=run)
    evidence, markdown = _load_verified_documents(
        root, evidence_artifact=evidence_artifact, brief_artifact=brief_artifact
    )
    expected = _approval_input(
        run=run,
        step_run=step_run,
        evidence_artifact=evidence_artifact,
        brief_artifact=brief_artifact,
    )
    if approval.payload_digest != approval_payload_digest(expected):
        raise OperationsBriefRunNotRunnableError("approval does not match the brief artifacts")
    window = evidence["window"]
    return OperationsBriefReview(
        approval_id=_persisted_id(approval.id, "approval"),
        run_id=_persisted_id(run.id, "run"),
        evidence_artifact_id=_persisted_id(evidence_artifact.id, "artifact"),
        brief_artifact_id=_persisted_id(brief_artifact.id, "artifact"),
        evidence_sha256=evidence_artifact.sha256,
        brief_sha256=brief_artifact.sha256,
        window_days=int(window["days"]),
        window_start=str(window["start"]),
        window_end_exclusive=str(window["end_exclusive"]),
        markdown=markdown,
    )


async def finalize_operations_brief_approval(
    session: AsyncSession,
    *,
    approval: Approval,
    storage_root: Path | None = None,
) -> bool:
    """Complete the run after an approved review. Never requests a continuation."""
    if approval.action != APPROVAL_ACTION:
        return False
    if ApprovalStatus(approval.status) is not ApprovalStatus.APPROVED:
        raise OperationsBriefRunNotRunnableError("operations brief approval is not approved")
    review = await load_operations_brief_review(
        session, approval=approval, storage_root=storage_root
    )
    run = await session.get(Run, approval.run_id)
    if run is None:
        raise OperationsBriefRunNotRunnableError("approval run does not exist")
    step_run = await _required_step(session, run=run)
    output_payload = {
        "step_run_id": step_run.id,
        "evidence_artifact_id": review.evidence_artifact_id,
        "brief_artifact_id": review.brief_artifact_id,
        "approval_id": review.approval_id,
    }
    current = RunStatus(run.status)
    if current is RunStatus.SUCCEEDED:
        if (run.output_payload or {}) != output_payload:
            raise OperationsBriefRunNotRunnableError(
                "completed brief run has conflicting approval evidence"
            )
        return False
    if current is not RunStatus.RUNNING:
        raise OperationsBriefRunNotRunnableError(
            "approved brief run must be running before finalization"
        )
    await transition_run(
        session,
        run,
        RunStatus.SUCCEEDED,
        output_payload=output_payload,
        note="operations brief approved; no external effect executed",
    )
    return False


async def _build_documents(
    session: AsyncSession,
    *,
    window: EvidenceWindow,
    run_id: int,
    collected_at: datetime,
    root: Path,
    evidence_path: Path,
    brief_path: Path,
) -> dict[str, object]:
    try:
        async with session.begin_nested():
            evidence = await collect_operations_evidence(
                session,
                window=window,
                collected_at=collected_at,
                exclude_run_id=run_id,
            )
    except SQLAlchemyError as exc:
        raise RetryableTaskError("BRIEF_EVIDENCE_UNAVAILABLE") from exc
    try:
        markdown = render_operations_brief(evidence)
    except OperationsBriefRenderError as exc:
        raise PermanentTaskError("BRIEF_RENDER_FAILED") from exc
    try:
        _write_atomic(evidence_path, _canonical_json(evidence))
        _write_atomic(brief_path, markdown.encode("utf-8"))
    except OSError as exc:
        raise RetryableTaskError("BRIEF_STORAGE_WRITE_FAILED") from exc
    resolved = root.resolve()
    return {
        "evidence_relative_path": evidence_path.relative_to(resolved).as_posix(),
        "brief_relative_path": brief_path.relative_to(resolved).as_posix(),
        "runs_observed": evidence["runs"]["total"],
        "failed_runs": evidence["runs"]["failed_total"],
        "pending_approvals": evidence["approvals"]["pending_count"],
        "active_alerts": evidence["alerts"]["active_count"],
    }


async def _record_evidence(
    session: AsyncSession,
    *,
    automation: Automation,
    run: Run,
    step_run: StepRun,
    idempotency_key: str,
    evidence: dict[str, Any],
    markdown_bytes: int,
    collected_at: datetime,
) -> None:
    runs = evidence["runs"]
    for name, kind, value, unit in (
        ("operations_brief_runs_observed", MetricKind.COUNTER, runs["total"], "runs"),
        ("operations_brief_failed_runs", MetricKind.COUNTER, runs["failed_total"], "runs"),
        ("operations_brief_markdown_bytes", MetricKind.GAUGE, markdown_bytes, "bytes"),
    ):
        await record_metric_point(
            session,
            automation=automation,
            run=run,
            step_run=step_run,
            idempotency_key=f"{idempotency_key}:{name}",
            name=name,
            kind=kind,
            value=value,
            unit=unit,
            source=_SOURCE,
        )
    await record_ledger_entry(
        session,
        automation=automation,
        run=run,
        step_run=step_run,
        idempotency_key=f"{idempotency_key}:external-api-cost",
        entry_type=LedgerEntryType.COST,
        category="external_api",
        amount=0,
        currency="BRL",
        source=_SOURCE,
        confidence=1,
    )
    concluded = int(runs["concluded_total"])
    with_timing = int(runs["concluded_with_timing"])
    if concluded == 0:
        quality_status, quality_score = DataQualityStatus.UNKNOWN, None
    else:
        quality_score = (Decimal(with_timing) / Decimal(concluded)).quantize(_QUALITY_QUANTUM)
        quality_status = (
            DataQualityStatus.PASS if with_timing == concluded else DataQualityStatus.WARNING
        )
    observed_at = collected_at.replace(tzinfo=UTC)
    await record_connector_observation(
        session,
        automation=automation,
        connector_key=CONNECTOR_KEY,
        idempotency_key=f"{idempotency_key}:connector-{CONNECTOR_KEY}",
        status=ConnectorStatus.HEALTHY,
        status_slo_seconds=_CONNECTOR_STATUS_SLO_SECONDS,
        last_success_at=observed_at,
        freshness_slo_seconds=_CONNECTOR_FRESHNESS_SLO_SECONDS,
        quality_status=quality_status,
        quality_score=quality_score,
        observed_at=observed_at,
    )


async def _register(
    session: AsyncSession,
    *,
    run: Run,
    step_run: StepRun,
    root: Path,
    path: Path,
    key: str,
    artifact_type: str,
    media_type: str,
) -> Artifact:
    return (
        await register_local_artifact(
            session,
            run=run,
            step_run=step_run,
            storage_root=root,
            file_path=path,
            idempotency_key=key,
            artifact_type=artifact_type,
            media_type=media_type,
            origin=_SOURCE,
            sensitivity=ArtifactSensitivity.INTERNAL,
            retention_days=resolve_retention_days(artifact_type),
        )
    ).artifact


async def _required_step(session: AsyncSession, *, run: Run) -> StepRun:
    step = await session.scalar(
        select(StepRun)
        .where(
            StepRun.run_id == run.id,
            StepRun.name == STEP_NAME,
            StepRun.status == RunStatus.SUCCEEDED.value,
        )
        .order_by(StepRun.attempt.desc())
    )
    if step is None:
        raise OperationsBriefRunNotRunnableError("brief run is missing its succeeded step")
    return step


async def _required_artifacts(session: AsyncSession, *, run: Run) -> tuple[Artifact, Artifact]:
    found: list[Artifact] = []
    for artifact_type in (EVIDENCE_ARTIFACT_TYPE, BRIEF_ARTIFACT_TYPE):
        artifact = await session.scalar(
            select(Artifact)
            .where(Artifact.run_id == run.id, Artifact.artifact_type == artifact_type)
            .order_by(Artifact.id.desc())
        )
        if artifact is None:
            raise OperationsBriefRunNotRunnableError(f"brief run is missing {artifact_type}")
        found.append(artifact)
    return found[0], found[1]


async def _required_approval(session: AsyncSession, *, run: Run) -> Approval:
    approval = await session.scalar(
        select(Approval)
        .where(Approval.run_id == run.id, Approval.action == APPROVAL_ACTION)
        .order_by(Approval.id.desc())
    )
    if approval is None:
        raise OperationsBriefRunNotRunnableError("brief run is missing its review approval")
    return approval


def _load_verified_documents(
    root: Path,
    *,
    evidence_artifact: Artifact,
    brief_artifact: Artifact,
) -> tuple[dict[str, Any], str]:
    evidence_bytes = _read_verified(root, evidence_artifact)
    brief_bytes = _read_verified(root, brief_artifact)
    try:
        evidence = json.loads(evidence_bytes.decode("utf-8"))
        markdown = brief_bytes.decode("utf-8")
        if not isinstance(evidence, dict):
            raise ValueError("evidence must be an object")
        if render_operations_brief(evidence) != markdown:
            raise ValueError("brief does not match its evidence")
    except (UnicodeError, ValueError) as exc:
        raise OperationsBriefRunNotRunnableError("operations brief documents are invalid") from exc
    return evidence, markdown


def _read_verified(root: Path, artifact: Artifact) -> bytes:
    if artifact.size_bytes > _MAX_DOCUMENT_BYTES:
        raise OperationsBriefRunNotRunnableError("brief document exceeds the review limit")
    try:
        resolved_root = root.resolve(strict=True)
        candidate = (resolved_root / artifact.relative_path).resolve(strict=True)
        candidate.relative_to(resolved_root)
        if not candidate.is_file():
            raise OSError("not a regular file")
        data = candidate.read_bytes()
    except (OSError, ValueError) as exc:
        raise ArtifactStorageError("brief document is unavailable") from exc
    if len(data) != artifact.size_bytes or len(data) > _MAX_DOCUMENT_BYTES:
        raise ArtifactStorageError("brief document size does not match durable metadata")
    if hashlib.sha256(data).hexdigest() != artifact.sha256:
        raise ArtifactStorageError("brief document checksum does not match durable metadata")
    return data


def _approval_input(
    *,
    run: Run,
    step_run: StepRun,
    evidence_artifact: Artifact,
    brief_artifact: Artifact,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "run_id": _persisted_id(run.id, "run"),
        "step_run_id": _persisted_id(step_run.id, "step"),
        "evidence_artifact_id": _persisted_id(evidence_artifact.id, "artifact"),
        "evidence_sha256": evidence_artifact.sha256,
        "brief_artifact_id": _persisted_id(brief_artifact.id, "artifact"),
        "brief_sha256": brief_artifact.sha256,
    }


def _approval_review_payload(
    *,
    evidence: dict[str, Any],
    brief_artifact: Artifact,
    output: dict[str, object],
) -> dict[str, str]:
    window = evidence["window"]
    return {
        "window": f"{window['days']} dia(s) até {window['end_exclusive']} (exclusive, UTC)",
        "runs_observed": str(output.get("runs_observed", 0)),
        "failed_runs": str(output.get("failed_runs", 0)),
        "pending_approvals": str(output.get("pending_approvals", 0)),
        "active_alerts": str(output.get("active_alerts", 0)),
        "artifact": f"Brief #{_persisted_id(brief_artifact.id, 'artifact')}",
        "sha256": brief_artifact.sha256,
    }


def _collected_at(evidence: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(str(evidence["collected_at"]).removesuffix("Z"))


def _document_path(storage_root: Path, *, run_id: int, name: str) -> Path:
    root = storage_root.resolve()
    if root == Path(root.anchor):
        raise ValueError("storage root must not be a filesystem root")
    return root / AUTOMATION_SLUG / f"run-{run_id}" / name


def _canonical_json(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _write_atomic(destination: Path, content: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _persisted_id(value: int | None, kind: str) -> int:
    if not isinstance(value, int):
        raise ValueError(f"{kind} must be persisted")
    return value


def _result(
    run: Run,
    step_run: StepRun | None,
    evidence_artifact: Artifact | None,
    brief_artifact: Artifact | None,
    *,
    approval_id: int | None,
    created: bool,
    replayed: bool,
) -> OperationsBriefRunResult:
    return OperationsBriefRunResult(
        automation_id=_persisted_id(run.automation_id, "automation"),
        run_id=_persisted_id(run.id, "run"),
        step_run_id=None if step_run is None else _persisted_id(step_run.id, "step"),
        evidence_artifact_id=(
            None if evidence_artifact is None else _persisted_id(evidence_artifact.id, "artifact")
        ),
        brief_artifact_id=(
            None if brief_artifact is None else _persisted_id(brief_artifact.id, "artifact")
        ),
        approval_id=approval_id,
        run_status=run.status,
        step_status=None if step_run is None else step_run.status,
        created=created,
        replayed=replayed,
    )
