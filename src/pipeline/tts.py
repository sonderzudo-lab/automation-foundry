"""Approved, adapter-driven Content Engine TTS step with verified WAV evidence."""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.metadata
import os
import shutil
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from src.platform.artifact_service import register_local_artifact
from src.platform.ledger_service import record_ledger_entry
from src.platform.metric_service import record_metric_point
from src.platform.models import (
    Approval,
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

TTS_STEP_NAME = "synthesize-tts-a2"
KOKORO_QUALITY_TEST_BACKEND = "kokoro_quality_test"
KOKORO_PACKAGE_VERSION = "0.9.4"
KOKORO_MODEL_REPOSITORY = "hexgrad/Kokoro-82M"
KOKORO_MODEL_REVISION = "f3ff3571791e39611d31c381e3a41a3af07b4987"
KOKORO_MODEL_SHA256 = (
    "496dba118d1a58f5f3db2efc88dbdc216e0483fc89fe6e47ee1f2c53f18ad1e4"
)
KOKORO_PT_BR_VOICE_SHA256_PREFIXES = {
    "pf_dora": "07e4ff98",
    "pm_alex": "cf0ba8c5",
    "pm_santa": "d4210316",
}
KOKORO_SAMPLE_RATE_HZ = 24_000
_SOURCE = "content-engine:a2"
_RETENTION_DAYS = 90
_MAX_AUDIO_BYTES = 1024 * 1024 * 1024
_MIN_SAMPLE_RATE = 8_000
_MAX_SAMPLE_RATE = 96_000


class TTSRetryableAdapterError(RuntimeError):
    """Raised when a configured local backend can be retried safely."""


class TTSPermanentAdapterError(RuntimeError):
    """Raised when adapter configuration or output cannot be retried."""


@dataclass(frozen=True, slots=True)
class TTSAdapterResult:
    """Provenance returned by an adapter after it writes one WAV file."""

    backend: str
    model_id: str
    voice_id: str
    language_code: str
    license_id: str


TTSSynthesizer = Callable[[str, Path, str, str], TTSAdapterResult]


@dataclass(frozen=True, slots=True)
class TTSExecutionResult:
    """Observable result of an approved A2 step."""

    run_id: int
    step_run_id: int | None
    artifact_id: int | None
    run_status: str
    step_status: str | None
    replayed: bool


def get_configured_tts_synthesizer(backend: str) -> TTSSynthesizer:
    """Return only explicitly reviewed runtime modes; unknown names fail closed."""
    normalized = backend.strip().casefold()
    if normalized == KOKORO_QUALITY_TEST_BACKEND:
        return synthesize_kokoro_quality_test

    def unavailable(
        _narration: str,
        _destination: Path,
        _voice_id: str,
        _language_code: str,
    ) -> TTSAdapterResult:
        code = (
            "CONTENT_TTS_DISABLED"
            if normalized == "disabled"
            else "CONTENT_TTS_BACKEND_UNREVIEWED"
        )
        raise TTSPermanentAdapterError(code)

    return unavailable


@dataclass(frozen=True, slots=True)
class _KokoroRuntime:
    pipeline: Any
    torch: Any
    numpy: Any
    hf_hub_download: Callable[..., str]


def synthesize_kokoro_quality_test(
    narration: str,
    destination: Path,
    voice_id: str,
    language_code: str,
) -> TTSAdapterResult:
    """Generate a pinned PT-BR sample for local human quality evaluation only."""
    if language_code != "p":
        raise TTSPermanentAdapterError("CONTENT_TTS_KOKORO_REQUIRES_PT_BR")
    if voice_id not in KOKORO_PT_BR_VOICE_SHA256_PREFIXES:
        raise TTSPermanentAdapterError("CONTENT_TTS_KOKORO_VOICE_NOT_REVIEWED")

    runtime = _load_kokoro_runtime()
    voice = _load_kokoro_voice(voice_id)
    chunks = _split_kokoro_text(narration)
    frame_count = 0
    try:
        with wave.open(str(destination), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(KOKORO_SAMPLE_RATE_HZ)
            for chunk in chunks:
                generated = runtime.pipeline(
                    [chunk],
                    voice=voice,
                    speed=1.0,
                    split_pattern=None,
                )
                for result in generated:
                    audio = getattr(result, "audio", None)
                    if audio is None:
                        raise TTSPermanentAdapterError(
                            "CONTENT_TTS_KOKORO_EMPTY_AUDIO"
                        )
                    frames = _kokoro_audio_to_pcm_s16le(audio, runtime.numpy)
                    if not frames:
                        raise TTSPermanentAdapterError(
                            "CONTENT_TTS_KOKORO_EMPTY_AUDIO"
                        )
                    output.writeframesraw(frames)
                    frame_count += len(frames) // 2
    except (TTSPermanentAdapterError, TTSRetryableAdapterError):
        raise
    except (OSError, RuntimeError) as exc:
        raise TTSRetryableAdapterError(
            "CONTENT_TTS_KOKORO_INFERENCE_FAILED"
        ) from exc
    if frame_count < 1:
        raise TTSPermanentAdapterError("CONTENT_TTS_KOKORO_EMPTY_AUDIO")

    return TTSAdapterResult(
        backend=KOKORO_QUALITY_TEST_BACKEND,
        model_id=(
            f"{KOKORO_MODEL_REPOSITORY}@{KOKORO_MODEL_REVISION}"
            f"#sha256:{KOKORO_MODEL_SHA256}"
        ),
        voice_id=voice_id,
        language_code=language_code,
        license_id=(
            "Apache-2.0; espeak-ng=GPL-3.0-or-later(external); "
            "pt-br-voice=provenance-unverified; scope=quality-test-only"
        ),
    )


@lru_cache(maxsize=1)
def _load_kokoro_runtime() -> _KokoroRuntime:
    if shutil.which("espeak-ng") is None:
        raise TTSPermanentAdapterError("CONTENT_TTS_ESPEAK_NG_REQUIRED")
    try:
        installed_version = importlib.metadata.version("kokoro")
    except importlib.metadata.PackageNotFoundError as exc:
        raise TTSPermanentAdapterError("CONTENT_TTS_KOKORO_NOT_INSTALLED") from exc
    if installed_version != KOKORO_PACKAGE_VERSION:
        raise TTSPermanentAdapterError("CONTENT_TTS_KOKORO_VERSION_MISMATCH")

    try:
        kokoro = importlib.import_module("kokoro")
        torch = importlib.import_module("torch")
        numpy = importlib.import_module("numpy")
        huggingface_hub = importlib.import_module("huggingface_hub")
    except ImportError as exc:
        raise TTSPermanentAdapterError("CONTENT_TTS_KOKORO_DEPENDENCY_MISSING") from exc
    if not bool(torch.cuda.is_available()):
        raise TTSPermanentAdapterError("CONTENT_TTS_KOKORO_CUDA_REQUIRED")

    try:
        config_path = huggingface_hub.hf_hub_download(
            repo_id=KOKORO_MODEL_REPOSITORY,
            filename="config.json",
            revision=KOKORO_MODEL_REVISION,
        )
        model_path = huggingface_hub.hf_hub_download(
            repo_id=KOKORO_MODEL_REPOSITORY,
            filename="kokoro-v1_0.pth",
            revision=KOKORO_MODEL_REVISION,
        )
    except Exception as exc:
        raise TTSRetryableAdapterError("CONTENT_TTS_KOKORO_MODEL_UNAVAILABLE") from exc
    if _sha256_file(Path(model_path)) != KOKORO_MODEL_SHA256:
        raise TTSPermanentAdapterError("CONTENT_TTS_KOKORO_MODEL_DIGEST_MISMATCH")

    try:
        model = kokoro.KModel(
            repo_id=KOKORO_MODEL_REPOSITORY,
            config=config_path,
            model=model_path,
        ).to("cuda").eval()
        pipeline = kokoro.KPipeline(
            lang_code="p",
            repo_id=KOKORO_MODEL_REPOSITORY,
            model=model,
            device="cuda",
        )
    except Exception as exc:
        raise TTSRetryableAdapterError(
            "CONTENT_TTS_KOKORO_INITIALIZATION_FAILED"
        ) from exc
    return _KokoroRuntime(
        pipeline=pipeline,
        torch=torch,
        numpy=numpy,
        hf_hub_download=huggingface_hub.hf_hub_download,
    )


@lru_cache(maxsize=len(KOKORO_PT_BR_VOICE_SHA256_PREFIXES))
def _load_kokoro_voice(voice_id: str) -> Any:
    expected_prefix = KOKORO_PT_BR_VOICE_SHA256_PREFIXES.get(voice_id)
    if expected_prefix is None:
        raise TTSPermanentAdapterError("CONTENT_TTS_KOKORO_VOICE_NOT_REVIEWED")
    runtime = _load_kokoro_runtime()
    try:
        voice_path = runtime.hf_hub_download(
            repo_id=KOKORO_MODEL_REPOSITORY,
            filename=f"voices/{voice_id}.pt",
            revision=KOKORO_MODEL_REVISION,
        )
    except Exception as exc:
        raise TTSRetryableAdapterError("CONTENT_TTS_KOKORO_VOICE_UNAVAILABLE") from exc
    if not _sha256_file(Path(voice_path)).startswith(expected_prefix):
        raise TTSPermanentAdapterError("CONTENT_TTS_KOKORO_VOICE_DIGEST_MISMATCH")
    try:
        return runtime.torch.load(voice_path, map_location="cpu", weights_only=True)
    except Exception as exc:
        raise TTSPermanentAdapterError("CONTENT_TTS_KOKORO_VOICE_INVALID") from exc


def _split_kokoro_text(text: str, *, max_chars: int = 400) -> tuple[str, ...]:
    """Bound non-English inference chunks without dropping approved narration."""
    normalized = " ".join(text.split())
    if not normalized:
        raise TTSPermanentAdapterError("CONTENT_TTS_KOKORO_EMPTY_TEXT")
    if max_chars < 40:
        raise ValueError("max_chars must be at least 40")

    chunks: list[str] = []
    remaining = normalized
    while remaining:
        if len(remaining) <= max_chars:
            chunks.append(remaining)
            break
        boundary = max(
            remaining.rfind(marker, 0, max_chars + 1)
            for marker in (". ", "! ", "? ", "; ", ": ", ", ", " ")
        )
        if boundary < max_chars // 2:
            boundary = max_chars
        elif remaining[boundary] in ".!?;:,":
            boundary += 1
        chunk = remaining[:boundary].strip()
        if not chunk:
            boundary = max_chars
            chunk = remaining[:boundary]
        chunks.append(chunk)
        remaining = remaining[boundary:].strip()
    return tuple(chunks)


def _kokoro_audio_to_pcm_s16le(audio: Any, numpy: Any) -> bytes:
    if hasattr(audio, "detach"):
        audio = audio.detach().cpu().numpy()
    samples = numpy.asarray(audio, dtype=numpy.float32).reshape(-1)
    if samples.size < 1 or not bool(numpy.isfinite(samples).all()):
        raise TTSPermanentAdapterError("CONTENT_TTS_KOKORO_INVALID_SAMPLES")
    clipped = numpy.clip(samples, -1.0, 1.0)
    return bytes(numpy.rint(clipped * 32767.0).astype("<i2").tobytes())


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


async def execute_approved_tts_step(
    session: AsyncSession,
    *,
    run: Run,
    approval: Approval,
    approval_input_payload: dict[str, object],
    source_artifact: Artifact,
    narration: str,
    idempotency_key: str,
    storage_root: Path,
    synthesizer: TTSSynthesizer,
    voice_id: str,
    language_code: str,
    gpu_power_watts: int,
    timeout_seconds: float,
    finalize_run: bool = True,
    sleep: Sleeper = asyncio.sleep,
) -> TTSExecutionResult:
    """Synthesize and register one WAV, authorized by the exact A1 approval."""
    narration = narration.strip()
    voice_id = voice_id.strip()
    language_code = language_code.strip()
    if not narration:
        raise ValueError("narration must not be empty")
    if not voice_id or len(voice_id) > 100:
        raise ValueError("voice_id must contain 1 to 100 characters")
    if not language_code or len(language_code) > 20:
        raise ValueError("language_code must contain 1 to 20 characters")
    if gpu_power_watts < 1:
        raise ValueError("gpu_power_watts must be positive")
    if source_artifact.run_id != run.id:
        raise ValueError("source artifact does not belong to the TTS run")
    narration_digest = approval_input_payload.get("narration_sha256")
    if (
        not isinstance(narration_digest, str)
        or narration_digest != hashlib.sha256(narration.encode("utf-8")).hexdigest()
    ):
        raise ValueError("narration does not match the approved A1 bundle")

    step_key = f"{idempotency_key}:{TTS_STEP_NAME}"
    audio_path = _audio_path(storage_root, run_id=_persisted_id(run.id, "run"))
    step_input = {
        **approval_input_payload,
        "voice_id": voice_id,
        "language_code": language_code,
        "output_format": "wav-pcm-s16le-mono",
    }

    async def synthesize_operation(_context: TaskAttemptContext) -> dict[str, object]:
        started = time.perf_counter()
        try:
            adapter_result, audio = await asyncio.to_thread(
                _synthesize_atomic,
                synthesizer,
                narration,
                audio_path,
                voice_id,
                language_code,
            )
        except TTSRetryableAdapterError as exc:
            raise RetryableTaskError("CONTENT_TTS_BACKEND_UNAVAILABLE") from exc
        except TTSPermanentAdapterError as exc:
            raise PermanentTaskError(str(exc) or "CONTENT_TTS_INVALID_OUTPUT") from exc
        elapsed = Decimal(str(round(time.perf_counter() - started, 6)))
        return {
            "artifact_relative_path": audio_path.relative_to(storage_root.resolve()).as_posix(),
            "backend": adapter_result.backend,
            "model_id": adapter_result.model_id,
            "voice_id": adapter_result.voice_id,
            "language_code": adapter_result.language_code,
            "license_id": adapter_result.license_id,
            "duration_seconds": str(audio.duration_seconds),
            "sample_rate_hz": audio.sample_rate_hz,
            "frame_count": audio.frame_count,
            "inference_seconds": str(elapsed),
        }

    task_result = await execute_task_step(
        session,
        run=run,
        spec=TaskStepSpec(
            name=TTS_STEP_NAME,
            queue=QueueClass.GPU,
            ordinal=2,
            idempotency_key=step_key,
            input_payload=step_input,
            required_approval_id=_persisted_id(approval.id, "approval"),
            required_approval_action=approval.action,
            approval_input_payload=approval_input_payload,
        ),
        policy=RetryPolicy(max_attempts=2, timeout_seconds=timeout_seconds),
        operation=synthesize_operation,
        sleep=sleep,
    )
    step_run = await session.get(StepRun, task_result.step_run_ids[-1])
    if step_run is None:
        raise ValueError("TTS step result was not persisted")
    if task_result.status is RunStatus.CANCELLED:
        return _result(run, step_run, None, replayed=task_result.replayed)
    if task_result.status is RunStatus.FAILED:
        if task_result.error is None:
            raise ValueError("failed TTS step is missing structured error")
        if RunStatus(run.status) is RunStatus.RUNNING:
            await transition_run(
                session,
                run,
                RunStatus.FAILED,
                error=task_result.error,
                note="Content Engine A2 TTS failed",
            )
        return _result(run, step_run, None, replayed=task_result.replayed)

    try:
        async with session.begin_nested():
            automation = await session.get(Automation, run.automation_id)
            if automation is None:
                raise ValueError("TTS automation does not exist")
            artifact = (
                await register_local_artifact(
                    session,
                    run=run,
                    step_run=step_run,
                    storage_root=storage_root,
                    file_path=audio_path,
                    idempotency_key=f"{idempotency_key}:tts-audio",
                    artifact_type="narration_audio",
                    media_type="audio/wav",
                    origin=_SOURCE,
                    sensitivity=ArtifactSensitivity.INTERNAL,
                    retention_days=_RETENTION_DAYS,
                )
            ).artifact
            output = task_result.output_payload or {}
            duration = _decimal_output(output, "duration_seconds")
            inference_seconds = _decimal_output(output, "inference_seconds")
            duration_metric = (
                await record_metric_point(
                    session,
                    automation=automation,
                    run=run,
                    step_run=step_run,
                    idempotency_key=f"{idempotency_key}:tts-audio-duration",
                    name="content_tts_audio_duration_seconds",
                    kind=MetricKind.DURATION,
                    value=duration,
                    unit="seconds",
                    source=_SOURCE,
                    dimensions=_metric_dimensions(output),
                )
            ).metric_point
            energy_kwh = (inference_seconds / Decimal("3600")) * (
                Decimal(gpu_power_watts) / Decimal("1000")
            )
            energy_metric = (
                await record_metric_point(
                    session,
                    automation=automation,
                    run=run,
                    step_run=step_run,
                    idempotency_key=f"{idempotency_key}:tts-energy-estimate",
                    name="content_tts_energy_estimate_kwh",
                    kind=MetricKind.GAUGE,
                    value=energy_kwh.quantize(Decimal("0.0000000001")),
                    unit="kWh",
                    source=_SOURCE,
                    confidence="0.5",
                    dimensions=_metric_dimensions(output),
                )
            ).metric_point
            ledger = (
                await record_ledger_entry(
                    session,
                    automation=automation,
                    run=run,
                    step_run=step_run,
                    idempotency_key=f"{idempotency_key}:tts-external-api-cost",
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
            "code": "CONTENT_TTS_EVIDENCE_PERSIST_FAILED",
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
                note="Content Engine A2 evidence persistence failed",
            )
        return _result(run, step_run, None, replayed=task_result.replayed)

    if finalize_run and RunStatus(run.status) is RunStatus.RUNNING:
        await transition_run(
            session,
            run,
            RunStatus.SUCCEEDED,
            output_payload={
                "script_artifact_id": source_artifact.id,
                "script_approval_id": approval.id,
                "tts_step_run_id": step_run.id,
                "tts_artifact_id": artifact.id,
                "tts_duration_metric_id": duration_metric.id,
                "tts_energy_metric_id": energy_metric.id,
                "tts_ledger_entry_id": ledger.id,
            },
            note="Content Engine A2 completed without external effects",
        )
    return _result(
        run,
        step_run,
        _persisted_id(artifact.id, "artifact"),
        replayed=task_result.replayed,
    )


@dataclass(frozen=True, slots=True)
class _WavInspection:
    sample_rate_hz: int
    frame_count: int
    duration_seconds: Decimal


def _synthesize_atomic(
    synthesizer: TTSSynthesizer,
    narration: str,
    destination: Path,
    voice_id: str,
    language_code: str,
) -> tuple[TTSAdapterResult, _WavInspection]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.stem}.{uuid4().hex}.tmp.wav")
    try:
        result = synthesizer(narration, temporary, voice_id, language_code)
        _validate_adapter_result(
            result,
            expected_voice=voice_id,
            expected_language=language_code,
        )
        inspection = _inspect_wav(temporary)
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        return result, inspection
    except (TTSRetryableAdapterError, TTSPermanentAdapterError):
        raise
    except Exception as exc:
        raise TTSPermanentAdapterError("CONTENT_TTS_INVALID_OUTPUT") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _validate_adapter_result(
    result: TTSAdapterResult,
    *,
    expected_voice: str,
    expected_language: str,
) -> None:
    if not isinstance(result, TTSAdapterResult):
        raise TTSPermanentAdapterError("CONTENT_TTS_INVALID_METADATA")
    values = (
        result.backend,
        result.model_id,
        result.voice_id,
        result.language_code,
        result.license_id,
    )
    if any(not value.strip() or len(value) > 200 for value in values):
        raise TTSPermanentAdapterError("CONTENT_TTS_INVALID_METADATA")
    if result.voice_id != expected_voice or result.language_code != expected_language:
        raise TTSPermanentAdapterError("CONTENT_TTS_METADATA_MISMATCH")


def _inspect_wav(path: Path) -> _WavInspection:
    size = path.stat().st_size
    if size <= 44 or size > _MAX_AUDIO_BYTES:
        raise TTSPermanentAdapterError("CONTENT_TTS_INVALID_WAV_SIZE")
    try:
        with wave.open(str(path), "rb") as audio:
            channels = audio.getnchannels()
            sample_width = audio.getsampwidth()
            sample_rate = audio.getframerate()
            frame_count = audio.getnframes()
            compression = audio.getcomptype()
            decoded_bytes = 0
            while frames := audio.readframes(min(65_536, frame_count)):
                decoded_bytes += len(frames)
    except (OSError, EOFError, wave.Error) as exc:
        raise TTSPermanentAdapterError("CONTENT_TTS_INVALID_WAV") from exc
    if channels != 1 or sample_width != 2 or compression != "NONE":
        raise TTSPermanentAdapterError("CONTENT_TTS_WAV_MUST_BE_PCM_S16LE_MONO")
    if not _MIN_SAMPLE_RATE <= sample_rate <= _MAX_SAMPLE_RATE or frame_count < 1:
        raise TTSPermanentAdapterError("CONTENT_TTS_INVALID_WAV_TIMING")
    if decoded_bytes != frame_count * channels * sample_width:
        raise TTSPermanentAdapterError("CONTENT_TTS_TRUNCATED_WAV")
    duration = (Decimal(frame_count) / Decimal(sample_rate)).quantize(
        Decimal("0.0000000001")
    )
    return _WavInspection(sample_rate, frame_count, duration)


def _audio_path(storage_root: Path, *, run_id: int) -> Path:
    root = storage_root.resolve()
    if root == Path(root.anchor):
        raise ValueError("storage root must not be a filesystem root")
    return root / "content-engine" / f"run-{run_id}" / "audio" / "narration.wav"


def _decimal_output(output: dict[str, object], key: str) -> Decimal:
    value = output.get(key)
    if not isinstance(value, str):
        raise ValueError(f"TTS output {key} is unavailable")
    parsed = Decimal(value)
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"TTS output {key} is invalid")
    return parsed


def _metric_dimensions(output: dict[str, object]) -> dict[str, object]:
    fields = ("backend", "model_id", "voice_id", "language_code", "license_id")
    dimensions: dict[str, object] = {}
    for field in fields:
        value = output.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"TTS output {field} is unavailable")
        dimensions[field] = value
    return dimensions


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
) -> TTSExecutionResult:
    return TTSExecutionResult(
        run_id=_persisted_id(run.id, "run"),
        step_run_id=_persisted_id(step_run.id, "step"),
        artifact_id=artifact_id,
        run_status=run.status,
        step_status=step_run.status,
        replayed=replayed,
    )
