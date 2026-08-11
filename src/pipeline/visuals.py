"""Approved, adapter-driven Content Engine visual step with licensed evidence."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import shutil
import struct
import time
import wave
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse
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

VISUAL_STEP_NAME = "prepare-visuals-a3"
_SOURCE = "content-engine:a3"
_RETENTION_DAYS = 90
_SECONDS_PER_VISUAL = Decimal("6")
_MAX_VISUALS = 60
_MAX_IMAGE_BYTES = 50 * 1024 * 1024
_MAX_IMAGE_PIXELS = 4096 * 4096
_MAX_SCRIPT_BUNDLE_BYTES = 5 * 1024 * 1024
_MAX_LOCAL_MANIFEST_BYTES = 1024 * 1024
_LOCAL_ASSET_FIELDS = {
    "relative_path",
    "sha256",
    "source_type",
    "provider",
    "model_id",
    "source_id",
    "source_uri",
    "license_id",
    "commercial_use",
    "attribution",
}


class VisualRetryableAdapterError(RuntimeError):
    """Raised when a visual backend can be retried without changing inputs."""


class VisualPermanentAdapterError(RuntimeError):
    """Raised when visual configuration, licensing or output is invalid."""


@dataclass(frozen=True, slots=True)
class VisualRequest:
    """One deterministic visual requested from approved editorial content."""

    index: int
    prompt: str
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class VisualAssetResult:
    """Per-file provenance returned by a visual adapter."""

    file_name: str
    source_type: Literal["generated", "stock", "owned"]
    provider: str
    model_id: str
    source_id: str
    source_uri: str
    license_id: str
    commercial_use: bool
    attribution: str


VisualAdapter = Callable[
    [tuple[VisualRequest, ...], Path],
    tuple[VisualAssetResult, ...],
]


@dataclass(frozen=True, slots=True)
class VisualExecutionResult:
    """Observable identifiers produced by the A3 step."""

    run_id: int
    step_run_id: int | None
    manifest_artifact_id: int | None
    visual_artifact_ids: tuple[int, ...]
    run_status: str
    step_status: str | None
    replayed: bool


@dataclass(frozen=True, slots=True)
class _VisualContext:
    script_artifact_id: int
    script_artifact_sha256: str
    audio_artifact_id: int
    audio_artifact_sha256: str
    audio_step_run_id: int
    duration_seconds: Decimal
    format: Literal["short", "long"]
    full_script: str
    narration: str


@dataclass(frozen=True, slots=True)
class _PngInspection:
    width: int
    height: int
    sha256: str
    size_bytes: int


def get_configured_visual_adapter(
    backend: str,
    *,
    import_root: Path | None = None,
    manifest_path: Path | None = None,
) -> VisualAdapter:
    """Return the explicitly configured visual adapter or fail closed."""
    normalized = backend.strip().casefold()
    if normalized == "local_assets_quality_test":
        if import_root is None or manifest_path is None:
            raise VisualPermanentAdapterError("CONTENT_VISUALS_IMPORT_CONFIG_REQUIRED")
        return build_local_asset_adapter(
            import_root=import_root,
            manifest_path=manifest_path,
        )

    def unavailable(
        _requests: tuple[VisualRequest, ...],
        _destination: Path,
    ) -> tuple[VisualAssetResult, ...]:
        code = (
            "CONTENT_VISUALS_DISABLED"
            if normalized == "disabled"
            else "CONTENT_VISUALS_BACKEND_UNREVIEWED"
        )
        raise VisualPermanentAdapterError(code)

    return unavailable


def build_local_asset_adapter(
    *,
    import_root: Path,
    manifest_path: Path,
) -> VisualAdapter:
    """Import operator-selected PNGs from a contained, hash-pinned manifest."""
    root = import_root.resolve()
    if root == Path(root.anchor):
        raise VisualPermanentAdapterError("CONTENT_VISUALS_IMPORT_ROOT_UNSAFE")
    manifest = manifest_path if manifest_path.is_absolute() else root / manifest_path
    manifest = manifest.resolve()
    try:
        manifest.relative_to(root)
    except ValueError as exc:
        raise VisualPermanentAdapterError("CONTENT_VISUALS_MANIFEST_OUTSIDE_IMPORT_ROOT") from exc

    def import_assets(
        requests: tuple[VisualRequest, ...],
        destination: Path,
    ) -> tuple[VisualAssetResult, ...]:
        entries = _load_local_asset_manifest(manifest, root=root, expected_count=len(requests))
        results: list[VisualAssetResult] = []
        for request, entry in zip(requests, entries, strict=True):
            source_path = _local_asset_source_path(root, entry)
            file_name = f"visual-{request.index:03d}.png"
            _copy_pinned_local_asset(
                source_path,
                destination / file_name,
                expected_sha256=_manifest_sha256(entry),
            )
            results.append(
                VisualAssetResult(
                    file_name=file_name,
                    source_type=_manifest_source_type(entry),
                    provider=_manifest_text(entry, "provider", max_length=200),
                    model_id=_manifest_text(entry, "model_id", max_length=200),
                    source_id=_manifest_text(entry, "source_id", max_length=500),
                    source_uri=_manifest_text(entry, "source_uri", max_length=1000),
                    license_id=_manifest_text(entry, "license_id", max_length=200),
                    commercial_use=True,
                    attribution=_manifest_text(entry, "attribution", max_length=1000),
                )
            )
        return tuple(results)

    return import_assets


def _load_local_asset_manifest(
    manifest_path: Path,
    *,
    root: Path,
    expected_count: int,
) -> tuple[dict[str, object], ...]:
    try:
        if (
            not manifest_path.is_file()
            or manifest_path.stat().st_size > _MAX_LOCAL_MANIFEST_BYTES
        ):
            raise VisualPermanentAdapterError("CONTENT_VISUALS_MANIFEST_UNAVAILABLE")
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except VisualPermanentAdapterError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VisualPermanentAdapterError("CONTENT_VISUALS_MANIFEST_INVALID") from exc
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "assets"}:
        raise VisualPermanentAdapterError("CONTENT_VISUALS_MANIFEST_INVALID")
    assets = payload.get("assets")
    if payload.get("schema_version") != 1 or not isinstance(assets, list):
        raise VisualPermanentAdapterError("CONTENT_VISUALS_MANIFEST_INVALID")
    if len(assets) != expected_count:
        raise VisualPermanentAdapterError("CONTENT_VISUALS_COUNT_MISMATCH")
    entries: list[dict[str, object]] = []
    seen_paths: set[Path] = set()
    seen_source_ids: set[str] = set()
    for asset in assets:
        if not isinstance(asset, dict) or set(asset) != _LOCAL_ASSET_FIELDS:
            raise VisualPermanentAdapterError("CONTENT_VISUALS_MANIFEST_INVALID")
        if asset.get("commercial_use") is not True:
            raise VisualPermanentAdapterError("CONTENT_VISUALS_COMMERCIAL_USE_BLOCKED")
        source_path = _local_asset_source_path(root, asset)
        source_id = _manifest_text(asset, "source_id", max_length=500)
        if source_path in seen_paths or source_id in seen_source_ids:
            raise VisualPermanentAdapterError("CONTENT_VISUALS_DUPLICATE_LOCAL_ASSET")
        seen_paths.add(source_path)
        seen_source_ids.add(source_id)
        _manifest_sha256(asset)
        _manifest_source_type(asset)
        for key, limit in (
            ("provider", 200),
            ("model_id", 200),
            ("source_uri", 1000),
            ("license_id", 200),
            ("attribution", 1000),
        ):
            _manifest_text(asset, key, max_length=limit)
        entries.append(asset)
    return tuple(entries)


def _local_asset_source_path(root: Path, entry: dict[str, object]) -> Path:
    relative_path = _manifest_text(entry, "relative_path", max_length=500)
    candidate = Path(relative_path)
    if candidate.is_absolute() or candidate.suffix.casefold() != ".png":
        raise VisualPermanentAdapterError("CONTENT_VISUALS_INVALID_LOCAL_PATH")
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise VisualPermanentAdapterError("CONTENT_VISUALS_INVALID_LOCAL_PATH") from exc
    if not resolved.is_file():
        raise VisualPermanentAdapterError("CONTENT_VISUALS_IMAGE_UNAVAILABLE")
    return resolved


def _manifest_text(
    entry: dict[str, object],
    key: str,
    *,
    max_length: int,
) -> str:
    value = entry.get(key)
    if not isinstance(value, str):
        raise VisualPermanentAdapterError("CONTENT_VISUALS_MANIFEST_INVALID")
    normalized = value.strip()
    if not normalized or len(normalized) > max_length:
        raise VisualPermanentAdapterError("CONTENT_VISUALS_MANIFEST_INVALID")
    return normalized


def _manifest_sha256(entry: dict[str, object]) -> str:
    value = _manifest_text(entry, "sha256", max_length=64).casefold()
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise VisualPermanentAdapterError("CONTENT_VISUALS_INVALID_LOCAL_SHA256")
    return value


def _manifest_source_type(
    entry: dict[str, object],
) -> Literal["generated", "stock", "owned"]:
    value = entry.get("source_type")
    if value not in {"generated", "stock", "owned"}:
        raise VisualPermanentAdapterError("CONTENT_VISUALS_INVALID_SOURCE_TYPE")
    return value


def _copy_pinned_local_asset(
    source: Path,
    destination: Path,
    *,
    expected_sha256: str,
) -> None:
    digest = hashlib.sha256()
    size_bytes = 0
    try:
        with source.open("rb") as source_handle, destination.open("xb") as output_handle:
            while chunk := source_handle.read(1024 * 1024):
                size_bytes += len(chunk)
                if size_bytes > _MAX_IMAGE_BYTES:
                    raise VisualPermanentAdapterError("CONTENT_VISUALS_INVALID_IMAGE_SIZE")
                digest.update(chunk)
                output_handle.write(chunk)
            output_handle.flush()
            os.fsync(output_handle.fileno())
    except VisualPermanentAdapterError:
        raise
    except OSError as exc:
        raise VisualPermanentAdapterError("CONTENT_VISUALS_IMAGE_UNAVAILABLE") from exc
    if size_bytes < 1 or digest.hexdigest() != expected_sha256:
        raise VisualPermanentAdapterError("CONTENT_VISUALS_LOCAL_ASSET_HASH_MISMATCH")


async def execute_approved_visual_step(
    session: AsyncSession,
    *,
    run: Run,
    approval: Approval,
    approval_input_payload: dict[str, object],
    script_artifact: Artifact,
    audio_artifact: Artifact,
    idempotency_key: str,
    storage_root: Path,
    adapter: VisualAdapter,
    queue: QueueClass,
    gpu_power_watts: int,
    timeout_seconds: float,
    finalize_run: bool = True,
    sleep: Sleeper = asyncio.sleep,
) -> VisualExecutionResult:
    """Create licensed visual assets from the exact approved script and A2 WAV."""
    if queue not in {QueueClass.GPU, QueueClass.IO}:
        raise ValueError("visual queue must be gpu or io")
    if gpu_power_watts < 1:
        raise ValueError("gpu_power_watts must be positive")
    context = _load_visual_context(
        run=run,
        approval=approval,
        approval_input_payload=approval_input_payload,
        script_artifact=script_artifact,
        audio_artifact=audio_artifact,
        storage_root=storage_root,
    )
    requests = _build_visual_requests(context)
    visual_directory = _visual_directory(
        storage_root,
        run_id=_persisted_id(run.id, "run"),
    )
    step_key = f"{idempotency_key}:{VISUAL_STEP_NAME}"

    async def prepare_operation(_context: TaskAttemptContext) -> dict[str, object]:
        started = time.perf_counter()
        try:
            bundle = await asyncio.to_thread(
                _prepare_visual_bundle,
                adapter,
                requests,
                visual_directory,
                context,
                storage_root,
            )
        except VisualRetryableAdapterError as exc:
            raise RetryableTaskError("CONTENT_VISUALS_BACKEND_UNAVAILABLE") from exc
        except VisualPermanentAdapterError as exc:
            raise PermanentTaskError(str(exc) or "CONTENT_VISUALS_INVALID_OUTPUT") from exc
        bundle["inference_seconds"] = str(
            Decimal(str(round(time.perf_counter() - started, 6)))
        )
        return bundle

    task_result = await execute_task_step(
        session,
        run=run,
        spec=TaskStepSpec(
            name=VISUAL_STEP_NAME,
            queue=queue,
            ordinal=3,
            idempotency_key=step_key,
            input_payload={
                **approval_input_payload,
                "audio_artifact_id": context.audio_artifact_id,
                "audio_artifact_sha256": context.audio_artifact_sha256,
                "visual_count": len(requests),
                "width": requests[0].width,
                "height": requests[0].height,
            },
        ),
        policy=RetryPolicy(max_attempts=2, timeout_seconds=timeout_seconds),
        operation=prepare_operation,
        sleep=sleep,
    )
    step_run = await session.get(StepRun, task_result.step_run_ids[-1])
    if step_run is None:
        raise ValueError("visual step result was not persisted")
    if task_result.status is RunStatus.CANCELLED:
        return _result(run, step_run, None, (), replayed=task_result.replayed)
    if task_result.status is RunStatus.FAILED:
        if task_result.error is None:
            raise ValueError("failed visual step is missing structured error")
        if RunStatus(run.status) is RunStatus.RUNNING:
            await transition_run(
                session,
                run,
                RunStatus.FAILED,
                error=task_result.error,
                note="Content Engine A3 visuals failed",
            )
        return _result(run, step_run, None, (), replayed=task_result.replayed)

    try:
        async with session.begin_nested():
            automation = await session.get(Automation, run.automation_id)
            if automation is None:
                raise ValueError("visual automation does not exist")
            output = task_result.output_payload or {}
            manifest_path = _output_path(storage_root, output, "manifest_relative_path")
            manifest = (
                await register_local_artifact(
                    session,
                    run=run,
                    step_run=step_run,
                    storage_root=storage_root,
                    file_path=manifest_path,
                    idempotency_key=f"{idempotency_key}:visual-manifest",
                    artifact_type="visual_manifest",
                    media_type="application/json",
                    origin=_SOURCE,
                    sensitivity=ArtifactSensitivity.INTERNAL,
                    retention_days=_RETENTION_DAYS,
                )
            ).artifact
            asset_outputs = _asset_outputs(output)
            visual_artifacts: list[Artifact] = []
            for index, asset_output in enumerate(asset_outputs, start=1):
                file_path = _output_path(
                    storage_root,
                    asset_output,
                    "relative_path",
                )
                visual_artifacts.append(
                    (
                        await register_local_artifact(
                            session,
                            run=run,
                            step_run=step_run,
                            storage_root=storage_root,
                            file_path=file_path,
                            idempotency_key=f"{idempotency_key}:visual-{index}",
                            artifact_type="visual_image",
                            media_type="image/png",
                            origin=_SOURCE,
                            sensitivity=ArtifactSensitivity.INTERNAL,
                            retention_days=_RETENTION_DAYS,
                            expected_sha256=_required_text(
                                asset_output,
                                "sha256",
                                max_length=64,
                            ),
                        )
                    ).artifact
                )
            dimensions = _metric_dimensions(asset_outputs, queue=queue)
            count_metric = (
                await record_metric_point(
                    session,
                    automation=automation,
                    run=run,
                    step_run=step_run,
                    idempotency_key=f"{idempotency_key}:visual-count",
                    name="content_visual_asset_count",
                    kind=MetricKind.COUNTER,
                    value=len(visual_artifacts),
                    unit="images",
                    source=_SOURCE,
                    dimensions=dimensions,
                )
            ).metric_point
            coverage_metric = (
                await record_metric_point(
                    session,
                    automation=automation,
                    run=run,
                    step_run=step_run,
                    idempotency_key=f"{idempotency_key}:visual-coverage",
                    name="content_visual_seconds_per_asset",
                    kind=MetricKind.GAUGE,
                    value=(
                        context.duration_seconds / Decimal(len(visual_artifacts))
                    ).quantize(Decimal("0.0000000001")),
                    unit="seconds_per_image",
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
                        idempotency_key=f"{idempotency_key}:visual-energy-estimate",
                        name="content_visual_energy_estimate_kwh",
                        kind=MetricKind.GAUGE,
                        value=energy.quantize(Decimal("0.0000000001")),
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
                    idempotency_key=f"{idempotency_key}:visual-external-api-cost",
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
            "code": "CONTENT_VISUALS_EVIDENCE_PERSIST_FAILED",
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
                note="Content Engine A3 evidence persistence failed",
            )
        return _result(run, step_run, None, (), replayed=task_result.replayed)

    visual_ids = tuple(_persisted_id(item.id, "artifact") for item in visual_artifacts)
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
                "visual_step_run_id": step_run.id,
                "visual_manifest_artifact_id": manifest.id,
                "visual_artifact_ids": list(visual_ids),
                "visual_count_metric_id": count_metric.id,
                "visual_coverage_metric_id": coverage_metric.id,
                "visual_energy_metric_id": energy_metric_id,
                "visual_ledger_entry_id": ledger.id,
            },
            note="Content Engine A3 completed without external publication",
        )
    return _result(
        run,
        step_run,
        _persisted_id(manifest.id, "artifact"),
        visual_ids,
        replayed=task_result.replayed,
    )


def _load_visual_context(
    *,
    run: Run,
    approval: Approval,
    approval_input_payload: dict[str, object],
    script_artifact: Artifact,
    audio_artifact: Artifact,
    storage_root: Path,
) -> _VisualContext:
    run_id = _persisted_id(run.id, "run")
    if script_artifact.run_id != run_id or audio_artifact.run_id != run_id:
        raise ValueError("visual source artifacts must belong to the run")
    if approval.run_id != run_id:
        raise ValueError("visual approval must belong to the run")
    if ApprovalStatus(approval.status) is not ApprovalStatus.APPROVED:
        raise ValueError("visual script approval must be approved")
    if approval.payload_digest != approval_payload_digest(approval_input_payload):
        raise ValueError("visual approval payload does not match durable approval")
    if approval_input_payload.get("artifact_id") != script_artifact.id:
        raise ValueError("visual approval does not identify the script artifact")
    if approval_input_payload.get("artifact_sha256") != script_artifact.sha256:
        raise ValueError("visual approval does not match the script artifact")
    if script_artifact.artifact_type != "script_bundle":
        raise ValueError("visual script source must be a script bundle")
    if (
        audio_artifact.artifact_type != "narration_audio"
        or audio_artifact.media_type != "audio/wav"
        or not isinstance(audio_artifact.step_run_id, int)
    ):
        raise ValueError("visual audio source must be a persisted narration WAV")

    script_path = _verified_artifact_path(storage_root, script_artifact)
    if script_artifact.size_bytes > _MAX_SCRIPT_BUNDLE_BYTES:
        raise ValueError("visual script bundle exceeds the size limit")
    try:
        payload = json.loads(script_path.read_text(encoding="utf-8"))
        result = payload.get("result") if isinstance(payload, dict) else None
        format_value = payload.get("format") if isinstance(payload, dict) else None
        if (
            payload.get("schema_version") != 1
            or not isinstance(result, dict)
            or format_value not in {"short", "long"}
        ):
            raise ValueError("unsupported script bundle")
        full_script = _json_text(result, "full_script", max_length=200_000)
        narration = _json_text(result, "narration", max_length=200_000)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("visual script bundle is invalid") from exc
    narration_digest = hashlib.sha256(narration.encode("utf-8")).hexdigest()
    if approval_input_payload.get("narration_sha256") != narration_digest:
        raise ValueError("visual narration does not match the approved script")

    audio_path = _verified_artifact_path(storage_root, audio_artifact)
    duration = _wav_duration(audio_path)
    return _VisualContext(
        script_artifact_id=_persisted_id(script_artifact.id, "artifact"),
        script_artifact_sha256=script_artifact.sha256,
        audio_artifact_id=_persisted_id(audio_artifact.id, "artifact"),
        audio_artifact_sha256=audio_artifact.sha256,
        audio_step_run_id=audio_artifact.step_run_id,
        duration_seconds=duration,
        format=format_value,
        full_script=full_script,
        narration=narration,
    )


def _build_visual_requests(context: _VisualContext) -> tuple[VisualRequest, ...]:
    desired = max(
        1,
        min(
            _MAX_VISUALS,
            math.ceil(context.duration_seconds / _SECONDS_PER_VISUAL),
        ),
    )
    segments = _split_editorial_text(context.full_script, desired)
    width, height = (1080, 1920) if context.format == "short" else (1920, 1080)
    return tuple(
        VisualRequest(
            index=index,
            prompt=f"Editorial documentary image, no text or logos: {segment}",
            width=width,
            height=height,
        )
        for index, segment in enumerate(segments, start=1)
    )


def _split_editorial_text(text: str, count: int) -> tuple[str, ...]:
    words = text.split()
    if not words:
        raise ValueError("approved script cannot produce an empty visual plan")
    count = min(count, len(words))
    segments: list[str] = []
    for index in range(count):
        start = index * len(words) // count
        end = (index + 1) * len(words) // count
        segment = " ".join(words[start:end]).strip()
        segments.append(segment[:450])
    return tuple(segments)


def _prepare_visual_bundle(
    adapter: VisualAdapter,
    requests: tuple[VisualRequest, ...],
    destination: Path,
    context: _VisualContext,
    storage_root: Path,
) -> dict[str, object]:
    if destination.exists():
        return _load_existing_bundle(destination, requests, context, storage_root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    temporary.mkdir()
    try:
        results = adapter(requests, temporary)
        assets = _validate_visual_results(results, requests, temporary)
        manifest_payload = _manifest_payload(context, requests, assets)
        manifest_path = temporary / "manifest.json"
        _write_json_file(manifest_path, manifest_payload)
        os.replace(temporary, destination)
        return _bundle_output(destination, manifest_payload, storage_root)
    except (VisualPermanentAdapterError, VisualRetryableAdapterError):
        raise
    except Exception as exc:
        raise VisualPermanentAdapterError("CONTENT_VISUALS_INVALID_OUTPUT") from exc
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def _validate_visual_results(
    results: tuple[VisualAssetResult, ...],
    requests: tuple[VisualRequest, ...],
    directory: Path,
) -> list[dict[str, object]]:
    if not isinstance(results, tuple) or len(results) != len(requests):
        raise VisualPermanentAdapterError("CONTENT_VISUALS_COUNT_MISMATCH")
    seen: set[str] = set()
    assets: list[dict[str, object]] = []
    for request, result in zip(requests, results, strict=True):
        if not isinstance(result, VisualAssetResult):
            raise VisualPermanentAdapterError("CONTENT_VISUALS_INVALID_METADATA")
        file_name = result.file_name.strip()
        if (
            not file_name
            or file_name != Path(file_name).name
            or Path(file_name).suffix.casefold() != ".png"
            or file_name in seen
        ):
            raise VisualPermanentAdapterError("CONTENT_VISUALS_INVALID_FILE_NAME")
        seen.add(file_name)
        metadata = (
            result.provider,
            result.model_id,
            result.source_id,
            result.source_uri,
            result.license_id,
            result.attribution,
        )
        if any(not value.strip() or len(value) > 1000 for value in metadata):
            raise VisualPermanentAdapterError("CONTENT_VISUALS_INVALID_METADATA")
        if result.source_type not in {"generated", "stock", "owned"}:
            raise VisualPermanentAdapterError("CONTENT_VISUALS_INVALID_SOURCE_TYPE")
        if result.commercial_use is not True:
            raise VisualPermanentAdapterError("CONTENT_VISUALS_COMMERCIAL_USE_BLOCKED")
        if urlparse(result.source_uri).scheme not in {
            "https",
            "local-asset",
            "local-model",
        }:
            raise VisualPermanentAdapterError("CONTENT_VISUALS_INVALID_SOURCE_URI")
        image_path = directory / file_name
        inspection = _inspect_png(
            image_path,
            expected_width=request.width,
            expected_height=request.height,
        )
        with image_path.open("r+b") as handle:
            os.fsync(handle.fileno())
        assets.append(
            {
                "index": request.index,
                "prompt": request.prompt,
                "file_name": file_name,
                "source_type": result.source_type,
                "provider": result.provider.strip(),
                "model_id": result.model_id.strip(),
                "source_id": result.source_id.strip(),
                "source_uri": result.source_uri.strip(),
                "license_id": result.license_id.strip(),
                "commercial_use": True,
                "attribution": result.attribution.strip(),
                "width": inspection.width,
                "height": inspection.height,
                "sha256": inspection.sha256,
                "size_bytes": inspection.size_bytes,
            }
        )
    return assets


def _inspect_png(
    path: Path,
    *,
    expected_width: int,
    expected_height: int,
) -> _PngInspection:
    try:
        encoded = path.read_bytes()
    except OSError as exc:
        raise VisualPermanentAdapterError("CONTENT_VISUALS_IMAGE_UNAVAILABLE") from exc
    if len(encoded) < 57 or len(encoded) > _MAX_IMAGE_BYTES:
        raise VisualPermanentAdapterError("CONTENT_VISUALS_INVALID_IMAGE_SIZE")
    if encoded[:8] != b"\x89PNG\r\n\x1a\n":
        raise VisualPermanentAdapterError("CONTENT_VISUALS_PNG_REQUIRED")
    position = 8
    ihdr: tuple[int, int, int, int, int] | None = None
    compressed = bytearray()
    saw_end = False
    while position < len(encoded):
        if position + 12 > len(encoded):
            raise VisualPermanentAdapterError("CONTENT_VISUALS_TRUNCATED_PNG")
        length = struct.unpack(">I", encoded[position : position + 4])[0]
        chunk_end = position + 12 + length
        if chunk_end > len(encoded):
            raise VisualPermanentAdapterError("CONTENT_VISUALS_TRUNCATED_PNG")
        chunk_type = encoded[position + 4 : position + 8]
        data = encoded[position + 8 : position + 8 + length]
        expected_crc = struct.unpack(">I", encoded[position + 8 + length : chunk_end])[0]
        if zlib.crc32(chunk_type + data) & 0xFFFFFFFF != expected_crc:
            raise VisualPermanentAdapterError("CONTENT_VISUALS_INVALID_PNG_CRC")
        if chunk_type == b"IHDR":
            if ihdr is not None or length != 13:
                raise VisualPermanentAdapterError("CONTENT_VISUALS_INVALID_PNG_HEADER")
            width, height, bit_depth, color_type, compression, filtering, interlace = (
                struct.unpack(">IIBBBBB", data)
            )
            if compression != 0 or filtering != 0 or interlace != 0:
                raise VisualPermanentAdapterError("CONTENT_VISUALS_UNSUPPORTED_PNG")
            ihdr = (width, height, bit_depth, color_type, interlace)
        elif chunk_type == b"IDAT":
            compressed.extend(data)
        elif chunk_type == b"IEND":
            if length != 0 or chunk_end != len(encoded):
                raise VisualPermanentAdapterError("CONTENT_VISUALS_INVALID_PNG_END")
            saw_end = True
        position = chunk_end
    if ihdr is None or not compressed or not saw_end:
        raise VisualPermanentAdapterError("CONTENT_VISUALS_INCOMPLETE_PNG")
    width, height, bit_depth, color_type, _interlace = ihdr
    if width != expected_width or height != expected_height:
        raise VisualPermanentAdapterError("CONTENT_VISUALS_DIMENSION_MISMATCH")
    if width * height > _MAX_IMAGE_PIXELS or bit_depth != 8 or color_type not in {2, 6}:
        raise VisualPermanentAdapterError("CONTENT_VISUALS_UNSUPPORTED_PNG")
    channels = 3 if color_type == 2 else 4
    expected_decoded = height * (1 + width * channels)
    try:
        decompressor = zlib.decompressobj()
        decoded = decompressor.decompress(
            bytes(compressed),
            max_length=expected_decoded + 1,
        )
        if decompressor.unconsumed_tail or len(decoded) > expected_decoded:
            raise VisualPermanentAdapterError("CONTENT_VISUALS_INVALID_PNG_DATA")
        decoded += decompressor.flush()
    except zlib.error as exc:
        raise VisualPermanentAdapterError("CONTENT_VISUALS_INVALID_PNG_DATA") from exc
    if (
        len(decoded) != expected_decoded
        or not decompressor.eof
        or bool(decompressor.unused_data)
    ):
        raise VisualPermanentAdapterError("CONTENT_VISUALS_TRUNCATED_PNG_DATA")
    stride = 1 + width * channels
    if any(decoded[offset] > 4 for offset in range(0, len(decoded), stride)):
        raise VisualPermanentAdapterError("CONTENT_VISUALS_INVALID_PNG_FILTER")
    return _PngInspection(
        width=width,
        height=height,
        sha256=hashlib.sha256(encoded).hexdigest(),
        size_bytes=len(encoded),
    )


def _manifest_payload(
    context: _VisualContext,
    requests: tuple[VisualRequest, ...],
    assets: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "script_artifact_id": context.script_artifact_id,
        "script_artifact_sha256": context.script_artifact_sha256,
        "audio_artifact_id": context.audio_artifact_id,
        "audio_artifact_sha256": context.audio_artifact_sha256,
        "audio_duration_seconds": str(context.duration_seconds),
        "format": context.format,
        "visual_count": len(requests),
        "assets": assets,
    }


def _write_json_file(path: Path, payload: dict[str, object]) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _load_existing_bundle(
    destination: Path,
    requests: tuple[VisualRequest, ...],
    context: _VisualContext,
    storage_root: Path,
) -> dict[str, object]:
    manifest_path = destination / "manifest.json"
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VisualPermanentAdapterError("CONTENT_VISUALS_EXISTING_BUNDLE_INVALID") from exc
    expected_identity = (
        payload.get("schema_version"),
        payload.get("script_artifact_id"),
        payload.get("script_artifact_sha256"),
        payload.get("audio_artifact_id"),
        payload.get("audio_artifact_sha256"),
        payload.get("visual_count"),
    )
    if expected_identity != (
        1,
        context.script_artifact_id,
        context.script_artifact_sha256,
        context.audio_artifact_id,
        context.audio_artifact_sha256,
        len(requests),
    ):
        raise VisualPermanentAdapterError("CONTENT_VISUALS_EXISTING_BUNDLE_CONFLICT")
    assets = payload.get("assets")
    if not isinstance(assets, list) or len(assets) != len(requests):
        raise VisualPermanentAdapterError("CONTENT_VISUALS_EXISTING_BUNDLE_INVALID")
    for request, asset in zip(requests, assets, strict=True):
        if not isinstance(asset, dict) or asset.get("prompt") != request.prompt:
            raise VisualPermanentAdapterError("CONTENT_VISUALS_EXISTING_BUNDLE_CONFLICT")
        file_name = asset.get("file_name")
        sha256 = asset.get("sha256")
        if not isinstance(file_name, str) or file_name != Path(file_name).name:
            raise VisualPermanentAdapterError("CONTENT_VISUALS_EXISTING_BUNDLE_INVALID")
        inspection = _inspect_png(
            destination / file_name,
            expected_width=request.width,
            expected_height=request.height,
        )
        if inspection.sha256 != sha256:
            raise VisualPermanentAdapterError("CONTENT_VISUALS_EXISTING_BUNDLE_TAMPERED")
    return _bundle_output(destination, payload, storage_root)


def _bundle_output(
    directory: Path,
    payload: dict[str, object],
    storage_root: Path,
) -> dict[str, object]:
    assets = payload.get("assets")
    if not isinstance(assets, list):
        raise VisualPermanentAdapterError("CONTENT_VISUALS_INVALID_MANIFEST")
    relative_directory = directory.relative_to(storage_root.resolve())
    return {
        "manifest_relative_path": (relative_directory / "manifest.json").as_posix(),
        "assets": [
            {
                **asset,
                "relative_path": (
                    relative_directory / str(asset["file_name"])
                ).as_posix(),
            }
            for asset in assets
            if isinstance(asset, dict)
        ],
    }


def _verified_artifact_path(storage_root: Path, artifact: Artifact) -> Path:
    root = storage_root.resolve()
    path = (root / artifact.relative_path).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("visual source artifact escapes storage root") from exc
    try:
        stat = path.stat()
    except OSError as exc:
        raise ValueError("visual source artifact is unavailable") from exc
    if not path.is_file() or stat.st_size != artifact.size_bytes:
        raise ValueError("visual source artifact size does not match durable metadata")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest() != artifact.sha256:
        raise ValueError("visual source artifact checksum does not match durable metadata")
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
                raise ValueError("unsupported narration WAV")
            duration = Decimal(audio.getnframes()) / Decimal(audio.getframerate())
    except (OSError, EOFError, wave.Error) as exc:
        raise ValueError("visual narration WAV is invalid") from exc
    if duration > Decimal(6 * 60 * 60):
        raise ValueError("visual narration WAV exceeds six hours")
    return duration.quantize(Decimal("0.0000000001"))


def _visual_directory(storage_root: Path, *, run_id: int) -> Path:
    root = storage_root.resolve()
    if root == Path(root.anchor):
        raise ValueError("storage root must not be a filesystem root")
    return root / "content-engine" / f"run-{run_id}" / "visuals"


def _output_path(
    storage_root: Path,
    output: dict[str, object],
    key: str,
) -> Path:
    value = output.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"visual output {key} is unavailable")
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = storage_root.resolve() / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(storage_root.resolve())
    except ValueError as exc:
        raise ValueError(f"visual output {key} escapes storage root") from exc
    return resolved


def _asset_outputs(output: dict[str, object]) -> list[dict[str, object]]:
    assets = output.get("assets")
    if not isinstance(assets, list) or not assets:
        raise ValueError("visual asset outputs are unavailable")
    if any(not isinstance(asset, dict) for asset in assets):
        raise ValueError("visual asset output is invalid")
    return assets


def _metric_dimensions(
    assets: list[dict[str, object]],
    *,
    queue: QueueClass,
) -> dict[str, object]:
    providers = sorted({_required_text(asset, "provider", max_length=200) for asset in assets})
    source_types = sorted(
        {_required_text(asset, "source_type", max_length=20) for asset in assets}
    )
    licenses = sorted(
        {_required_text(asset, "license_id", max_length=200) for asset in assets}
    )
    return {
        "queue": queue.value,
        "providers": providers,
        "source_types": source_types,
        "licenses": licenses,
    }


def _required_text(
    payload: dict[str, object],
    key: str,
    *,
    max_length: int,
) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise ValueError(f"visual output {key} is invalid")
    return value


def _json_text(payload: dict[str, object], key: str, *, max_length: int) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ValueError(f"script result {key} is invalid")
    normalized = value.strip()
    if not normalized or len(normalized) > max_length:
        raise ValueError(f"script result {key} is invalid")
    return normalized


def _decimal_output(output: dict[str, object], key: str) -> Decimal:
    value = output.get(key)
    if not isinstance(value, str):
        raise ValueError(f"visual output {key} is unavailable")
    parsed = Decimal(value)
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"visual output {key} is invalid")
    return parsed


def _persisted_id(value: int | None, kind: str) -> int:
    if not isinstance(value, int):
        raise ValueError(f"{kind} must be persisted")
    return value


def _result(
    run: Run,
    step_run: StepRun,
    manifest_artifact_id: int | None,
    visual_artifact_ids: tuple[int, ...],
    *,
    replayed: bool,
) -> VisualExecutionResult:
    return VisualExecutionResult(
        run_id=_persisted_id(run.id, "run"),
        step_run_id=_persisted_id(step_run.id, "step"),
        manifest_artifact_id=manifest_artifact_id,
        visual_artifact_ids=visual_artifact_ids,
        run_status=run.status,
        step_status=step_run.status,
        replayed=replayed,
    )
