"""Content Engine A7: human review gate for the finished local video.

A7 is the last local gate before any future publication path exists. It never
uploads, sends or spends anything: approving it only concludes the local run and
records an immutable decision bound to the exact SHA-256 of the final MP4 and of
the A6 originality report. Rejecting it cancels the run through the shared
approval contract.

The gate is disabled by default. When `CONTENT_FINAL_REVIEW_ENABLED` is false,
A6 keeps concluding the run exactly as before.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.platform.approval_service import approval_payload_digest, request_approval
from src.platform.models import (
    Approval,
    ApprovalStatus,
    Artifact,
    LedgerEntry,
    MetricPoint,
    Run,
    RunStatus,
    StepRun,
)
from src.platform.run_service import transition_run

FINAL_REVIEW_APPROVAL_ACTION = "review_content_final_video_a7"
FINAL_REVIEW_STEP_NAME = "check-originality-a6"
_SCRIPT_APPROVAL_ACTION = "review_content_script_a1"
_MAX_REPORT_BYTES = 5 * 1024 * 1024
_MAX_REVIEW_COMPARISONS = 100
_APPROVAL_SCHEMA_VERSION = 1


class FinalReviewGateError(ValueError):
    """Raised when the A7 gate cannot be requested or finalized safely."""


@dataclass(frozen=True, slots=True)
class FinalReviewRequestResult:
    """Observable identifiers produced when A7 opens the human gate."""

    run_id: int
    approval_id: int
    run_status: str
    created: bool


@dataclass(frozen=True, slots=True)
class FinalVideoComparison:
    """One redacted historical comparison shown on the A7 review page."""

    reference_run_id: int
    score: str


@dataclass(frozen=True, slots=True)
class FinalVideoReview:
    """Complete, checksum-verified A7 projection for one human decision.

    `video_path` is server-side only: it lets the dashboard stream the exact
    verified file and must never be rendered in a template.
    """

    approval_id: int
    run_id: int
    video_artifact_id: int
    video_sha256: str
    video_size_bytes: int
    width: int
    height: int
    duration_seconds: str
    video_codec: str
    audio_codec: str
    video_format: str
    report_artifact_id: int
    report_sha256: str
    algorithm: str
    threshold: str
    max_similarity: str
    mean_similarity: str
    reference_count: int
    comparisons: tuple[FinalVideoComparison, ...]
    video_path: Path


@dataclass(frozen=True, slots=True)
class _VerifiedEvidence:
    video: Artifact
    report: Artifact
    video_path: Path
    originality_step_run_id: int
    algorithm: str
    max_similarity: str
    mean_similarity: str
    threshold: str
    reference_count: int
    comparisons: tuple[FinalVideoComparison, ...]
    duration_seconds: str
    width: int
    height: int
    video_codec: str
    audio_codec: str
    video_format: str


async def request_final_video_approval(
    session: AsyncSession,
    *,
    run: Run,
    script_approval: Approval,
    approval_input_payload: dict[str, object],
    final_video_artifact: Artifact,
    originality_report_artifact: Artifact,
    idempotency_key: str,
    storage_root: Path,
) -> FinalReviewRequestResult:
    """Open the A7 gate for a verified, unblocked local video without publishing."""
    _validate_script_approval(
        run=run,
        script_approval=script_approval,
        approval_input_payload=approval_input_payload,
    )
    if RunStatus(run.status) is not RunStatus.RUNNING:
        raise FinalReviewGateError("A7 gate requires a running run")
    evidence = await _verify_evidence(
        session,
        run=run,
        final_video_artifact=final_video_artifact,
        originality_report_artifact=originality_report_artifact,
        storage_root=storage_root,
    )
    creation = await request_approval(
        session,
        run=run,
        idempotency_key=f"{idempotency_key}:final-video-review",
        action=FINAL_REVIEW_APPROVAL_ACTION,
        summary="Revisar o vídeo final local antes de qualquer publicação futura",
        input_payload=_approval_input(run=run, evidence=evidence),
        review_payload=_review_payload(evidence),
    )
    return FinalReviewRequestResult(
        run_id=_persisted_id(run.id, "run"),
        approval_id=_persisted_id(creation.approval.id, "approval"),
        run_status=run.status,
        created=creation.created,
    )


async def load_final_video_review(
    session: AsyncSession,
    *,
    approval: Approval,
    storage_root: Path,
) -> FinalVideoReview:
    """Load the complete, checksum-verified A7 evidence for one human decision."""
    if approval.action != FINAL_REVIEW_APPROVAL_ACTION:
        raise FinalReviewGateError("approval is not an A7 final video review")
    run = await session.get(Run, approval.run_id)
    if run is None:
        raise FinalReviewGateError("approval run does not exist")
    video = await _required_artifact(session, run=run, artifact_type="final_video")
    report = await _required_artifact(session, run=run, artifact_type="originality_report")
    evidence = await _verify_evidence(
        session,
        run=run,
        final_video_artifact=video,
        originality_report_artifact=report,
        storage_root=storage_root,
    )
    if approval.payload_digest != approval_payload_digest(
        _approval_input(run=run, evidence=evidence)
    ):
        raise FinalReviewGateError("approval does not match the reviewed final video")
    return FinalVideoReview(
        approval_id=_persisted_id(approval.id, "approval"),
        run_id=_persisted_id(run.id, "run"),
        video_artifact_id=_persisted_id(evidence.video.id, "artifact"),
        video_sha256=evidence.video.sha256,
        video_size_bytes=evidence.video.size_bytes,
        width=evidence.width,
        height=evidence.height,
        duration_seconds=evidence.duration_seconds,
        video_codec=evidence.video_codec,
        audio_codec=evidence.audio_codec,
        video_format=evidence.video_format,
        report_artifact_id=_persisted_id(evidence.report.id, "artifact"),
        report_sha256=evidence.report.sha256,
        algorithm=evidence.algorithm,
        threshold=evidence.threshold,
        max_similarity=evidence.max_similarity,
        mean_similarity=evidence.mean_similarity,
        reference_count=evidence.reference_count,
        comparisons=evidence.comparisons,
        video_path=evidence.video_path,
    )


async def finalize_final_video_approval(
    session: AsyncSession,
    *,
    approval: Approval,
    storage_root: Path,
) -> bool:
    """Conclude an approved A7 run locally; never continue into an external effect."""
    if approval.action != FINAL_REVIEW_APPROVAL_ACTION:
        return False
    if ApprovalStatus(approval.status) is not ApprovalStatus.APPROVED:
        raise FinalReviewGateError("A7 final review approval is not approved")
    run = await session.get(Run, approval.run_id)
    if run is None:
        raise FinalReviewGateError("approval run does not exist")
    video = await _required_artifact(session, run=run, artifact_type="final_video")
    report = await _required_artifact(session, run=run, artifact_type="originality_report")
    evidence = await _verify_evidence(
        session,
        run=run,
        final_video_artifact=video,
        originality_report_artifact=report,
        storage_root=storage_root,
    )
    if approval.payload_digest != approval_payload_digest(
        _approval_input(run=run, evidence=evidence)
    ):
        raise FinalReviewGateError("approval does not match the reviewed final video")
    output_payload = await _output_payload(
        session,
        approval=approval,
        evidence=evidence,
    )
    current = RunStatus(run.status)
    if current is RunStatus.SUCCEEDED:
        if (run.output_payload or {}) != output_payload:
            raise FinalReviewGateError("completed A7 run has conflicting review evidence")
        return False
    if current is not RunStatus.RUNNING:
        raise FinalReviewGateError("approved A7 run must be running before finalization")
    await transition_run(
        session,
        run,
        RunStatus.SUCCEEDED,
        output_payload=output_payload,
        note="Content Engine A7 human review approved the local video; nothing published",
    )
    return False


def _validate_script_approval(
    *,
    run: Run,
    script_approval: Approval,
    approval_input_payload: dict[str, object],
) -> None:
    if script_approval.action != _SCRIPT_APPROVAL_ACTION:
        raise FinalReviewGateError("A7 requires the exact A1 script approval")
    if script_approval.run_id != run.id:
        raise FinalReviewGateError("A1 approval belongs to a different run")
    if ApprovalStatus(script_approval.status) is not ApprovalStatus.APPROVED:
        raise FinalReviewGateError("A1 script approval is not approved")
    if script_approval.payload_digest != approval_payload_digest(approval_input_payload):
        raise FinalReviewGateError("A1 approval payload does not match the run evidence")


async def _verify_evidence(
    session: AsyncSession,
    *,
    run: Run,
    final_video_artifact: Artifact,
    originality_report_artifact: Artifact,
    storage_root: Path,
) -> _VerifiedEvidence:
    for artifact in (final_video_artifact, originality_report_artifact):
        if artifact.run_id != run.id:
            raise FinalReviewGateError("A7 evidence belongs to a different run")
    video_path, _ = _verify_artifact_file(storage_root, final_video_artifact)
    _, encoded_report = _verify_artifact_file(
        storage_root,
        originality_report_artifact,
        max_bytes=_MAX_REPORT_BYTES,
    )
    try:
        report = json.loads(encoded_report.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise FinalReviewGateError("A6 report is not readable") from exc
    if not isinstance(report, dict) or report.get("schema_version") != 1:
        raise FinalReviewGateError("A6 report schema is not supported")
    if (
        report.get("final_video_artifact_id") != final_video_artifact.id
        or report.get("final_video_sha256") != final_video_artifact.sha256
    ):
        raise FinalReviewGateError("A6 report does not describe the reviewed video")
    if report.get("blocked") is not False:
        raise FinalReviewGateError("A7 cannot review a video blocked by A6")
    assembly = await _required_step_output(session, run=run, name="assemble-video-a5")
    originality_step_id = await _required_step_id(
        session,
        run=run,
        name=FINAL_REVIEW_STEP_NAME,
    )
    return _VerifiedEvidence(
        video=final_video_artifact,
        report=originality_report_artifact,
        video_path=video_path,
        originality_step_run_id=originality_step_id,
        algorithm=_report_text(report, "algorithm"),
        max_similarity=_report_decimal(report, "max_similarity"),
        mean_similarity=_report_decimal(report, "mean_similarity"),
        threshold=_report_decimal(report, "threshold"),
        reference_count=_report_int(report, "reference_count"),
        comparisons=_report_comparisons(report),
        duration_seconds=_step_decimal(assembly, "duration_seconds"),
        width=_step_int(assembly, "width"),
        height=_step_int(assembly, "height"),
        video_codec=_step_text(assembly, "video_codec"),
        audio_codec=_step_text(assembly, "audio_codec"),
        video_format=_step_text(assembly, "format"),
    )


def _approval_input(*, run: Run, evidence: _VerifiedEvidence) -> dict[str, object]:
    return {
        "schema_version": _APPROVAL_SCHEMA_VERSION,
        "run_id": _persisted_id(run.id, "run"),
        "final_video_artifact_id": _persisted_id(evidence.video.id, "artifact"),
        "final_video_sha256": evidence.video.sha256,
        "originality_report_artifact_id": _persisted_id(evidence.report.id, "artifact"),
        "originality_report_sha256": evidence.report.sha256,
        "originality_max_similarity": evidence.max_similarity,
        "originality_threshold": evidence.threshold,
    }


def _review_payload(evidence: _VerifiedEvidence) -> dict[str, str]:
    return {
        "video": (
            f"Vídeo final #{evidence.video.id} · {evidence.width}x{evidence.height} · "
            f"{evidence.duration_seconds} s · {evidence.video.size_bytes} bytes"
        ),
        "format": "Short" if evidence.video_format == "short" else "Longo",
        "codecs": (
            f"{evidence.video_codec.upper()} + {evidence.audio_codec.upper()} · "
            "legendas queimadas"
        ),
        "video_sha256": evidence.video.sha256,
        "originality": (
            f"máximo {evidence.max_similarity} · média {evidence.mean_similarity} · "
            f"limite {evidence.threshold} · {evidence.reference_count} referências"
        ),
        "report_sha256": evidence.report.sha256,
        "publication": (
            "Nenhum upload, envio ou gasto existe. Aprovar apenas conclui a run local "
            "e registra a decisão ligada aos hashes acima."
        ),
    }


async def _output_payload(
    session: AsyncSession,
    *,
    approval: Approval,
    evidence: _VerifiedEvidence,
) -> dict[str, object]:
    metric_ids = list(
        (
            await session.scalars(
                select(MetricPoint.id)
                .where(MetricPoint.run_id == approval.run_id)
                .order_by(MetricPoint.id)
            )
        ).all()
    )
    ledger_ids = list(
        (
            await session.scalars(
                select(LedgerEntry.id)
                .where(LedgerEntry.run_id == approval.run_id)
                .order_by(LedgerEntry.id)
            )
        ).all()
    )
    return {
        "final_video_artifact_id": _persisted_id(evidence.video.id, "artifact"),
        "final_video_sha256": evidence.video.sha256,
        "originality_report_artifact_id": _persisted_id(evidence.report.id, "artifact"),
        "originality_report_sha256": evidence.report.sha256,
        "originality_step_run_id": evidence.originality_step_run_id,
        "originality_max_similarity": evidence.max_similarity,
        "originality_threshold": evidence.threshold,
        "originality_reference_count": evidence.reference_count,
        "final_review_approval_id": _persisted_id(approval.id, "approval"),
        "metric_point_ids": metric_ids,
        "ledger_entry_ids": ledger_ids,
        "published": False,
    }


async def _required_artifact(
    session: AsyncSession,
    *,
    run: Run,
    artifact_type: str,
) -> Artifact:
    artifact = await session.scalar(
        select(Artifact)
        .where(Artifact.run_id == run.id, Artifact.artifact_type == artifact_type)
        .order_by(Artifact.id.desc())
        .limit(1)
    )
    if artifact is None:
        raise FinalReviewGateError(f"A7 run is missing its {artifact_type} artifact")
    return artifact


async def _required_step_output(
    session: AsyncSession,
    *,
    run: Run,
    name: str,
) -> dict[str, object]:
    step = await session.scalar(
        select(StepRun)
        .where(StepRun.run_id == run.id, StepRun.name == name)
        .order_by(StepRun.id.desc())
        .limit(1)
    )
    if step is None or not isinstance(step.output_payload, dict):
        raise FinalReviewGateError(f"A7 run is missing verified output for {name}")
    return step.output_payload


async def _required_step_id(session: AsyncSession, *, run: Run, name: str) -> int:
    step_id = await session.scalar(
        select(StepRun.id)
        .where(StepRun.run_id == run.id, StepRun.name == name)
        .order_by(StepRun.id.desc())
        .limit(1)
    )
    if not isinstance(step_id, int):
        raise FinalReviewGateError(f"A7 run is missing the persisted step {name}")
    return step_id


def _verify_artifact_file(
    storage_root: Path,
    artifact: Artifact,
    *,
    max_bytes: int | None = None,
) -> tuple[Path, bytes]:
    try:
        root = storage_root.resolve(strict=True)
        candidate = (root / artifact.relative_path).resolve(strict=True)
        candidate.relative_to(root)
        if not candidate.is_file():
            raise OSError("not a regular file")
        size = candidate.stat().st_size
        if size != artifact.size_bytes:
            raise FinalReviewGateError("A7 evidence size does not match durable metadata")
        if max_bytes is not None and size > max_bytes:
            raise FinalReviewGateError("A7 evidence exceeds the review limit")
        digest = hashlib.sha256()
        encoded = b""
        with candidate.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                if max_bytes is not None:
                    encoded += chunk
    except FinalReviewGateError:
        raise
    except (OSError, ValueError) as exc:
        raise FinalReviewGateError("A7 evidence is unavailable") from exc
    if digest.hexdigest() != artifact.sha256:
        raise FinalReviewGateError("A7 evidence checksum does not match durable metadata")
    return candidate, encoded


def _report_text(report: dict[str, object], field: str) -> str:
    value = report.get(field)
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise FinalReviewGateError(f"A6 report field {field} is invalid")
    return value.strip()


def _report_comparisons(report: dict[str, object]) -> tuple[FinalVideoComparison, ...]:
    raw = report.get("comparisons")
    if not isinstance(raw, list) or len(raw) > _MAX_REVIEW_COMPARISONS:
        raise FinalReviewGateError("A6 report comparisons are invalid")
    comparisons: list[FinalVideoComparison] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise FinalReviewGateError("A6 report comparisons are invalid")
        reference_run_id = entry.get("reference_run_id")
        if not isinstance(reference_run_id, int) or isinstance(reference_run_id, bool):
            raise FinalReviewGateError("A6 report comparisons are invalid")
        comparisons.append(
            FinalVideoComparison(
                reference_run_id=reference_run_id,
                score=_report_decimal(entry, "score"),
            )
        )
    return tuple(comparisons)


def _report_decimal(report: dict[str, object], field: str) -> str:
    value = report.get(field)
    if not isinstance(value, str):
        raise FinalReviewGateError(f"A6 report field {field} is invalid")
    try:
        return _human_decimal(Decimal(value))
    except (InvalidOperation, ValueError) as exc:
        raise FinalReviewGateError(f"A6 report field {field} is invalid") from exc


def _report_int(report: dict[str, object], field: str) -> int:
    value = report.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise FinalReviewGateError(f"A6 report field {field} is invalid")
    return value


def _step_text(output: dict[str, object], field: str) -> str:
    value = output.get(field)
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise FinalReviewGateError(f"A5 output field {field} is invalid")
    return value.strip()


def _step_int(output: dict[str, object], field: str) -> int:
    value = output.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise FinalReviewGateError(f"A5 output field {field} is invalid")
    return value


def _step_decimal(output: dict[str, object], field: str) -> str:
    value = output.get(field)
    if not isinstance(value, str):
        raise FinalReviewGateError(f"A5 output field {field} is invalid")
    try:
        return _human_decimal(Decimal(value))
    except (InvalidOperation, ValueError) as exc:
        raise FinalReviewGateError(f"A5 output field {field} is invalid") from exc


def _human_decimal(value: Decimal) -> str:
    """Render an exact decimal without trailing zeros, exponents or precision loss."""
    normalized = value.normalize()
    exponent = normalized.as_tuple().exponent
    if isinstance(exponent, int) and exponent > 0:
        normalized = normalized.quantize(Decimal(1))
    return format(normalized, "f")


def _persisted_id(value: int | None, kind: str) -> int:
    if not isinstance(value, int):
        raise ValueError(f"{kind} must be persisted")
    return value
