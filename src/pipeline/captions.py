"""Approved, adapter-driven Content Engine word-level caption step."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time
import unicodedata
import wave
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from pathlib import Path
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from src.operations.retention_policy import resolve_retention_days
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

CAPTION_STEP_NAME = "generate-captions-a4"
_SOURCE = "content-engine:a4"
_RETENTION_DAYS = resolve_retention_days("caption_ass")
_MAX_WORDS = 10_000
_MAX_WORD_LENGTH = 100
_MAX_AUDIO_SECONDS = Decimal(6 * 60 * 60)
_TIMING_TOLERANCE_SECONDS = Decimal("0.25")
_MIN_TRANSCRIPT_MATCH = Decimal("0.85")
_MATCH_QUANTUM = Decimal("0.0000000001")
_TOKEN_PATTERN = re.compile(r"[a-z0-9]+", re.ASCII)


class CaptionRetryableAdapterError(RuntimeError):
    """Raised when caption transcription can be retried unchanged."""


class CaptionPermanentAdapterError(RuntimeError):
    """Raised when caption configuration, rights or output is invalid."""


@dataclass(frozen=True, slots=True)
class CaptionWord:
    """One transcribed word with an absolute interval in the A2 audio."""

    text: str
    start_seconds: Decimal
    end_seconds: Decimal


@dataclass(frozen=True, slots=True)
class CaptionAdapterResult:
    """Word timings and model provenance returned by a caption adapter."""

    backend: str
    provider: str
    model_id: str
    language_code: str
    license_id: str
    commercial_use: bool
    words: tuple[CaptionWord, ...]


CaptionAdapter = Callable[[Path, str], CaptionAdapterResult]


@dataclass(frozen=True, slots=True)
class CaptionExecutionResult:
    """Observable identifiers produced by the A4 step."""

    run_id: int
    step_run_id: int | None
    artifact_id: int | None
    run_status: str
    step_status: str | None
    replayed: bool


@dataclass(frozen=True, slots=True)
class _AudioInspection:
    duration_seconds: Decimal


@dataclass(frozen=True, slots=True)
class _CaptionBundle:
    result: CaptionAdapterResult
    sha256: str
    word_count: int
    transcript_match: Decimal
    inference_seconds: Decimal


def get_configured_caption_adapter(
    backend: str,
    *,
    narration: str | None = None,
) -> CaptionAdapter:
    """Return the selected caption adapter or fail closed."""
    normalized = backend.strip().casefold()
    if normalized == "approved_text_timing_quality_test":
        if narration is None or not narration.strip():
            raise CaptionPermanentAdapterError("CONTENT_CAPTIONS_NARRATION_REQUIRED")
        return build_approved_text_timing_adapter(narration)

    def unavailable(_audio_path: Path, _language_code: str) -> CaptionAdapterResult:
        code = (
            "CONTENT_CAPTIONS_DISABLED"
            if normalized == "disabled"
            else "CONTENT_CAPTIONS_BACKEND_UNREVIEWED"
        )
        raise CaptionPermanentAdapterError(code)

    return unavailable


def build_approved_text_timing_adapter(narration: str) -> CaptionAdapter:
    """Estimate deterministic word intervals from approved text and WAV duration."""
    approved_words = tuple(narration.strip().split())
    if not approved_words or len(approved_words) > _MAX_WORDS:
        raise CaptionPermanentAdapterError("CONTENT_CAPTIONS_INVALID_WORD_COUNT")
    if any(
        len(word) > _MAX_WORD_LENGTH or any(character in word for character in "\r\n{}\\")
        for word in approved_words
    ):
        raise CaptionPermanentAdapterError("CONTENT_CAPTIONS_INVALID_WORD")
    weights = tuple(max(1, len(_tokens(word)[0]) if _tokens(word) else 1) for word in approved_words)
    total_weight = sum(weights)

    def align(audio_path: Path, language_code: str) -> CaptionAdapterResult:
        if language_code.strip().casefold() != "pt":
            raise CaptionPermanentAdapterError("CONTENT_CAPTIONS_LANGUAGE_MISMATCH")
        duration = _adapter_wav_duration(audio_path)
        elapsed_weight = 0
        words: list[CaptionWord] = []
        for text, weight in zip(approved_words, weights, strict=True):
            start = duration * Decimal(elapsed_weight) / Decimal(total_weight)
            elapsed_weight += weight
            end = duration * Decimal(elapsed_weight) / Decimal(total_weight)
            words.append(
                CaptionWord(
                    text=text,
                    start_seconds=start.quantize(_MATCH_QUANTUM),
                    end_seconds=end.quantize(_MATCH_QUANTUM),
                )
            )
        return CaptionAdapterResult(
            backend="approved-text-timing-quality-test",
            provider="automation-foundry",
            model_id="deterministic-proportional-v1",
            language_code="pt",
            license_id="PROJECT-INTERNAL",
            commercial_use=True,
            words=tuple(words),
        )

    return align


def _adapter_wav_duration(audio_path: Path) -> Decimal:
    try:
        with wave.open(str(audio_path), "rb") as audio:
            if (
                audio.getnchannels() != 1
                or audio.getsampwidth() != 2
                or audio.getcomptype() != "NONE"
                or audio.getframerate() < 8_000
                or audio.getnframes() < 1
            ):
                raise CaptionPermanentAdapterError("CONTENT_CAPTIONS_AUDIO_INVALID")
            duration = Decimal(audio.getnframes()) / Decimal(audio.getframerate())
    except CaptionPermanentAdapterError:
        raise
    except (OSError, EOFError, wave.Error) as exc:
        raise CaptionPermanentAdapterError("CONTENT_CAPTIONS_AUDIO_INVALID") from exc
    if duration > _MAX_AUDIO_SECONDS:
        raise CaptionPermanentAdapterError("CONTENT_CAPTIONS_AUDIO_INVALID")
    return duration.quantize(_MATCH_QUANTUM)


async def execute_approved_caption_step(
    session: AsyncSession,
    *,
    run: Run,
    approval: Approval,
    approval_input_payload: dict[str, object],
    audio_artifact: Artifact,
    narration: str,
    idempotency_key: str,
    storage_root: Path,
    adapter: CaptionAdapter,
    language_code: str,
    gpu_power_watts: int,
    timeout_seconds: float,
    queue: QueueClass = QueueClass.GPU,
    visual_manifest_artifact_id: int | None = None,
    visual_artifact_ids: tuple[int, ...] = (),
    finalize_run: bool = True,
    sleep: Sleeper = asyncio.sleep,
) -> CaptionExecutionResult:
    """Generate and register one ASS file from the exact approved A2 WAV."""
    narration = narration.strip()
    language_code = language_code.strip().casefold()
    if not narration:
        raise ValueError("caption narration must not be empty")
    if language_code != "pt":
        raise ValueError("caption language must be 'pt'")
    if gpu_power_watts < 1:
        raise ValueError("gpu_power_watts must be positive")
    if queue not in {QueueClass.CPU, QueueClass.GPU}:
        raise ValueError("caption queue must be cpu or gpu")
    script_artifact_id = _validate_approval_and_audio(
        run=run,
        approval=approval,
        approval_input_payload=approval_input_payload,
        audio_artifact=audio_artifact,
        narration=narration,
    )
    await _validate_visual_evidence(
        session,
        run_id=_persisted_id(run.id, "run"),
        manifest_artifact_id=visual_manifest_artifact_id,
        visual_artifact_ids=visual_artifact_ids,
    )
    audio_path, audio = _verified_audio(storage_root, audio_artifact)
    output_path = _caption_path(storage_root, run_id=_persisted_id(run.id, "run"))
    video_format = _run_format(run)
    step_key = f"{idempotency_key}:{CAPTION_STEP_NAME}"

    async def transcribe_operation(_context: TaskAttemptContext) -> dict[str, object]:
        try:
            bundle = await asyncio.to_thread(
                _generate_caption_atomic,
                adapter,
                audio_path,
                output_path,
                narration,
                language_code,
                audio.duration_seconds,
                video_format,
            )
        except CaptionRetryableAdapterError as exc:
            raise RetryableTaskError("CONTENT_CAPTIONS_BACKEND_UNAVAILABLE") from exc
        except CaptionPermanentAdapterError as exc:
            raise PermanentTaskError(str(exc) or "CONTENT_CAPTIONS_INVALID_OUTPUT") from exc
        return {
            "artifact_relative_path": output_path.relative_to(
                storage_root.resolve()
            ).as_posix(),
            "sha256": bundle.sha256,
            "backend": bundle.result.backend,
            "provider": bundle.result.provider,
            "model_id": bundle.result.model_id,
            "language_code": bundle.result.language_code,
            "license_id": bundle.result.license_id,
            "commercial_use": bundle.result.commercial_use,
            "word_count": bundle.word_count,
            "transcript_match": str(bundle.transcript_match),
            "audio_duration_seconds": str(audio.duration_seconds),
            "inference_seconds": str(bundle.inference_seconds),
            "format": video_format,
        }

    task_result = await execute_task_step(
        session,
        run=run,
        spec=TaskStepSpec(
            name=CAPTION_STEP_NAME,
            queue=queue,
            ordinal=4,
            idempotency_key=step_key,
            input_payload={
                **approval_input_payload,
                "audio_artifact_id": audio_artifact.id,
                "audio_artifact_sha256": audio_artifact.sha256,
                "visual_manifest_artifact_id": visual_manifest_artifact_id,
                "visual_artifact_ids": list(visual_artifact_ids),
                "language_code": language_code,
                "output_format": "ass-word-level",
            },
        ),
        policy=RetryPolicy(max_attempts=2, timeout_seconds=timeout_seconds),
        operation=transcribe_operation,
        sleep=sleep,
    )
    step_run = await session.get(StepRun, task_result.step_run_ids[-1])
    if step_run is None:
        raise ValueError("caption step result was not persisted")
    if task_result.status is RunStatus.CANCELLED:
        return _result(run, step_run, None, replayed=task_result.replayed)
    if task_result.status is RunStatus.FAILED:
        if task_result.error is None:
            raise ValueError("failed caption step is missing structured error")
        if RunStatus(run.status) is RunStatus.RUNNING:
            await transition_run(
                session,
                run,
                RunStatus.FAILED,
                error=task_result.error,
                note="Content Engine A4 captions failed",
            )
        return _result(run, step_run, None, replayed=task_result.replayed)

    try:
        async with session.begin_nested():
            automation = await session.get(Automation, run.automation_id)
            if automation is None:
                raise ValueError("caption automation does not exist")
            output = task_result.output_payload or {}
            artifact = (
                await register_local_artifact(
                    session,
                    run=run,
                    step_run=step_run,
                    storage_root=storage_root,
                    file_path=_output_path(storage_root, output),
                    idempotency_key=f"{idempotency_key}:caption-ass",
                    artifact_type="caption_ass",
                    media_type="text/x-ssa",
                    origin=_SOURCE,
                    sensitivity=ArtifactSensitivity.INTERNAL,
                    retention_days=_RETENTION_DAYS,
                    expected_sha256=_required_text(output, "sha256", max_length=64),
                )
            ).artifact
            dimensions = _metric_dimensions(output)
            word_metric = (
                await record_metric_point(
                    session,
                    automation=automation,
                    run=run,
                    step_run=step_run,
                    idempotency_key=f"{idempotency_key}:caption-word-count",
                    name="content_caption_timed_word_count",
                    kind=MetricKind.COUNTER,
                    value=_required_int(output, "word_count", minimum=1),
                    unit="words",
                    source=_SOURCE,
                    dimensions=dimensions,
                )
            ).metric_point
            match_metric = (
                await record_metric_point(
                    session,
                    automation=automation,
                    run=run,
                    step_run=step_run,
                    idempotency_key=f"{idempotency_key}:caption-transcript-match",
                    name="content_caption_transcript_match_ratio",
                    kind=MetricKind.GAUGE,
                    value=_decimal_output(output, "transcript_match"),
                    unit="ratio",
                    source=_SOURCE,
                    dimensions=dimensions,
                )
            ).metric_point
            energy_metric_id: int | None = None
            if queue is QueueClass.GPU:
                inference_seconds = _decimal_output(output, "inference_seconds")
                energy = (inference_seconds / Decimal("3600")) * (
                    Decimal(gpu_power_watts) / Decimal("1000")
                )
                energy_metric = (
                    await record_metric_point(
                        session,
                        automation=automation,
                        run=run,
                        step_run=step_run,
                        idempotency_key=f"{idempotency_key}:caption-energy-estimate",
                        name="content_caption_energy_estimate_kwh",
                        kind=MetricKind.GAUGE,
                        value=energy.quantize(_MATCH_QUANTUM),
                        unit="kWh",
                        source=_SOURCE,
                        confidence="0.5",
                        dimensions=dimensions,
                    )
                ).metric_point
                energy_metric_id = _persisted_id(energy_metric.id, "metric")
            ledger = (
                await record_ledger_entry(
                    session,
                    automation=automation,
                    run=run,
                    step_run=step_run,
                    idempotency_key=f"{idempotency_key}:caption-external-api-cost",
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
            "code": "CONTENT_CAPTIONS_EVIDENCE_PERSIST_FAILED",
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
                note="Content Engine A4 evidence persistence failed",
            )
        return _result(run, step_run, None, replayed=task_result.replayed)

    if finalize_run and RunStatus(run.status) is RunStatus.RUNNING:
        output_payload: dict[str, object] = {
            "script_artifact_id": script_artifact_id,
            "script_approval_id": approval.id,
            "tts_step_run_id": audio_artifact.step_run_id,
            "tts_artifact_id": audio_artifact.id,
            "caption_step_run_id": step_run.id,
            "caption_artifact_id": artifact.id,
            "caption_word_count_metric_id": word_metric.id,
            "caption_match_metric_id": match_metric.id,
            "caption_energy_metric_id": energy_metric_id,
            "caption_ledger_entry_id": ledger.id,
        }
        if visual_manifest_artifact_id is not None:
            output_payload["visual_manifest_artifact_id"] = visual_manifest_artifact_id
            output_payload["visual_artifact_ids"] = list(visual_artifact_ids)
        await transition_run(
            session,
            run,
            RunStatus.SUCCEEDED,
            output_payload=output_payload,
            note="Content Engine A4 completed without external publication",
        )
    return _result(
        run,
        step_run,
        _persisted_id(artifact.id, "artifact"),
        replayed=task_result.replayed,
    )


def _validate_approval_and_audio(
    *,
    run: Run,
    approval: Approval,
    approval_input_payload: dict[str, object],
    audio_artifact: Artifact,
    narration: str,
) -> int:
    run_id = _persisted_id(run.id, "run")
    if approval.run_id != run_id or audio_artifact.run_id != run_id:
        raise ValueError("caption approval and audio must belong to the run")
    if ApprovalStatus(approval.status) is not ApprovalStatus.APPROVED:
        raise ValueError("caption script approval must be approved")
    if approval.payload_digest != approval_payload_digest(approval_input_payload):
        raise ValueError("caption approval payload does not match durable approval")
    narration_digest = hashlib.sha256(narration.encode("utf-8")).hexdigest()
    if approval_input_payload.get("narration_sha256") != narration_digest:
        raise ValueError("caption narration does not match the approved script")
    script_artifact_id = approval_input_payload.get("artifact_id")
    script_artifact_sha256 = approval_input_payload.get("artifact_sha256")
    if isinstance(script_artifact_id, bool) or not isinstance(script_artifact_id, int):
        raise ValueError("caption approval does not identify a script artifact")
    if (
        not isinstance(script_artifact_sha256, str)
        or len(script_artifact_sha256) != 64
        or any(character not in "0123456789abcdef" for character in script_artifact_sha256)
    ):
        raise ValueError("caption approval has an invalid script checksum")
    if (
        audio_artifact.artifact_type != "narration_audio"
        or audio_artifact.media_type != "audio/wav"
        or not isinstance(audio_artifact.step_run_id, int)
    ):
        raise ValueError("caption source must be a persisted narration WAV")
    return script_artifact_id


async def _validate_visual_evidence(
    session: AsyncSession,
    *,
    run_id: int,
    manifest_artifact_id: int | None,
    visual_artifact_ids: tuple[int, ...],
) -> None:
    if manifest_artifact_id is None:
        if visual_artifact_ids:
            raise ValueError("caption visual assets require a manifest")
        return
    if not visual_artifact_ids or len(set(visual_artifact_ids)) != len(
        visual_artifact_ids
    ):
        raise ValueError("caption visual evidence is incomplete")
    manifest = await session.get(Artifact, manifest_artifact_id)
    if (
        manifest is None
        or manifest.run_id != run_id
        or manifest.artifact_type != "visual_manifest"
    ):
        raise ValueError("caption visual manifest is invalid")
    for artifact_id in visual_artifact_ids:
        artifact = await session.get(Artifact, artifact_id)
        if (
            artifact is None
            or artifact.run_id != run_id
            or artifact.artifact_type != "visual_image"
        ):
            raise ValueError("caption visual asset is invalid")


def _verified_audio(
    storage_root: Path,
    artifact: Artifact,
) -> tuple[Path, _AudioInspection]:
    root = storage_root.resolve()
    path = (root / artifact.relative_path).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("caption audio escapes storage root") from exc
    try:
        stat = path.stat()
    except OSError as exc:
        raise ValueError("caption audio is unavailable") from exc
    if not path.is_file() or stat.st_size != artifact.size_bytes:
        raise ValueError("caption audio size does not match durable metadata")
    if _sha256_file(path) != artifact.sha256:
        raise ValueError("caption audio checksum does not match durable metadata")
    try:
        with wave.open(str(path), "rb") as audio:
            if (
                audio.getnchannels() != 1
                or audio.getsampwidth() != 2
                or audio.getcomptype() != "NONE"
                or audio.getframerate() < 8_000
                or audio.getnframes() < 1
            ):
                raise ValueError("unsupported caption narration WAV")
            duration = Decimal(audio.getnframes()) / Decimal(audio.getframerate())
    except (OSError, EOFError, wave.Error) as exc:
        raise ValueError("caption narration WAV is invalid") from exc
    if duration > _MAX_AUDIO_SECONDS:
        raise ValueError("caption narration WAV exceeds six hours")
    return path, _AudioInspection(duration.quantize(_MATCH_QUANTUM))


def _generate_caption_atomic(
    adapter: CaptionAdapter,
    audio_path: Path,
    destination: Path,
    narration: str,
    language_code: str,
    audio_duration: Decimal,
    video_format: str,
) -> _CaptionBundle:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.stem}.{uuid4().hex}.tmp.ass")
    started = time.perf_counter()
    try:
        result = adapter(audio_path, language_code)
        match = _validate_adapter_result(
            result,
            narration=narration,
            expected_language=language_code,
            audio_duration=audio_duration,
        )
        rendered = _render_ass(result.words, video_format=video_format)
        temporary.write_text(rendered, encoding="utf-8", newline="\n")
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        sha256 = _sha256_file(temporary)
        os.replace(temporary, destination)
        return _CaptionBundle(
            result=result,
            sha256=sha256,
            word_count=len(result.words),
            transcript_match=match,
            inference_seconds=Decimal(
                str(round(time.perf_counter() - started, 6))
            ).quantize(_MATCH_QUANTUM),
        )
    except (CaptionRetryableAdapterError, CaptionPermanentAdapterError):
        raise
    except Exception as exc:
        raise CaptionPermanentAdapterError("CONTENT_CAPTIONS_INVALID_OUTPUT") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _validate_adapter_result(
    result: CaptionAdapterResult,
    *,
    narration: str,
    expected_language: str,
    audio_duration: Decimal,
) -> Decimal:
    if not isinstance(result, CaptionAdapterResult):
        raise CaptionPermanentAdapterError("CONTENT_CAPTIONS_INVALID_RESULT")
    for value in (
        result.backend,
        result.provider,
        result.model_id,
        result.language_code,
        result.license_id,
    ):
        if not isinstance(value, str) or not value.strip() or len(value) > 200:
            raise CaptionPermanentAdapterError("CONTENT_CAPTIONS_INVALID_PROVENANCE")
    if result.language_code.strip().casefold() != expected_language:
        raise CaptionPermanentAdapterError("CONTENT_CAPTIONS_LANGUAGE_MISMATCH")
    if result.commercial_use is not True:
        raise CaptionPermanentAdapterError("CONTENT_CAPTIONS_COMMERCIAL_RIGHTS_REQUIRED")
    if not isinstance(result.words, tuple) or not 1 <= len(result.words) <= _MAX_WORDS:
        raise CaptionPermanentAdapterError("CONTENT_CAPTIONS_INVALID_WORD_COUNT")

    prior_end = Decimal("0")
    transcript_words: list[str] = []
    for word in result.words:
        if not isinstance(word, CaptionWord):
            raise CaptionPermanentAdapterError("CONTENT_CAPTIONS_INVALID_WORD")
        text = word.text.strip()
        start = _timing_decimal(word.start_seconds)
        end = _timing_decimal(word.end_seconds)
        if (
            not text
            or len(text) > _MAX_WORD_LENGTH
            or any(character in text for character in "\r\n{}\\")
            or any(ord(character) < 32 for character in text)
        ):
            raise CaptionPermanentAdapterError("CONTENT_CAPTIONS_INVALID_WORD")
        if start < prior_end or end <= start:
            raise CaptionPermanentAdapterError("CONTENT_CAPTIONS_INVALID_TIMING")
        if end > audio_duration + _TIMING_TOLERANCE_SECONDS:
            raise CaptionPermanentAdapterError("CONTENT_CAPTIONS_TIMING_EXCEEDS_AUDIO")
        prior_end = end
        transcript_words.append(text)

    expected_tokens = _tokens(narration)
    actual_tokens = _tokens(" ".join(transcript_words))
    if not expected_tokens or not actual_tokens:
        raise CaptionPermanentAdapterError("CONTENT_CAPTIONS_EMPTY_TRANSCRIPT")
    match = Decimal(
        str(SequenceMatcher(None, expected_tokens, actual_tokens).ratio())
    ).quantize(_MATCH_QUANTUM)
    if match < _MIN_TRANSCRIPT_MATCH:
        raise CaptionPermanentAdapterError("CONTENT_CAPTIONS_TRANSCRIPT_MISMATCH")
    return match


def _render_ass(words: tuple[CaptionWord, ...], *, video_format: str) -> str:
    if video_format == "short":
        width, height, font_size, margin_v = 1080, 1920, 72, 180
    else:
        width, height, font_size, margin_v = 1920, 1080, 52, 90
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding",
        "Style: Default,Arial,"
        f"{font_size},&H00FFFFFF,&H0000D7FF,&H00101010,&H80000000,-1,0,0,0,"
        f"100,100,0,0,1,4,1,2,80,80,{margin_v},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for offset in range(0, len(words), 6):
        group = words[offset : offset + 6]
        start = _timing_decimal(group[0].start_seconds)
        end = _timing_decimal(group[-1].end_seconds)
        karaoke: list[str] = []
        for index, word in enumerate(group):
            word_start = _timing_decimal(word.start_seconds)
            next_start = (
                _timing_decimal(group[index + 1].start_seconds)
                if index + 1 < len(group)
                else end
            )
            centiseconds = max(1, int((next_start - word_start) * Decimal("100")))
            karaoke.append(f"{{\\kf{centiseconds}}}{word.text.strip()}")
        lines.append(
            "Dialogue: 0,"
            f"{_ass_time(start)},{_ass_time(end)},Default,,0,0,0,,"
            + " ".join(karaoke)
        )
    return "\n".join(lines) + "\n"


def _tokens(value: str) -> list[str]:
    folded = unicodedata.normalize("NFKD", value.casefold()).encode(
        "ascii", "ignore"
    ).decode("ascii")
    return _TOKEN_PATTERN.findall(folded)


def _timing_decimal(value: Decimal) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, str)):
        raise CaptionPermanentAdapterError("CONTENT_CAPTIONS_INVALID_TIMING")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise CaptionPermanentAdapterError("CONTENT_CAPTIONS_INVALID_TIMING") from exc
    if not parsed.is_finite() or parsed < 0:
        raise CaptionPermanentAdapterError("CONTENT_CAPTIONS_INVALID_TIMING")
    return parsed


def _ass_time(value: Decimal) -> str:
    centiseconds = int(value * Decimal("100"))
    hours, remainder = divmod(centiseconds, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    seconds, fraction = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{fraction:02d}"


def _run_format(run: Run) -> str:
    payload = run.input_payload or {}
    value = payload.get("format")
    if value not in {"short", "long"}:
        raise ValueError("caption run format is invalid")
    return str(value)


def _caption_path(storage_root: Path, *, run_id: int) -> Path:
    root = storage_root.resolve()
    if root == Path(root.anchor):
        raise ValueError("storage root must not be a filesystem root")
    return root / "content-engine" / f"run-{run_id}" / "captions" / "captions.ass"


def _output_path(storage_root: Path, output: dict[str, object]) -> Path:
    value = output.get("artifact_relative_path")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("caption output artifact path is unavailable")
    path = (storage_root.resolve() / value).resolve()
    try:
        path.relative_to(storage_root.resolve())
    except ValueError as exc:
        raise ValueError("caption output escapes storage root") from exc
    return path


def _metric_dimensions(output: dict[str, object]) -> dict[str, object]:
    return {
        "backend": _required_text(output, "backend", max_length=200),
        "provider": _required_text(output, "provider", max_length=200),
        "model_id": _required_text(output, "model_id", max_length=200),
        "language_code": _required_text(output, "language_code", max_length=20),
        "license_id": _required_text(output, "license_id", max_length=200),
    }


def _required_text(
    payload: dict[str, object],
    key: str,
    *,
    max_length: int,
) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise ValueError(f"caption output {key} is invalid")
    return value


def _required_int(
    payload: dict[str, object],
    key: str,
    *,
    minimum: int,
) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"caption output {key} is invalid")
    return value


def _decimal_output(output: dict[str, object], key: str) -> Decimal:
    value = output.get(key)
    if not isinstance(value, str):
        raise ValueError(f"caption output {key} is unavailable")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"caption output {key} is invalid") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"caption output {key} is invalid")
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
) -> CaptionExecutionResult:
    return CaptionExecutionResult(
        run_id=_persisted_id(run.id, "run"),
        step_run_id=_persisted_id(step_run.id, "step"),
        artifact_id=artifact_id,
        run_status=run.status,
        step_status=step_run.status,
        replayed=replayed,
    )
