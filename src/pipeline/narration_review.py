"""Content Engine A2 human review gate before audiovisual continuation.

The gate binds a human decision to the exact narration WAV and exposes the
number and dimensions of visual assets required by A3. It never publishes,
uploads, copies, or modifies the reviewed audio.
"""

from __future__ import annotations

import hashlib
import json
import math
import wave
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.pipeline.tts import TTS_STEP_NAME
from src.platform.approval_service import approval_payload_digest, request_approval
from src.platform.models import (
    Approval,
    ApprovalStatus,
    Artifact,
    Run,
    RunStatus,
    StepRun,
)

NARRATION_REVIEW_APPROVAL_ACTION = "review_content_narration_a2"
_SCRIPT_APPROVAL_ACTION = "review_content_script_a1"
_MAX_AUDIO_BYTES = 1024 * 1024 * 1024
_MAX_SCRIPT_BYTES = 5 * 1024 * 1024
_MAX_VISUALS = 60
_SECONDS_PER_VISUAL = Decimal("6")


class NarrationReviewGateError(ValueError):
    """Raised when A2 review evidence cannot be trusted."""


@dataclass(frozen=True, slots=True)
class NarrationReviewRequestResult:
    """Observable result of opening or replaying the A2 review gate."""

    run_id: int
    approval_id: int
    run_status: str
    created: bool


@dataclass(frozen=True, slots=True)
class NarrationReview:
    """Checksum-verified projection used by the local review page.

    ``audio_path`` is server-side only and must not be rendered into HTML.
    """

    approval_id: int
    run_id: int
    audio_artifact_id: int
    audio_sha256: str
    audio_size_bytes: int
    duration_seconds: str
    sample_rate_hz: int
    voice_id: str
    backend: str
    model_id: str
    license_id: str
    format: str
    required_visual_count: int
    visual_width: int
    visual_height: int
    audio_path: Path


@dataclass(frozen=True, slots=True)
class _VerifiedNarrationEvidence:
    audio: Artifact
    audio_path: Path
    duration_seconds: str
    sample_rate_hz: int
    frame_count: int
    voice_id: str
    backend: str
    model_id: str
    license_id: str
    format: str
    required_visual_count: int
    visual_width: int
    visual_height: int


async def request_narration_approval(
    session: AsyncSession,
    *,
    run: Run,
    script_approval: Approval,
    script_approval_input_payload: dict[str, object],
    script_artifact: Artifact,
    audio_artifact: Artifact,
    idempotency_key: str,
    storage_root: Path,
) -> NarrationReviewRequestResult:
    """Open A2 review after verifying the approved script and narration WAV."""
    _validate_script_approval(
        run=run,
        approval=script_approval,
        input_payload=script_approval_input_payload,
    )
    if RunStatus(run.status) is not RunStatus.RUNNING:
        raise NarrationReviewGateError("A2 review requires a running run")
    evidence = await _verify_evidence(
        session,
        run=run,
        script_artifact=script_artifact,
        audio_artifact=audio_artifact,
        storage_root=storage_root,
    )
    creation = await request_approval(
        session,
        run=run,
        idempotency_key=f"{idempotency_key}:narration-review",
        action=NARRATION_REVIEW_APPROVAL_ACTION,
        summary="Escutar e aprovar a narração A2 antes de preparar visuais e legendas",
        input_payload=_approval_input(run=run, evidence=evidence),
        review_payload=_review_payload(evidence),
    )
    return NarrationReviewRequestResult(
        run_id=_persisted_id(run.id, "run"),
        approval_id=_persisted_id(creation.approval.id, "approval"),
        run_status=run.status,
        created=creation.created,
    )


async def load_narration_review(
    session: AsyncSession,
    *,
    approval: Approval,
    storage_root: Path,
) -> NarrationReview:
    """Load the exact A2 audio and visual preparation requirements."""
    if approval.action != NARRATION_REVIEW_APPROVAL_ACTION:
        raise NarrationReviewGateError("approval is not an A2 narration review")
    run = await session.get(Run, approval.run_id)
    if run is None:
        raise NarrationReviewGateError("approval run does not exist")
    script_artifact = await _required_artifact(
        session,
        run=run,
        artifact_type="script_bundle",
    )
    audio_artifact = await _required_artifact(
        session,
        run=run,
        artifact_type="narration_audio",
    )
    evidence = await _verify_evidence(
        session,
        run=run,
        script_artifact=script_artifact,
        audio_artifact=audio_artifact,
        storage_root=storage_root,
    )
    if approval.payload_digest != approval_payload_digest(
        _approval_input(run=run, evidence=evidence)
    ):
        raise NarrationReviewGateError("approval does not match the reviewed narration")
    return NarrationReview(
        approval_id=_persisted_id(approval.id, "approval"),
        run_id=_persisted_id(run.id, "run"),
        audio_artifact_id=_persisted_id(evidence.audio.id, "artifact"),
        audio_sha256=evidence.audio.sha256,
        audio_size_bytes=evidence.audio.size_bytes,
        duration_seconds=evidence.duration_seconds,
        sample_rate_hz=evidence.sample_rate_hz,
        voice_id=evidence.voice_id,
        backend=evidence.backend,
        model_id=evidence.model_id,
        license_id=evidence.license_id,
        format=evidence.format,
        required_visual_count=evidence.required_visual_count,
        visual_width=evidence.visual_width,
        visual_height=evidence.visual_height,
        audio_path=evidence.audio_path,
    )


async def finalize_narration_approval(
    session: AsyncSession,
    *,
    approval: Approval,
    storage_root: Path,
) -> bool:
    """Verify an approved A2 decision and request durable continuation."""
    if approval.action != NARRATION_REVIEW_APPROVAL_ACTION:
        return False
    if ApprovalStatus(approval.status) is not ApprovalStatus.APPROVED:
        raise NarrationReviewGateError("A2 narration review is not approved")
    await load_narration_review(
        session,
        approval=approval,
        storage_root=storage_root,
    )
    run = await session.get(Run, approval.run_id)
    if run is None:
        raise NarrationReviewGateError("approval run does not exist")
    current = RunStatus(run.status)
    if current is RunStatus.RUNNING:
        return True
    if current is RunStatus.SUCCEEDED:
        return False
    raise NarrationReviewGateError(
        "approved A2 narration run must be running before continuation"
    )


def _validate_script_approval(
    *,
    run: Run,
    approval: Approval,
    input_payload: dict[str, object],
) -> None:
    if approval.action != _SCRIPT_APPROVAL_ACTION or approval.run_id != run.id:
        raise NarrationReviewGateError("A2 review requires the exact A1 approval")
    if ApprovalStatus(approval.status) is not ApprovalStatus.APPROVED:
        raise NarrationReviewGateError("A1 script approval is not approved")
    if approval.payload_digest != approval_payload_digest(input_payload):
        raise NarrationReviewGateError("A1 approval payload does not match the run")


async def _verify_evidence(
    session: AsyncSession,
    *,
    run: Run,
    script_artifact: Artifact,
    audio_artifact: Artifact,
    storage_root: Path,
) -> _VerifiedNarrationEvidence:
    if script_artifact.run_id != run.id or audio_artifact.run_id != run.id:
        raise NarrationReviewGateError("A2 review evidence belongs to another run")
    if script_artifact.artifact_type != "script_bundle":
        raise NarrationReviewGateError("A2 review is missing the script bundle")
    if (
        audio_artifact.artifact_type != "narration_audio"
        or audio_artifact.media_type != "audio/wav"
        or not isinstance(audio_artifact.step_run_id, int)
    ):
        raise NarrationReviewGateError("A2 review requires a persisted narration WAV")

    _, script_bytes = _verify_artifact_file(
        storage_root,
        script_artifact,
        max_bytes=_MAX_SCRIPT_BYTES,
        read_content=True,
    )
    try:
        script_payload = json.loads(script_bytes.decode("utf-8"))
        if not isinstance(script_payload, dict):
            raise NarrationReviewGateError("A1 script bundle is invalid")
        result = script_payload.get("result")
        full_script = result.get("full_script") if isinstance(result, dict) else None
        format_value = script_payload.get("format")
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise NarrationReviewGateError("A1 script bundle is unreadable") from exc
    if (
        script_payload.get("schema_version") != 1
        or not isinstance(full_script, str)
        or not full_script.strip()
        or format_value not in {"short", "long"}
    ):
        raise NarrationReviewGateError("A1 script bundle is invalid")
    input_format = (run.input_payload or {}).get("format")
    if input_format != format_value:
        raise NarrationReviewGateError("A1 format does not match the run")

    audio_path, _ = _verify_artifact_file(
        storage_root,
        audio_artifact,
        max_bytes=_MAX_AUDIO_BYTES,
        read_content=False,
    )
    try:
        with wave.open(str(audio_path), "rb") as audio:
            channels = audio.getnchannels()
            sample_width = audio.getsampwidth()
            sample_rate = audio.getframerate()
            frame_count = audio.getnframes()
    except (OSError, EOFError, wave.Error) as exc:
        raise NarrationReviewGateError("A2 narration WAV is unreadable") from exc
    if channels != 1 or sample_width != 2 or sample_rate < 8_000 or frame_count < 1:
        raise NarrationReviewGateError("A2 narration WAV format is unsupported")
    duration = (Decimal(frame_count) / Decimal(sample_rate)).quantize(
        Decimal("0.0000000001")
    )
    if duration <= 0 or duration > Decimal(6 * 60 * 60):
        raise NarrationReviewGateError("A2 narration duration is invalid")

    step = await session.get(StepRun, audio_artifact.step_run_id)
    if (
        step is None
        or step.run_id != run.id
        or step.name != TTS_STEP_NAME
        or step.status != RunStatus.SUCCEEDED.value
    ):
        raise NarrationReviewGateError("A2 narration step evidence is invalid")
    output = step.output_payload or {}
    output_duration = _decimal_output(output, "duration_seconds")
    if abs(output_duration - duration) > Decimal("0.001"):
        raise NarrationReviewGateError("A2 duration does not match the WAV")
    if output.get("sample_rate_hz") != sample_rate or output.get("frame_count") != frame_count:
        raise NarrationReviewGateError("A2 WAV metadata does not match the step")
    voice_id = _text_output(output, "voice_id")
    backend = _text_output(output, "backend")
    model_id = _text_output(output, "model_id")
    license_id = _text_output(output, "license_id")

    word_count = len(full_script.split())
    desired_visuals = max(
        1,
        min(_MAX_VISUALS, math.ceil(duration / _SECONDS_PER_VISUAL)),
    )
    visual_count = min(desired_visuals, word_count)
    width, height = (1080, 1920) if format_value == "short" else (1920, 1080)
    return _VerifiedNarrationEvidence(
        audio=audio_artifact,
        audio_path=audio_path,
        duration_seconds=str(duration),
        sample_rate_hz=sample_rate,
        frame_count=frame_count,
        voice_id=voice_id,
        backend=backend,
        model_id=model_id,
        license_id=license_id,
        format=format_value,
        required_visual_count=visual_count,
        visual_width=width,
        visual_height=height,
    )


def _verify_artifact_file(
    storage_root: Path,
    artifact: Artifact,
    *,
    max_bytes: int,
    read_content: bool,
) -> tuple[Path, bytes]:
    if artifact.size_bytes < 1 or artifact.size_bytes > max_bytes:
        raise NarrationReviewGateError("review artifact size is invalid")
    try:
        root = storage_root.resolve(strict=True)
        candidate = (root / artifact.relative_path).resolve(strict=True)
        candidate.relative_to(root)
        if not candidate.is_file():
            raise OSError("not a regular file")
        digest = hashlib.sha256()
        content = bytearray() if read_content else None
        actual_size = 0
        with candidate.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                actual_size += len(chunk)
                if actual_size > max_bytes:
                    raise OSError("artifact exceeds review limit")
                digest.update(chunk)
                if content is not None:
                    content.extend(chunk)
    except (OSError, ValueError) as exc:
        raise NarrationReviewGateError("review artifact is unavailable") from exc
    if actual_size != artifact.size_bytes:
        raise NarrationReviewGateError("review artifact size does not match")
    if digest.hexdigest() != artifact.sha256:
        raise NarrationReviewGateError("review artifact checksum does not match")
    return candidate, bytes(content or b"")


async def _required_artifact(
    session: AsyncSession,
    *,
    run: Run,
    artifact_type: str,
) -> Artifact:
    artifacts = list(
        (
            await session.scalars(
                select(Artifact)
                .where(
                    Artifact.run_id == run.id,
                    Artifact.artifact_type == artifact_type,
                )
                .order_by(Artifact.id)
            )
        ).all()
    )
    if len(artifacts) != 1:
        raise NarrationReviewGateError("A2 review evidence is missing or ambiguous")
    return artifacts[0]


def _approval_input(
    *,
    run: Run,
    evidence: _VerifiedNarrationEvidence,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "run_id": _persisted_id(run.id, "run"),
        "audio_artifact_id": _persisted_id(evidence.audio.id, "artifact"),
        "audio_sha256": evidence.audio.sha256,
        "duration_seconds": evidence.duration_seconds,
        "voice_id": evidence.voice_id,
        "backend": evidence.backend,
        "model_id": evidence.model_id,
        "license_id": evidence.license_id,
        "format": evidence.format,
        "required_visual_count": evidence.required_visual_count,
        "visual_width": evidence.visual_width,
        "visual_height": evidence.visual_height,
    }


def _review_payload(evidence: _VerifiedNarrationEvidence) -> dict[str, str]:
    return {
        "audio": (
            f"Narração #{evidence.audio.id} · {evidence.duration_seconds} s · "
            f"{evidence.sample_rate_hz} Hz · {evidence.audio.size_bytes} bytes"
        ),
        "voice": f"{evidence.voice_id} · {evidence.backend}",
        "format": "Short" if evidence.format == "short" else "Longo",
        "visual_plan": (
            f"Preparar exatamente {evidence.required_visual_count} PNGs de "
            f"{evidence.visual_width}x{evidence.visual_height} antes de aprovar."
        ),
        "provenance": evidence.license_id,
        "audio_sha256": evidence.audio.sha256,
        "publication": "Nenhum upload, envio ou gasto é executado por esta aprovação.",
    }


def _decimal_output(payload: dict[str, object], key: str) -> Decimal:
    value = payload.get(key)
    if not isinstance(value, str):
        raise NarrationReviewGateError("A2 step output is invalid")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise NarrationReviewGateError("A2 step output is invalid") from exc
    if not parsed.is_finite():
        raise NarrationReviewGateError("A2 step output is invalid")
    return parsed


def _text_output(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > 1_000:
        raise NarrationReviewGateError("A2 step output is invalid")
    return value.strip()


def _persisted_id(value: int | None, kind: str) -> int:
    if not isinstance(value, int):
        raise NarrationReviewGateError(f"{kind} must be persisted")
    return value
