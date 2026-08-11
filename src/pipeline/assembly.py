"""Verified, adapter-driven Content Engine local MP4 assembly step."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import struct
import subprocess
import tempfile
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import BinaryIO, Literal
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from src.platform.approval_service import approval_payload_digest
from src.platform.artifact_service import register_local_artifact
from src.platform.ledger_service import record_ledger_entry
from src.platform.metric_service import record_metric_point
from src.platform.models import (
    Approval,
    ApprovalStatus,
    Artifact,
    ArtifactSensitivity,
    Automation,
    LedgerEntryType,
    MetricKind,
    QueueClass,
    Run,
    RunStatus,
    StepRun,
)
from src.platform.run_service import transition_run
from src.platform.task_runner import (
    PermanentTaskError,
    RetryableTaskError,
    RetryPolicy,
    Sleeper,
    TaskAttemptContext,
    TaskStepSpec,
    execute_task_step,
)

ASSEMBLY_STEP_NAME = "assemble-video-a5"
_SOURCE = "content-engine:a5"
_RETENTION_DAYS = 90
_MAX_MANIFEST_BYTES = 5 * 1024 * 1024
_MAX_CAPTION_BYTES = 20 * 1024 * 1024
_MAX_MP4_BYTES = 20 * 1024 * 1024 * 1024
_MAX_MOOV_BYTES = 64 * 1024 * 1024
_DURATION_TOLERANCE_SECONDS = Decimal("0.50")
_QUANTUM = Decimal("0.0000000001")


class AssemblyRetryableAdapterError(RuntimeError):
    """Raised when the media assembler can be retried unchanged."""


class AssemblyPermanentAdapterError(RuntimeError):
    """Raised when assembly configuration, rights or output is invalid."""


@dataclass(frozen=True, slots=True)
class FFmpegQualityTestConfig:
    """Pinned external binaries for non-monetized local playback validation."""

    ffmpeg_path: str
    ffprobe_path: str
    ffmpeg_sha256: str
    ffprobe_sha256: str
    timeout_seconds: float = 300.0


@dataclass(frozen=True, slots=True)
class AssemblyRequest:
    """Verified local inputs and immutable render parameters for one video."""

    audio_path: Path
    visual_paths: tuple[Path, ...]
    caption_path: Path
    format: Literal["short", "long"]
    width: int
    height: int
    duration_seconds: Decimal


@dataclass(frozen=True, slots=True)
class AssemblyAdapterResult:
    """Render and probe metadata returned by an assembly adapter."""

    backend: str
    provider: str
    tool_id: str
    license_id: str
    commercial_use: bool
    duration_seconds: Decimal
    width: int
    height: int
    video_codec: str
    audio_codec: str
    subtitles_burned: bool


AssemblyAdapter = Callable[[AssemblyRequest, Path], AssemblyAdapterResult]


@dataclass(frozen=True, slots=True)
class AssemblyExecutionResult:
    """Observable identifiers produced by the A5 step."""

    run_id: int
    step_run_id: int | None
    artifact_id: int | None
    run_status: str
    step_status: str | None
    replayed: bool


@dataclass(frozen=True, slots=True)
class _AssemblyContext:
    script_artifact_id: int
    audio_artifact_id: int
    audio_step_run_id: int
    visual_manifest_artifact_id: int
    visual_step_run_id: int
    visual_artifact_ids: tuple[int, ...]
    caption_artifact_id: int
    caption_step_run_id: int
    audio_path: Path
    visual_paths: tuple[Path, ...]
    caption_path: Path
    format: Literal["short", "long"]
    width: int
    height: int
    duration_seconds: Decimal


@dataclass(frozen=True, slots=True)
class _Mp4Inspection:
    duration_seconds: Decimal
    width: int
    height: int
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class _AssemblyBundle:
    result: AssemblyAdapterResult
    inspection: _Mp4Inspection
    render_seconds: Decimal


@dataclass(frozen=True, slots=True)
class _MemoryBox:
    kind: bytes
    payload: bytes


def get_configured_assembly_adapter(
    backend: str,
    *,
    quality_test_config: FFmpegQualityTestConfig | None = None,
) -> AssemblyAdapter:
    """Select only the pinned FFmpeg quality-test backend or fail closed."""
    normalized = backend.strip().casefold()
    if normalized == "ffmpeg_quality_test":
        if quality_test_config is None:
            def missing_config(
                _request: AssemblyRequest,
                _destination: Path,
            ) -> AssemblyAdapterResult:
                raise AssemblyPermanentAdapterError(
                    "CONTENT_ASSEMBLY_FFMPEG_CONFIG_REQUIRED"
                )

            return missing_config
        def configured_quality_test(
            request: AssemblyRequest,
            destination: Path,
        ) -> AssemblyAdapterResult:
            adapter = build_ffmpeg_quality_test_adapter(quality_test_config)
            return adapter(request, destination)

        return configured_quality_test

    def unavailable(
        _request: AssemblyRequest,
        _destination: Path,
    ) -> AssemblyAdapterResult:
        code = (
            "CONTENT_ASSEMBLY_DISABLED"
            if normalized == "disabled"
            else "CONTENT_ASSEMBLY_BACKEND_UNREVIEWED"
        )
        raise AssemblyPermanentAdapterError(code)

    return unavailable


def build_ffmpeg_quality_test_adapter(
    config: FFmpegQualityTestConfig,
) -> AssemblyAdapter:
    """Build an adapter around exact external FFmpeg/ffprobe executables."""
    ffmpeg = _verified_tool_path(
        config.ffmpeg_path,
        expected_sha256=config.ffmpeg_sha256,
        tool="FFMPEG",
    )
    ffprobe = _verified_tool_path(
        config.ffprobe_path,
        expected_sha256=config.ffprobe_sha256,
        tool="FFPROBE",
    )
    if not 1 <= config.timeout_seconds <= 3600:
        raise AssemblyPermanentAdapterError("CONTENT_ASSEMBLY_FFMPEG_TIMEOUT_INVALID")
    version_output = _run_media_tool(
        [str(ffmpeg), "-hide_banner", "-version"],
        timeout_seconds=min(config.timeout_seconds, 30.0),
        retryable_code="CONTENT_ASSEMBLY_FFMPEG_INVENTORY_UNAVAILABLE",
        permanent_code="CONTENT_ASSEMBLY_FFMPEG_INVENTORY_INVALID",
    )
    required_flags = (
        "--enable-gpl",
        "--enable-version3",
        "--enable-static",
        "--enable-libx264",
        "--enable-libass",
    )
    if (
        "ffmpeg version " not in version_output.casefold()
        or any(flag not in version_output for flag in required_flags)
    ):
        raise AssemblyPermanentAdapterError(
            "CONTENT_ASSEMBLY_FFMPEG_BUILD_UNSUPPORTED"
        )
    first_line = version_output.splitlines()[0].strip()
    if not first_line or len(first_line) > 180:
        raise AssemblyPermanentAdapterError("CONTENT_ASSEMBLY_FFMPEG_VERSION_INVALID")

    def assemble(request: AssemblyRequest, destination: Path) -> AssemblyAdapterResult:
        if not request.visual_paths:
            raise AssemblyPermanentAdapterError("CONTENT_ASSEMBLY_VISUALS_REQUIRED")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="ffmpeg-quality-test-",
            dir=destination.parent,
        ) as workspace_value:
            workspace = Path(workspace_value)
            caption_name = "captions.ass"
            shutil.copyfile(request.caption_path, workspace / caption_name)
            segment_duration = request.duration_seconds / Decimal(
                len(request.visual_paths)
            )
            command = [str(ffmpeg), "-hide_banner", "-nostdin", "-loglevel", "error"]
            for visual_path in request.visual_paths:
                command.extend(
                    [
                        "-loop",
                        "1",
                        "-framerate",
                        "30",
                        "-t",
                        _ffmpeg_decimal(segment_duration),
                        "-i",
                        str(visual_path),
                    ]
                )
            audio_index = len(request.visual_paths)
            command.extend(["-i", str(request.audio_path)])
            command.extend(
                [
                    "-filter_complex",
                    _ffmpeg_filter_graph(
                        visual_count=len(request.visual_paths),
                        width=request.width,
                        height=request.height,
                        caption_name=caption_name,
                    ),
                    "-map",
                    "[video_out]",
                    "-map",
                    f"{audio_index}:a:0",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "medium",
                    "-crf",
                    "20",
                    "-pix_fmt",
                    "yuv420p",
                    "-r",
                    "30",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-shortest",
                    "-movflags",
                    "+faststart",
                    "-y",
                    str(destination),
                ]
            )
            _run_media_tool(
                command,
                timeout_seconds=config.timeout_seconds,
                cwd=workspace,
                retryable_code="CONTENT_ASSEMBLY_FFMPEG_UNAVAILABLE",
                permanent_code="CONTENT_ASSEMBLY_FFMPEG_RENDER_FAILED",
            )
            probe = _probe_rendered_video(
                ffprobe,
                destination,
                timeout_seconds=min(config.timeout_seconds, 60.0),
            )
        return AssemblyAdapterResult(
            backend="ffmpeg_quality_test",
            provider="gyan-winget-external",
            tool_id=first_line,
            license_id="GPL-3.0-only-external-tool",
            commercial_use=True,
            duration_seconds=probe[0],
            width=probe[1],
            height=probe[2],
            video_codec=probe[3],
            audio_codec=probe[4],
            subtitles_burned=True,
        )

    return assemble


def _verified_tool_path(
    configured_path: str,
    *,
    expected_sha256: str,
    tool: str,
) -> Path:
    normalized_path = configured_path.strip()
    normalized_hash = expected_sha256.strip().casefold()
    if not normalized_path:
        raise AssemblyPermanentAdapterError(
            f"CONTENT_ASSEMBLY_{tool}_PATH_REQUIRED"
        )
    if len(normalized_hash) != 64 or any(
        character not in "0123456789abcdef" for character in normalized_hash
    ):
        raise AssemblyPermanentAdapterError(
            f"CONTENT_ASSEMBLY_{tool}_HASH_INVALID"
        )
    discovered = shutil.which(normalized_path)
    candidate = Path(discovered if discovered is not None else normalized_path)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise AssemblyPermanentAdapterError(
            f"CONTENT_ASSEMBLY_{tool}_UNAVAILABLE"
        ) from exc
    if not resolved.is_file() or _sha256_file(resolved) != normalized_hash:
        raise AssemblyPermanentAdapterError(
            f"CONTENT_ASSEMBLY_{tool}_HASH_MISMATCH"
        )
    return resolved


def _run_media_tool(
    arguments: list[str],
    *,
    timeout_seconds: float,
    retryable_code: str,
    permanent_code: str,
    cwd: Path | None = None,
) -> str:
    try:
        completed = subprocess.run(
            arguments,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AssemblyRetryableAdapterError(retryable_code) from exc
    if completed.returncode != 0:
        raise AssemblyPermanentAdapterError(permanent_code)
    return f"{completed.stdout}\n{completed.stderr}".strip()


def _ffmpeg_decimal(value: Decimal) -> str:
    parsed = _decimal(value, "ffmpeg duration")
    return format(parsed.quantize(Decimal("0.000001")), "f")


def _ffmpeg_filter_graph(
    *,
    visual_count: int,
    width: int,
    height: int,
    caption_name: str,
) -> str:
    if visual_count < 1 or caption_name != "captions.ass":
        raise AssemblyPermanentAdapterError("CONTENT_ASSEMBLY_FFMPEG_FILTER_INVALID")
    filters = [
        (
            f"[{index}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"setsar=1,fps=30,format=yuv420p[visual_{index}]"
        )
        for index in range(visual_count)
    ]
    if visual_count == 1:
        filters.append("[visual_0]null[video_base]")
    else:
        inputs = "".join(f"[visual_{index}]" for index in range(visual_count))
        filters.append(f"{inputs}concat=n={visual_count}:v=1:a=0[video_base]")
    filters.append(f"[video_base]ass=filename={caption_name}[video_out]")
    return ";".join(filters)


def _probe_rendered_video(
    ffprobe: Path,
    path: Path,
    *,
    timeout_seconds: float,
) -> tuple[Decimal, int, int, str, str]:
    output = _run_media_tool(
        [
            str(ffprobe),
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name,width,height:format=duration",
            "-of",
            "json",
            str(path),
        ],
        timeout_seconds=timeout_seconds,
        retryable_code="CONTENT_ASSEMBLY_FFPROBE_UNAVAILABLE",
        permanent_code="CONTENT_ASSEMBLY_FFPROBE_FAILED",
    )
    try:
        payload = json.loads(output)
        streams = payload["streams"]
        duration = Decimal(str(payload["format"]["duration"]))
    except (KeyError, TypeError, ValueError, InvalidOperation, json.JSONDecodeError) as exc:
        raise AssemblyPermanentAdapterError(
            "CONTENT_ASSEMBLY_FFPROBE_INVALID_OUTPUT"
        ) from exc
    if not isinstance(streams, list) or len(streams) != 2:
        raise AssemblyPermanentAdapterError("CONTENT_ASSEMBLY_UNEXPECTED_STREAMS")
    video_streams = [
        stream
        for stream in streams
        if isinstance(stream, dict) and stream.get("codec_type") == "video"
    ]
    audio_streams = [
        stream
        for stream in streams
        if isinstance(stream, dict) and stream.get("codec_type") == "audio"
    ]
    if len(video_streams) != 1 or len(audio_streams) != 1:
        raise AssemblyPermanentAdapterError("CONTENT_ASSEMBLY_UNEXPECTED_STREAMS")
    video = video_streams[0]
    audio = audio_streams[0]
    width = video.get("width")
    height = video.get("height")
    video_codec = video.get("codec_name")
    audio_codec = audio.get("codec_name")
    if (
        not duration.is_finite()
        or duration <= 0
        or isinstance(width, bool)
        or not isinstance(width, int)
        or isinstance(height, bool)
        or not isinstance(height, int)
        or not isinstance(video_codec, str)
        or not isinstance(audio_codec, str)
    ):
        raise AssemblyPermanentAdapterError(
            "CONTENT_ASSEMBLY_FFPROBE_INVALID_OUTPUT"
        )
    return (
        duration.quantize(_QUANTUM),
        width,
        height,
        video_codec,
        audio_codec,
    )


async def execute_approved_assembly_step(
    session: AsyncSession,
    *,
    run: Run,
    approval: Approval,
    approval_input_payload: dict[str, object],
    audio_artifact: Artifact,
    visual_manifest_artifact: Artifact,
    visual_artifacts: tuple[Artifact, ...],
    caption_artifact: Artifact,
    idempotency_key: str,
    storage_root: Path,
    adapter: AssemblyAdapter,
    cpu_power_watts: int,
    timeout_seconds: float,
    finalize_run: bool = True,
    sleep: Sleeper = asyncio.sleep,
) -> AssemblyExecutionResult:
    """Assemble and register one reviewable local MP4 without publishing it."""
    if cpu_power_watts < 1:
        raise ValueError("cpu_power_watts must be positive")
    context = _load_assembly_context(
        run=run,
        approval=approval,
        approval_input_payload=approval_input_payload,
        audio_artifact=audio_artifact,
        visual_manifest_artifact=visual_manifest_artifact,
        visual_artifacts=visual_artifacts,
        caption_artifact=caption_artifact,
        storage_root=storage_root,
    )
    output_path = _video_path(storage_root, run_id=_persisted_id(run.id, "run"))
    step_key = f"{idempotency_key}:{ASSEMBLY_STEP_NAME}"

    async def assemble_operation(_context: TaskAttemptContext) -> dict[str, object]:
        request = AssemblyRequest(
            audio_path=context.audio_path,
            visual_paths=context.visual_paths,
            caption_path=context.caption_path,
            format=context.format,
            width=context.width,
            height=context.height,
            duration_seconds=context.duration_seconds,
        )
        try:
            bundle = await asyncio.to_thread(
                _assemble_atomic,
                adapter,
                request,
                output_path,
            )
        except AssemblyRetryableAdapterError as exc:
            raise RetryableTaskError("CONTENT_ASSEMBLY_BACKEND_UNAVAILABLE") from exc
        except AssemblyPermanentAdapterError as exc:
            raise PermanentTaskError(str(exc) or "CONTENT_ASSEMBLY_INVALID_OUTPUT") from exc
        return {
            "artifact_relative_path": output_path.relative_to(
                storage_root.resolve()
            ).as_posix(),
            "sha256": bundle.inspection.sha256,
            "size_bytes": bundle.inspection.size_bytes,
            "backend": bundle.result.backend,
            "provider": bundle.result.provider,
            "tool_id": bundle.result.tool_id,
            "license_id": bundle.result.license_id,
            "commercial_use": bundle.result.commercial_use,
            "duration_seconds": str(bundle.inspection.duration_seconds),
            "width": bundle.inspection.width,
            "height": bundle.inspection.height,
            "video_codec": bundle.result.video_codec,
            "audio_codec": bundle.result.audio_codec,
            "subtitles_burned": bundle.result.subtitles_burned,
            "render_seconds": str(bundle.render_seconds),
            "format": context.format,
        }

    task_result = await execute_task_step(
        session,
        run=run,
        spec=TaskStepSpec(
            name=ASSEMBLY_STEP_NAME,
            queue=QueueClass.CPU,
            ordinal=5,
            idempotency_key=step_key,
            input_payload={
                **approval_input_payload,
                "audio_artifact_id": context.audio_artifact_id,
                "visual_manifest_artifact_id": context.visual_manifest_artifact_id,
                "visual_step_run_id": context.visual_step_run_id,
                "visual_artifact_ids": list(context.visual_artifact_ids),
                "caption_artifact_id": context.caption_artifact_id,
                "caption_step_run_id": context.caption_step_run_id,
                "format": context.format,
                "width": context.width,
                "height": context.height,
                "output_format": "mp4-h264-aac-burned-ass",
            },
        ),
        policy=RetryPolicy(max_attempts=2, timeout_seconds=timeout_seconds),
        operation=assemble_operation,
        sleep=sleep,
    )
    step_run = await session.get(StepRun, task_result.step_run_ids[-1])
    if step_run is None:
        raise ValueError("assembly step result was not persisted")
    if task_result.status is RunStatus.CANCELLED:
        return _result(run, step_run, None, replayed=task_result.replayed)
    if task_result.status is RunStatus.FAILED:
        if task_result.error is None:
            raise ValueError("failed assembly step is missing structured error")
        if RunStatus(run.status) is RunStatus.RUNNING:
            await transition_run(
                session,
                run,
                RunStatus.FAILED,
                error=task_result.error,
                note="Content Engine A5 assembly failed",
            )
        return _result(run, step_run, None, replayed=task_result.replayed)

    try:
        async with session.begin_nested():
            automation = await session.get(Automation, run.automation_id)
            if automation is None:
                raise ValueError("assembly automation does not exist")
            output = task_result.output_payload or {}
            artifact = (
                await register_local_artifact(
                    session,
                    run=run,
                    step_run=step_run,
                    storage_root=storage_root,
                    file_path=_output_path(storage_root, output),
                    idempotency_key=f"{idempotency_key}:final-video",
                    artifact_type="final_video",
                    media_type="video/mp4",
                    origin=_SOURCE,
                    sensitivity=ArtifactSensitivity.INTERNAL,
                    retention_days=_RETENTION_DAYS,
                    expected_sha256=_required_text(output, "sha256", max_length=64),
                )
            ).artifact
            dimensions = _metric_dimensions(output)
            duration_metric = (
                await record_metric_point(
                    session,
                    automation=automation,
                    run=run,
                    step_run=step_run,
                    idempotency_key=f"{idempotency_key}:assembly-video-duration",
                    name="content_assembly_video_duration_seconds",
                    kind=MetricKind.DURATION,
                    value=_decimal_output(output, "duration_seconds"),
                    unit="seconds",
                    source=_SOURCE,
                    dimensions=dimensions,
                )
            ).metric_point
            render_seconds = _decimal_output(output, "render_seconds")
            render_metric = (
                await record_metric_point(
                    session,
                    automation=automation,
                    run=run,
                    step_run=step_run,
                    idempotency_key=f"{idempotency_key}:assembly-render-duration",
                    name="content_assembly_render_duration_seconds",
                    kind=MetricKind.DURATION,
                    value=render_seconds,
                    unit="seconds",
                    source=_SOURCE,
                    dimensions=dimensions,
                )
            ).metric_point
            size_metric = (
                await record_metric_point(
                    session,
                    automation=automation,
                    run=run,
                    step_run=step_run,
                    idempotency_key=f"{idempotency_key}:assembly-output-bytes",
                    name="content_assembly_output_bytes",
                    kind=MetricKind.GAUGE,
                    value=_required_int(output, "size_bytes", minimum=1),
                    unit="bytes",
                    source=_SOURCE,
                    dimensions=dimensions,
                )
            ).metric_point
            energy = (render_seconds / Decimal("3600")) * (
                Decimal(cpu_power_watts) / Decimal("1000")
            )
            energy_metric = (
                await record_metric_point(
                    session,
                    automation=automation,
                    run=run,
                    step_run=step_run,
                    idempotency_key=f"{idempotency_key}:assembly-energy-estimate",
                    name="content_assembly_energy_estimate_kwh",
                    kind=MetricKind.GAUGE,
                    value=energy.quantize(_QUANTUM),
                    unit="kWh",
                    source=_SOURCE,
                    confidence="0.4",
                    dimensions=dimensions,
                )
            ).metric_point
            ledger = (
                await record_ledger_entry(
                    session,
                    automation=automation,
                    run=run,
                    step_run=step_run,
                    idempotency_key=f"{idempotency_key}:assembly-external-api-cost",
                    entry_type=LedgerEntryType.COST,
                    category="external_api",
                    amount=0,
                    currency="BRL",
                    source=_SOURCE,
                    confidence=1,
                )
            ).ledger_entry
    except Exception as exc:
        error = {
            "code": "CONTENT_ASSEMBLY_EVIDENCE_PERSIST_FAILED",
            "exception_type": type(exc).__name__,
            "retryable": True,
            "timed_out": False,
        }
        if RunStatus(run.status) is RunStatus.RUNNING:
            await transition_run(
                session,
                run,
                RunStatus.FAILED,
                error=error,
                note="Content Engine A5 evidence persistence failed",
            )
        return _result(run, step_run, None, replayed=task_result.replayed)

    if finalize_run and RunStatus(run.status) is RunStatus.RUNNING:
        await transition_run(
            session,
            run,
            RunStatus.SUCCEEDED,
            output_payload={
                "script_artifact_id": context.script_artifact_id,
                "script_approval_id": approval.id,
                "tts_step_run_id": context.audio_step_run_id,
                "tts_artifact_id": context.audio_artifact_id,
                "visual_manifest_artifact_id": context.visual_manifest_artifact_id,
                "visual_artifact_ids": list(context.visual_artifact_ids),
                "caption_artifact_id": context.caption_artifact_id,
                "assembly_step_run_id": step_run.id,
                "final_video_artifact_id": artifact.id,
                "assembly_duration_metric_id": duration_metric.id,
                "assembly_render_metric_id": render_metric.id,
                "assembly_size_metric_id": size_metric.id,
                "assembly_energy_metric_id": energy_metric.id,
                "assembly_ledger_entry_id": ledger.id,
            },
            note="Content Engine A5 created a local reviewable video; nothing published",
        )
    return _result(
        run,
        step_run,
        _persisted_id(artifact.id, "artifact"),
        replayed=task_result.replayed,
    )


def _load_assembly_context(
    *,
    run: Run,
    approval: Approval,
    approval_input_payload: dict[str, object],
    audio_artifact: Artifact,
    visual_manifest_artifact: Artifact,
    visual_artifacts: tuple[Artifact, ...],
    caption_artifact: Artifact,
    storage_root: Path,
) -> _AssemblyContext:
    run_id = _persisted_id(run.id, "run")
    if approval.run_id != run_id:
        raise ValueError("assembly approval must belong to the run")
    if ApprovalStatus(approval.status) is not ApprovalStatus.APPROVED:
        raise ValueError("assembly script approval must be approved")
    if approval.payload_digest != approval_payload_digest(approval_input_payload):
        raise ValueError("assembly approval payload does not match durable approval")
    script_artifact_id = approval_input_payload.get("artifact_id")
    script_artifact_sha256 = approval_input_payload.get("artifact_sha256")
    if isinstance(script_artifact_id, bool) or not isinstance(script_artifact_id, int):
        raise ValueError("assembly approval does not identify a script artifact")
    if (
        not isinstance(script_artifact_sha256, str)
        or len(script_artifact_sha256) != 64
        or any(character not in "0123456789abcdef" for character in script_artifact_sha256)
    ):
        raise ValueError("assembly approval has an invalid script checksum")
    source_artifacts = (
        audio_artifact,
        visual_manifest_artifact,
        caption_artifact,
        *visual_artifacts,
    )
    if any(artifact.run_id != run_id for artifact in source_artifacts):
        raise ValueError("assembly source artifacts must belong to the run")
    if (
        audio_artifact.artifact_type != "narration_audio"
        or audio_artifact.media_type != "audio/wav"
        or not isinstance(audio_artifact.step_run_id, int)
    ):
        raise ValueError("assembly audio source is invalid")
    if visual_manifest_artifact.artifact_type != "visual_manifest":
        raise ValueError("assembly visual manifest source is invalid")
    if not isinstance(visual_manifest_artifact.step_run_id, int):
        raise ValueError("assembly visual manifest has no producing step")
    if not visual_artifacts or any(
        artifact.artifact_type != "visual_image" for artifact in visual_artifacts
    ):
        raise ValueError("assembly visual image sources are invalid")
    if any(
        artifact.step_run_id != visual_manifest_artifact.step_run_id
        for artifact in visual_artifacts
    ):
        raise ValueError("assembly visual sources have conflicting producing steps")
    if len({artifact.id for artifact in visual_artifacts}) != len(visual_artifacts):
        raise ValueError("assembly visual image sources contain duplicates")
    if (
        caption_artifact.artifact_type != "caption_ass"
        or caption_artifact.media_type != "text/x-ssa"
        or not isinstance(caption_artifact.step_run_id, int)
    ):
        raise ValueError("assembly caption source is invalid")

    audio_path = _verified_artifact_path(storage_root, audio_artifact)
    manifest_path = _verified_artifact_path(storage_root, visual_manifest_artifact)
    caption_path = _verified_artifact_path(storage_root, caption_artifact)
    visual_paths = tuple(
        _verified_artifact_path(storage_root, artifact) for artifact in visual_artifacts
    )
    duration = _wav_duration(audio_path)
    video_format = _run_format(run)
    width, height = (1080, 1920) if video_format == "short" else (1920, 1080)
    _validate_visual_manifest(
        manifest_path,
        manifest_artifact=visual_manifest_artifact,
        visual_artifacts=visual_artifacts,
        script_artifact_id=script_artifact_id,
        script_artifact_sha256=script_artifact_sha256,
        audio_artifact=audio_artifact,
        video_format=video_format,
        width=width,
        height=height,
    )
    _validate_caption_file(
        caption_path,
        width=width,
        height=height,
    )
    return _AssemblyContext(
        script_artifact_id=script_artifact_id,
        audio_artifact_id=_persisted_id(audio_artifact.id, "audio artifact"),
        audio_step_run_id=audio_artifact.step_run_id,
        visual_manifest_artifact_id=_persisted_id(
            visual_manifest_artifact.id,
            "visual manifest artifact",
        ),
        visual_step_run_id=visual_manifest_artifact.step_run_id,
        visual_artifact_ids=tuple(
            _persisted_id(artifact.id, "visual artifact")
            for artifact in visual_artifacts
        ),
        caption_artifact_id=_persisted_id(caption_artifact.id, "caption artifact"),
        caption_step_run_id=caption_artifact.step_run_id,
        audio_path=audio_path,
        visual_paths=visual_paths,
        caption_path=caption_path,
        format=video_format,
        width=width,
        height=height,
        duration_seconds=duration,
    )


def _validate_visual_manifest(
    path: Path,
    *,
    manifest_artifact: Artifact,
    visual_artifacts: tuple[Artifact, ...],
    script_artifact_id: int,
    script_artifact_sha256: str,
    audio_artifact: Artifact,
    video_format: str,
    width: int,
    height: int,
) -> None:
    if manifest_artifact.size_bytes > _MAX_MANIFEST_BYTES:
        raise ValueError("assembly visual manifest exceeds the size limit")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("assembly visual manifest is invalid") from exc
    assets = payload.get("assets") if isinstance(payload, dict) else None
    expected_identity = (
        payload.get("schema_version") if isinstance(payload, dict) else None,
        payload.get("script_artifact_id") if isinstance(payload, dict) else None,
        payload.get("script_artifact_sha256") if isinstance(payload, dict) else None,
        payload.get("audio_artifact_id") if isinstance(payload, dict) else None,
        payload.get("audio_artifact_sha256") if isinstance(payload, dict) else None,
        payload.get("format") if isinstance(payload, dict) else None,
        payload.get("visual_count") if isinstance(payload, dict) else None,
    )
    if expected_identity != (
        1,
        script_artifact_id,
        script_artifact_sha256,
        audio_artifact.id,
        audio_artifact.sha256,
        video_format,
        len(visual_artifacts),
    ):
        raise ValueError("assembly visual manifest conflicts with source evidence")
    if not isinstance(assets, list) or len(assets) != len(visual_artifacts):
        raise ValueError("assembly visual manifest assets are invalid")
    for asset, artifact in zip(assets, visual_artifacts, strict=True):
        if not isinstance(asset, dict):
            raise ValueError("assembly visual manifest asset is invalid")
        if (
            asset.get("file_name") != Path(artifact.relative_path).name
            or asset.get("sha256") != artifact.sha256
            or asset.get("size_bytes") != artifact.size_bytes
            or asset.get("width") != width
            or asset.get("height") != height
            or asset.get("commercial_use") is not True
        ):
            raise ValueError("assembly visual manifest asset conflicts with artifact")


def _validate_caption_file(path: Path, *, width: int, height: int) -> None:
    try:
        size = path.stat().st_size
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError("assembly caption file is unavailable") from exc
    if not 1 <= size <= _MAX_CAPTION_BYTES:
        raise ValueError("assembly caption file size is invalid")
    required = (
        "[Script Info]",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "[Events]",
        "Dialogue:",
        "{\\kf",
    )
    if any(value not in text for value in required):
        raise ValueError("assembly caption file is invalid")


def _assemble_atomic(
    adapter: AssemblyAdapter,
    request: AssemblyRequest,
    destination: Path,
) -> _AssemblyBundle:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.stem}.{uuid4().hex}.tmp.mp4")
    started = time.perf_counter()
    try:
        result = adapter(request, temporary)
        inspection = _inspect_mp4(temporary)
        _validate_adapter_result(result, request=request, inspection=inspection)
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        return _AssemblyBundle(
            result=result,
            inspection=inspection,
            render_seconds=Decimal(
                str(round(time.perf_counter() - started, 6))
            ).quantize(_QUANTUM),
        )
    except (AssemblyRetryableAdapterError, AssemblyPermanentAdapterError):
        raise
    except Exception as exc:
        raise AssemblyPermanentAdapterError("CONTENT_ASSEMBLY_INVALID_OUTPUT") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _validate_adapter_result(
    result: AssemblyAdapterResult,
    *,
    request: AssemblyRequest,
    inspection: _Mp4Inspection,
) -> None:
    if not isinstance(result, AssemblyAdapterResult):
        raise AssemblyPermanentAdapterError("CONTENT_ASSEMBLY_INVALID_RESULT")
    for value in (
        result.backend,
        result.provider,
        result.tool_id,
        result.license_id,
        result.video_codec,
        result.audio_codec,
    ):
        if not isinstance(value, str) or not value.strip() or len(value) > 200:
            raise AssemblyPermanentAdapterError("CONTENT_ASSEMBLY_INVALID_PROVENANCE")
    if result.commercial_use is not True:
        raise AssemblyPermanentAdapterError("CONTENT_ASSEMBLY_COMMERCIAL_RIGHTS_REQUIRED")
    if result.subtitles_burned is not True:
        raise AssemblyPermanentAdapterError("CONTENT_ASSEMBLY_SUBTITLES_REQUIRED")
    if result.video_codec.strip().casefold() != "h264":
        raise AssemblyPermanentAdapterError("CONTENT_ASSEMBLY_H264_REQUIRED")
    if result.audio_codec.strip().casefold() != "aac":
        raise AssemblyPermanentAdapterError("CONTENT_ASSEMBLY_AAC_REQUIRED")
    declared_duration = _decimal(result.duration_seconds, "adapter duration")
    if (
        result.width != request.width
        or result.height != request.height
        or inspection.width != request.width
        or inspection.height != request.height
    ):
        raise AssemblyPermanentAdapterError("CONTENT_ASSEMBLY_DIMENSIONS_MISMATCH")
    if (
        abs(declared_duration - request.duration_seconds)
        > _DURATION_TOLERANCE_SECONDS
        or abs(inspection.duration_seconds - request.duration_seconds)
        > _DURATION_TOLERANCE_SECONDS
        or abs(declared_duration - inspection.duration_seconds)
        > _DURATION_TOLERANCE_SECONDS
    ):
        raise AssemblyPermanentAdapterError("CONTENT_ASSEMBLY_DURATION_MISMATCH")


def _inspect_mp4(path: Path) -> _Mp4Inspection:
    try:
        size_bytes = path.stat().st_size
    except OSError as exc:
        raise AssemblyPermanentAdapterError("CONTENT_ASSEMBLY_OUTPUT_UNAVAILABLE") from exc
    if not 32 <= size_bytes <= _MAX_MP4_BYTES:
        raise AssemblyPermanentAdapterError("CONTENT_ASSEMBLY_INVALID_MP4_SIZE")
    ftyp_count = 0
    mdat_count = 0
    moov_payload: bytes | None = None
    try:
        with path.open("rb") as handle:
            offset = 0
            box_count = 0
            while offset < size_bytes:
                kind, payload_offset, box_end = _stream_box_header(
                    handle,
                    offset=offset,
                    file_size=size_bytes,
                )
                box_count += 1
                if box_count > 10_000:
                    raise AssemblyPermanentAdapterError(
                        "CONTENT_ASSEMBLY_TOO_MANY_MP4_BOXES"
                    )
                payload_size = box_end - payload_offset
                if kind == b"ftyp":
                    ftyp_count += 1
                    if payload_size < 8 or payload_size > 1024:
                        raise AssemblyPermanentAdapterError(
                            "CONTENT_ASSEMBLY_INVALID_FTYP"
                        )
                    handle.seek(payload_offset)
                    brands = handle.read(payload_size)
                    if not any(
                        brand in brands for brand in (b"isom", b"mp41", b"mp42")
                    ):
                        raise AssemblyPermanentAdapterError(
                            "CONTENT_ASSEMBLY_UNSUPPORTED_MP4_BRAND"
                        )
                elif kind == b"moov":
                    if payload_size > _MAX_MOOV_BYTES or moov_payload is not None:
                        raise AssemblyPermanentAdapterError(
                            "CONTENT_ASSEMBLY_INVALID_MOOV"
                        )
                    handle.seek(payload_offset)
                    moov_payload = handle.read(payload_size)
                    if len(moov_payload) != payload_size:
                        raise AssemblyPermanentAdapterError(
                            "CONTENT_ASSEMBLY_TRUNCATED_MP4"
                        )
                elif kind == b"mdat":
                    if payload_size < 1:
                        raise AssemblyPermanentAdapterError(
                            "CONTENT_ASSEMBLY_EMPTY_MEDIA_DATA"
                        )
                    mdat_count += 1
                offset = box_end
    except OSError as exc:
        raise AssemblyPermanentAdapterError("CONTENT_ASSEMBLY_MP4_UNREADABLE") from exc
    if ftyp_count != 1 or mdat_count < 1 or moov_payload is None:
        raise AssemblyPermanentAdapterError("CONTENT_ASSEMBLY_REQUIRED_MP4_BOX_MISSING")
    duration, width, height = _inspect_moov(moov_payload)
    return _Mp4Inspection(
        duration_seconds=duration,
        width=width,
        height=height,
        size_bytes=size_bytes,
        sha256=_sha256_file(path),
    )


def _stream_box_header(
    handle: BinaryIO,
    *,
    offset: int,
    file_size: int,
) -> tuple[bytes, int, int]:
    handle.seek(offset)
    header = handle.read(8)
    if len(header) != 8:
        raise AssemblyPermanentAdapterError("CONTENT_ASSEMBLY_TRUNCATED_MP4")
    size32, kind = struct.unpack(">I4s", header)
    header_size = 8
    if size32 == 1:
        extended = handle.read(8)
        if len(extended) != 8:
            raise AssemblyPermanentAdapterError("CONTENT_ASSEMBLY_TRUNCATED_MP4")
        box_size = struct.unpack(">Q", extended)[0]
        header_size = 16
    elif size32 == 0:
        box_size = file_size - offset
    else:
        box_size = size32
    if box_size < header_size or offset + box_size > file_size:
        raise AssemblyPermanentAdapterError("CONTENT_ASSEMBLY_INVALID_MP4_BOX")
    return kind, offset + header_size, offset + box_size


def _memory_boxes(payload: bytes) -> tuple[_MemoryBox, ...]:
    boxes: list[_MemoryBox] = []
    offset = 0
    while offset < len(payload):
        if offset + 8 > len(payload):
            raise AssemblyPermanentAdapterError("CONTENT_ASSEMBLY_TRUNCATED_MOOV")
        size32, kind = struct.unpack(">I4s", payload[offset : offset + 8])
        header_size = 8
        if size32 == 1:
            if offset + 16 > len(payload):
                raise AssemblyPermanentAdapterError("CONTENT_ASSEMBLY_TRUNCATED_MOOV")
            box_size = struct.unpack(">Q", payload[offset + 8 : offset + 16])[0]
            header_size = 16
        elif size32 == 0:
            box_size = len(payload) - offset
        else:
            box_size = size32
        if box_size < header_size or offset + box_size > len(payload):
            raise AssemblyPermanentAdapterError("CONTENT_ASSEMBLY_INVALID_MOOV_BOX")
        boxes.append(
            _MemoryBox(
                kind=kind,
                payload=payload[offset + header_size : offset + box_size],
            )
        )
        offset += box_size
    return tuple(boxes)


def _inspect_moov(payload: bytes) -> tuple[Decimal, int, int]:
    boxes = _memory_boxes(payload)
    movie_headers = [box for box in boxes if box.kind == b"mvhd"]
    tracks = [box for box in boxes if box.kind == b"trak"]
    if len(movie_headers) != 1 or not tracks:
        raise AssemblyPermanentAdapterError("CONTENT_ASSEMBLY_INVALID_MOVIE_METADATA")
    duration = _movie_duration(movie_headers[0].payload)
    video_dimensions: list[tuple[int, int]] = []
    audio_tracks = 0
    for track in tracks:
        track_boxes = _memory_boxes(track.payload)
        track_header = next((box for box in track_boxes if box.kind == b"tkhd"), None)
        media = next((box for box in track_boxes if box.kind == b"mdia"), None)
        if track_header is None or media is None:
            raise AssemblyPermanentAdapterError("CONTENT_ASSEMBLY_INVALID_TRACK")
        media_boxes = _memory_boxes(media.payload)
        handler = next((box for box in media_boxes if box.kind == b"hdlr"), None)
        if handler is None or len(handler.payload) < 12:
            raise AssemblyPermanentAdapterError("CONTENT_ASSEMBLY_INVALID_TRACK_HANDLER")
        handler_type = handler.payload[8:12]
        if handler_type == b"vide":
            video_dimensions.append(_track_dimensions(track_header.payload))
        elif handler_type == b"soun":
            audio_tracks += 1
    if len(video_dimensions) != 1 or audio_tracks < 1:
        raise AssemblyPermanentAdapterError("CONTENT_ASSEMBLY_REQUIRED_TRACK_MISSING")
    width, height = video_dimensions[0]
    if width < 1 or height < 1:
        raise AssemblyPermanentAdapterError("CONTENT_ASSEMBLY_INVALID_TRACK_DIMENSIONS")
    return duration, width, height


def _movie_duration(payload: bytes) -> Decimal:
    if len(payload) < 20:
        raise AssemblyPermanentAdapterError("CONTENT_ASSEMBLY_INVALID_MOVIE_HEADER")
    version = payload[0]
    if version == 0:
        if len(payload) < 20:
            raise AssemblyPermanentAdapterError("CONTENT_ASSEMBLY_INVALID_MOVIE_HEADER")
        timescale = struct.unpack(">I", payload[12:16])[0]
        duration = struct.unpack(">I", payload[16:20])[0]
    elif version == 1:
        if len(payload) < 32:
            raise AssemblyPermanentAdapterError("CONTENT_ASSEMBLY_INVALID_MOVIE_HEADER")
        timescale = struct.unpack(">I", payload[20:24])[0]
        duration = struct.unpack(">Q", payload[24:32])[0]
    else:
        raise AssemblyPermanentAdapterError("CONTENT_ASSEMBLY_UNSUPPORTED_MOVIE_HEADER")
    if timescale < 1 or duration < 1:
        raise AssemblyPermanentAdapterError("CONTENT_ASSEMBLY_INVALID_MOVIE_DURATION")
    return (Decimal(duration) / Decimal(timescale)).quantize(_QUANTUM)


def _track_dimensions(payload: bytes) -> tuple[int, int]:
    if len(payload) < 8:
        raise AssemblyPermanentAdapterError("CONTENT_ASSEMBLY_INVALID_TRACK_HEADER")
    width_fixed, height_fixed = struct.unpack(">II", payload[-8:])
    return width_fixed >> 16, height_fixed >> 16


def _verified_artifact_path(storage_root: Path, artifact: Artifact) -> Path:
    root = storage_root.resolve()
    path = (root / artifact.relative_path).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("assembly source artifact escapes storage root") from exc
    try:
        stat = path.stat()
    except OSError as exc:
        raise ValueError("assembly source artifact is unavailable") from exc
    if not path.is_file() or stat.st_size != artifact.size_bytes:
        raise ValueError("assembly source artifact size does not match metadata")
    if _sha256_file(path) != artifact.sha256:
        raise ValueError("assembly source artifact checksum does not match metadata")
    return path


def _wav_duration(path: Path) -> Decimal:
    try:
        with wave.open(str(path), "rb") as audio:
            if (
                audio.getnchannels() != 1
                or audio.getsampwidth() != 2
                or audio.getcomptype() != "NONE"
                or audio.getframerate() < 8_000
                or audio.getnframes() < 1
            ):
                raise ValueError("unsupported assembly narration WAV")
            duration = Decimal(audio.getnframes()) / Decimal(audio.getframerate())
    except (OSError, EOFError, wave.Error) as exc:
        raise ValueError("assembly narration WAV is invalid") from exc
    if duration > Decimal(6 * 60 * 60):
        raise ValueError("assembly narration WAV exceeds six hours")
    return duration.quantize(_QUANTUM)


def _run_format(run: Run) -> Literal["short", "long"]:
    value = (run.input_payload or {}).get("format")
    if value == "short":
        return "short"
    if value == "long":
        return "long"
    raise ValueError("assembly run format is invalid")


def _video_path(storage_root: Path, *, run_id: int) -> Path:
    root = storage_root.resolve()
    if root == Path(root.anchor):
        raise ValueError("storage root must not be a filesystem root")
    return root / "content-engine" / f"run-{run_id}" / "assembly" / "video.mp4"


def _output_path(storage_root: Path, output: dict[str, object]) -> Path:
    value = output.get("artifact_relative_path")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("assembly output artifact path is unavailable")
    path = (storage_root.resolve() / value).resolve()
    try:
        path.relative_to(storage_root.resolve())
    except ValueError as exc:
        raise ValueError("assembly output escapes storage root") from exc
    return path


def _metric_dimensions(output: dict[str, object]) -> dict[str, object]:
    return {
        "queue": QueueClass.CPU.value,
        "backend": _required_text(output, "backend", max_length=200),
        "provider": _required_text(output, "provider", max_length=200),
        "tool_id": _required_text(output, "tool_id", max_length=200),
        "license_id": _required_text(output, "license_id", max_length=200),
        "video_codec": _required_text(output, "video_codec", max_length=20),
        "audio_codec": _required_text(output, "audio_codec", max_length=20),
        "format": _required_text(output, "format", max_length=20),
    }


def _required_text(
    payload: dict[str, object],
    key: str,
    *,
    max_length: int,
) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise ValueError(f"assembly output {key} is invalid")
    return value


def _required_int(
    payload: dict[str, object],
    key: str,
    *,
    minimum: int,
) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"assembly output {key} is invalid")
    return value


def _decimal_output(output: dict[str, object], key: str) -> Decimal:
    value = output.get(key)
    if not isinstance(value, str):
        raise ValueError(f"assembly output {key} is unavailable")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"assembly output {key} is invalid") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"assembly output {key} is invalid")
    return parsed


def _decimal(value: Decimal, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, str)):
        raise AssemblyPermanentAdapterError(f"CONTENT_ASSEMBLY_INVALID_{field.upper()}")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise AssemblyPermanentAdapterError(
            f"CONTENT_ASSEMBLY_INVALID_{field.upper()}"
        ) from exc
    if not parsed.is_finite() or parsed <= 0:
        raise AssemblyPermanentAdapterError(f"CONTENT_ASSEMBLY_INVALID_{field.upper()}")
    return parsed


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _persisted_id(value: int | None, kind: str) -> int:
    if not isinstance(value, int):
        raise ValueError(f"{kind} must be persisted")
    return value


def _result(
    run: Run,
    step_run: StepRun,
    artifact_id: int | None,
    *,
    replayed: bool,
) -> AssemblyExecutionResult:
    return AssemblyExecutionResult(
        run_id=_persisted_id(run.id, "run"),
        step_run_id=_persisted_id(step_run.id, "step"),
        artifact_id=artifact_id,
        run_status=run.status,
        step_status=step_run.status,
        replayed=replayed,
    )
