"""Optional fail-closed A4 alignment gate executed before the A7 review."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.pipeline.caption_alignment import (
    STATUS_PASS,
    CaptionAlignmentError,
    CaptionAlignmentParameters,
    CaptionAlignmentReportView,
    evaluate_caption_alignment,
    load_caption_alignment_report,
)
from src.platform.models import Artifact, QueueClass, Run, RunStatus, StepRun
from src.platform.run_service import transition_run
from src.platform.task_runner import (
    PermanentTaskError,
    RetryPolicy,
    Sleeper,
    TaskAttemptContext,
    TaskStepSpec,
    execute_task_step,
)

CAPTION_ALIGNMENT_GATE_STEP_NAME = "gate-caption-alignment-a4"
CAPTION_ALIGNMENT_BLOCKED_CODE = "CONTENT_CAPTION_ALIGNMENT_BLOCKED"
_GATE_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class CaptionAlignmentGateEvidence:
    """Verified evidence frozen by one successful gate step."""

    step_run_id: int
    report_artifact_id: int
    report_sha256: str
    parameters_digest: str
    audio_artifact_id: int
    caption_artifact_id: int
    speech_coverage_ratio: str
    caption_outside_speech_ratio: str
    onset_offset_seconds: str
    end_offset_seconds: str


@dataclass(frozen=True, slots=True)
class CaptionAlignmentGateResult:
    """Observable outcome of the optional pre-A7 quality gate."""

    run_id: int
    step_run_id: int
    report_artifact_id: int | None
    run_status: str
    step_status: str
    blocked: bool
    replayed: bool


async def execute_caption_alignment_gate_step(
    session: AsyncSession,
    *,
    run: Run,
    idempotency_key: str,
    storage_root: Path,
    parameters: CaptionAlignmentParameters,
    timeout_seconds: float,
    sleep: Sleeper = asyncio.sleep,
) -> CaptionAlignmentGateResult:
    """Measure A4 and allow A7 only when the exact evidence passes."""
    if RunStatus(run.status) is not RunStatus.RUNNING:
        raise ValueError("caption alignment gate requires a running run")
    if timeout_seconds <= 0:
        raise ValueError("caption alignment gate timeout must be positive")
    run_id = _persisted_id(run.id, "run")
    audio = await _required_source_artifact(
        session,
        run_id=run_id,
        artifact_type="narration_audio",
    )
    caption = await _required_source_artifact(
        session,
        run_id=run_id,
        artifact_type="caption_ass",
    )
    diagnostic_view: CaptionAlignmentReportView | None = None

    async def evaluate_operation(_context: TaskAttemptContext) -> dict[str, object]:
        nonlocal diagnostic_view
        try:
            result = await evaluate_caption_alignment(
                session,
                run_id=run_id,
                storage_root=storage_root,
                parameters=parameters,
            )
        except CaptionAlignmentError as exc:
            raise PermanentTaskError(exc.code) from exc
        if (
            result.audio_artifact_id != audio.id
            or result.caption_artifact_id != caption.id
        ):
            raise PermanentTaskError("CAPTION_ALIGNMENT_SOURCE_CHANGED")
        diagnostic_view = await load_caption_alignment_report(
            session,
            run_id=run_id,
            storage_root=storage_root,
            report_artifact_id=result.report_artifact_id,
        )
        if diagnostic_view is None:
            raise PermanentTaskError("CAPTION_ALIGNMENT_REPORT_UNVERIFIED")
        if result.measurement.status != STATUS_PASS:
            raise PermanentTaskError(CAPTION_ALIGNMENT_BLOCKED_CODE)
        return _gate_output(diagnostic_view)

    task_result = await execute_task_step(
        session,
        run=run,
        spec=TaskStepSpec(
            name=CAPTION_ALIGNMENT_GATE_STEP_NAME,
            queue=QueueClass.CPU,
            ordinal=7,
            idempotency_key=f"{idempotency_key}:{CAPTION_ALIGNMENT_GATE_STEP_NAME}",
            input_payload={
                "schema_version": _GATE_SCHEMA_VERSION,
                "audio_artifact_id": _persisted_id(audio.id, "audio artifact"),
                "audio_sha256": audio.sha256,
                "caption_artifact_id": _persisted_id(caption.id, "caption artifact"),
                "caption_sha256": caption.sha256,
                "parameters": parameters.as_payload(),
            },
        ),
        policy=RetryPolicy(max_attempts=1, timeout_seconds=timeout_seconds),
        operation=evaluate_operation,
        sleep=sleep,
    )
    step = await session.get(StepRun, task_result.step_run_ids[-1])
    if step is None:
        raise ValueError("caption alignment gate step was not persisted")
    if task_result.status is RunStatus.CANCELLED:
        return _result(
            run=run,
            step=step,
            report_artifact_id=None,
            blocked=False,
            replayed=task_result.replayed,
        )
    if task_result.status is RunStatus.FAILED:
        if task_result.error is None:
            raise ValueError("failed caption alignment gate is missing its error")
        error = dict(task_result.error)
        blocked = error.get("code") == CAPTION_ALIGNMENT_BLOCKED_CODE
        if blocked and diagnostic_view is not None:
            error.update(
                {
                    "report_artifact_id": diagnostic_view.report_artifact_id,
                    "parameters_digest": diagnostic_view.parameters_digest,
                    "reasons": list(diagnostic_view.reasons),
                }
            )
        if RunStatus(run.status) is RunStatus.RUNNING:
            await transition_run(
                session,
                run,
                RunStatus.FAILED,
                error=error,
                note=(
                    "Content Engine caption alignment gate blocked A7; nothing published"
                    if blocked
                    else "Content Engine caption alignment gate failed closed"
                ),
            )
        return _result(
            run=run,
            step=step,
            report_artifact_id=(
                None if diagnostic_view is None else diagnostic_view.report_artifact_id
            ),
            blocked=blocked,
            replayed=task_result.replayed,
        )

    evidence = await load_caption_alignment_gate_evidence(
        session,
        run=run,
        storage_root=storage_root,
    )
    if evidence is None:
        raise ValueError("successful caption alignment gate has no verified evidence")
    return _result(
        run=run,
        step=step,
        report_artifact_id=evidence.report_artifact_id,
        blocked=False,
        replayed=task_result.replayed,
    )


async def load_caption_alignment_gate_evidence(
    session: AsyncSession,
    *,
    run: Run,
    storage_root: Path,
) -> CaptionAlignmentGateEvidence | None:
    """Verify the persisted gate step and its exact diagnostic report."""
    run_id = _persisted_id(run.id, "run")
    step = await session.scalar(
        select(StepRun)
        .where(
            StepRun.run_id == run_id,
            StepRun.name == CAPTION_ALIGNMENT_GATE_STEP_NAME,
        )
        .order_by(StepRun.id.desc())
        .limit(1)
    )
    if step is None:
        return None
    if RunStatus(step.status) is not RunStatus.SUCCEEDED:
        raise ValueError("caption alignment gate did not pass")
    output = step.output_payload
    if not isinstance(output, dict) or output.get("schema_version") != _GATE_SCHEMA_VERSION:
        raise ValueError("caption alignment gate output is invalid")
    report_id = _required_int(output, "report_artifact_id")
    view = await load_caption_alignment_report(
        session,
        run_id=run_id,
        storage_root=storage_root,
        report_artifact_id=report_id,
    )
    if view is None or view.status != STATUS_PASS:
        raise ValueError("caption alignment gate report is unavailable or did not pass")
    expected = _gate_output(view)
    if output != expected:
        raise ValueError("caption alignment gate output conflicts with its report")
    return CaptionAlignmentGateEvidence(
        step_run_id=_persisted_id(step.id, "step"),
        report_artifact_id=view.report_artifact_id,
        report_sha256=view.report_sha256,
        parameters_digest=view.parameters_digest,
        audio_artifact_id=view.audio_artifact_id,
        caption_artifact_id=view.caption_artifact_id,
        speech_coverage_ratio=str(view.speech_coverage_ratio),
        caption_outside_speech_ratio=str(view.caption_outside_speech_ratio),
        onset_offset_seconds=str(view.onset_offset_seconds),
        end_offset_seconds=str(view.end_offset_seconds),
    )


def _gate_output(view: CaptionAlignmentReportView) -> dict[str, object]:
    return {
        "schema_version": _GATE_SCHEMA_VERSION,
        "report_artifact_id": view.report_artifact_id,
        "report_sha256": view.report_sha256,
        "parameters_digest": view.parameters_digest,
        "status": view.status,
        "reasons": list(view.reasons),
        "audio_artifact_id": view.audio_artifact_id,
        "caption_artifact_id": view.caption_artifact_id,
        "speech_coverage_ratio": str(view.speech_coverage_ratio),
        "caption_outside_speech_ratio": str(view.caption_outside_speech_ratio),
        "onset_offset_seconds": str(view.onset_offset_seconds),
        "end_offset_seconds": str(view.end_offset_seconds),
    }


async def _required_source_artifact(
    session: AsyncSession,
    *,
    run_id: int,
    artifact_type: str,
) -> Artifact:
    artifact = await session.scalar(
        select(Artifact)
        .where(Artifact.run_id == run_id, Artifact.artifact_type == artifact_type)
        .order_by(Artifact.id.desc())
        .limit(1)
    )
    if artifact is None:
        raise ValueError(f"caption alignment gate requires {artifact_type}")
    return artifact


def _required_int(payload: dict[str, object], field: str) -> int:
    value = payload.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"caption alignment gate field {field} is invalid")
    return value


def _persisted_id(value: int | None, kind: str) -> int:
    if not isinstance(value, int):
        raise ValueError(f"{kind} must be persisted")
    return value


def _result(
    *,
    run: Run,
    step: StepRun,
    report_artifact_id: int | None,
    blocked: bool,
    replayed: bool,
) -> CaptionAlignmentGateResult:
    return CaptionAlignmentGateResult(
        run_id=_persisted_id(run.id, "run"),
        step_run_id=_persisted_id(step.id, "step"),
        report_artifact_id=report_artifact_id,
        run_status=run.status,
        step_status=step.status,
        blocked=blocked,
        replayed=replayed,
    )
