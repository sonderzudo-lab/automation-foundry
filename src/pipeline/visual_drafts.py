"""Numbered rounds of generated A3 drafts, reviewed before the official bundle.

A generating visual backend does not publish the A3 bundle directly. Round 1
generates one draft per request; each later round regenerates only the images
the reviewer sent back, rewriting their descriptions from the reviewer's notes,
and copies every other draft unchanged from the previous round. Each round is a
GPU step of its own with its own manifest and images, so every set a person
reviewed stays as evidence. Once a round is approved, A3 imports exactly those
files, checked by SHA-256, and the rest of the pipeline sees a normal bundle.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from src.operations.retention_policy import resolve_retention_days
from src.pipeline.visual_review import VISUAL_DRAFT_STEP_PREFIX, VisualReviewImage
from src.pipeline.visuals import (
    QUALITY_TEST_USAGE_SCOPE,
    VisualAdapter,
    VisualAssetResult,
    VisualPermanentAdapterError,
    VisualRequest,
    VisualRequestPlan,
    VisualRetryableAdapterError,
    validate_generated_visuals,
    visual_draft_directory,
)
from src.platform.artifact_service import register_local_artifact
from src.platform.models import (
    Artifact,
    ArtifactSensitivity,
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

_SOURCE = "content-engine:a3"
_MANIFEST_RETENTION_DAYS = resolve_retention_days("visual_draft_manifest")
_IMAGE_RETENTION_DAYS = resolve_retention_days("visual_draft_image")
_MAX_MANIFEST_BYTES = 1024 * 1024

# revisions (request index -> previous scene and note), full narration, kept scenes.
DraftAdapterFactory = Callable[
    [Mapping[int, tuple[str, str]] | None, str, Sequence[str]],
    VisualAdapter,
]


@dataclass(frozen=True, slots=True)
class DraftRoundResult:
    """Observable identifiers of one draft round."""

    run_id: int
    round_number: int
    step_run_id: int | None
    manifest_artifact_id: int | None
    image_artifact_ids: tuple[int, ...]
    run_status: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class PreviousRound:
    """The reviewed images of the round a redo request refers to."""

    images: tuple[tuple[VisualReviewImage, dict[str, object]], ...]
    notes: Mapping[int, str]


async def execute_visual_draft_round(
    session: AsyncSession,
    *,
    run: Run,
    approval_input_payload: dict[str, object],
    plan: VisualRequestPlan,
    round_number: int,
    previous: PreviousRound | None,
    idempotency_key: str,
    storage_root: Path,
    adapter_factory: DraftAdapterFactory,
    style_prompt_suffix: str,
    timeout_seconds: float,
    sleep: Sleeper = asyncio.sleep,
) -> DraftRoundResult:
    """Generate (round 1) or partly regenerate (later rounds) one set of drafts."""
    if round_number < 1 or (round_number == 1) != (previous is None):
        raise ValueError("draft round and previous round do not match")
    if previous is not None and not previous.notes:
        raise ValueError("a later draft round needs the images to redo")
    run_id = _persisted_id(run.id, "run")
    destination = visual_draft_directory(storage_root, run_id=run_id, round_number=round_number)
    step_name = f"{VISUAL_DRAFT_STEP_PREFIX}{round_number}"
    step_key = f"{idempotency_key}:{step_name}"
    notes = dict(previous.notes) if previous is not None else {}

    async def operation(_context: TaskAttemptContext) -> dict[str, object]:
        started = time.perf_counter()
        try:
            output = await asyncio.to_thread(
                _prepare_round,
                plan=plan,
                round_number=round_number,
                previous=previous,
                destination=destination,
                storage_root=storage_root,
                adapter_factory=adapter_factory,
                style_prompt_suffix=style_prompt_suffix,
            )
        except VisualRetryableAdapterError as exc:
            raise RetryableTaskError("CONTENT_VISUALS_BACKEND_UNAVAILABLE") from exc
        except VisualPermanentAdapterError as exc:
            raise PermanentTaskError(str(exc) or "CONTENT_VISUALS_INVALID_OUTPUT") from exc
        output["inference_seconds"] = str(Decimal(str(round(time.perf_counter() - started, 6))))
        return output

    task_result = await execute_task_step(
        session,
        run=run,
        spec=TaskStepSpec(
            name=step_name,
            queue=QueueClass.GPU,
            ordinal=3,
            idempotency_key=step_key,
            input_payload={
                **approval_input_payload,
                "round": round_number,
                "audio_artifact_id": plan.audio_artifact_id,
                "audio_artifact_sha256": plan.audio_artifact_sha256,
                "visual_count": len(plan.requests),
                "redo": {str(index): note for index, note in sorted(notes.items())},
            },
        ),
        policy=RetryPolicy(max_attempts=2, timeout_seconds=timeout_seconds),
        operation=operation,
        sleep=sleep,
    )
    step_run = await session.get(StepRun, task_result.step_run_ids[-1])
    if step_run is None:
        raise ValueError("visual draft step result was not persisted")
    if task_result.status is RunStatus.FAILED:
        if RunStatus(run.status) is RunStatus.RUNNING:
            await transition_run(
                session,
                run,
                RunStatus.FAILED,
                error=task_result.error,
                note=f"Content Engine A3 draft round {round_number} failed",
            )
        return _result(run, round_number, step_run, None, (), task_result.replayed)
    if task_result.status is RunStatus.CANCELLED:
        return _result(run, round_number, step_run, None, (), task_result.replayed)

    output = task_result.output_payload or {}
    try:
        async with session.begin_nested():
            manifest = (
                await register_local_artifact(
                    session,
                    run=run,
                    step_run=step_run,
                    storage_root=storage_root,
                    file_path=storage_root / str(output["manifest_relative_path"]),
                    idempotency_key=f"{step_key}:manifest",
                    artifact_type="visual_draft_manifest",
                    media_type="application/json",
                    origin=_SOURCE,
                    sensitivity=ArtifactSensitivity.INTERNAL,
                    retention_days=_MANIFEST_RETENTION_DAYS,
                )
            ).artifact
            images: list[Artifact] = []
            for asset in _asset_list(output):
                images.append(
                    (
                        await register_local_artifact(
                            session,
                            run=run,
                            step_run=step_run,
                            storage_root=storage_root,
                            file_path=storage_root / str(asset["relative_path"]),
                            idempotency_key=f"{step_key}:image-{asset['index']}",
                            artifact_type="visual_draft_image",
                            media_type="image/png",
                            origin=_SOURCE,
                            sensitivity=ArtifactSensitivity.INTERNAL,
                            retention_days=_IMAGE_RETENTION_DAYS,
                            expected_sha256=str(asset["sha256"]),
                        )
                    ).artifact
                )
    except Exception as exc:
        if RunStatus(run.status) is RunStatus.RUNNING:
            await transition_run(
                session,
                run,
                RunStatus.FAILED,
                error={
                    "code": "CONTENT_VISUALS_DRAFT_EVIDENCE_PERSIST_FAILED",
                    "exception_type": type(exc).__name__,
                    "retryable": True,
                    "timed_out": False,
                },
                note=f"Content Engine A3 draft round {round_number} evidence failed",
            )
        return _result(run, round_number, step_run, None, (), task_result.replayed)
    return _result(
        run,
        round_number,
        step_run,
        _persisted_id(manifest.id, "artifact"),
        tuple(_persisted_id(image.id, "artifact") for image in images),
        task_result.replayed,
    )


def build_approved_draft_adapter(
    images: Sequence[tuple[VisualReviewImage, dict[str, object]]],
) -> VisualAdapter:
    """Import the approved drafts into A3, unchanged and checked by SHA-256."""

    def import_drafts(
        requests: tuple[VisualRequest, ...],
        destination: Path,
    ) -> tuple[VisualAssetResult, ...]:
        if len(requests) != len(images):
            raise VisualPermanentAdapterError("CONTENT_VISUALS_COUNT_MISMATCH")
        results: list[VisualAssetResult] = []
        for request, (image, asset) in zip(requests, images, strict=True):
            if image.index != request.index or asset.get("prompt") != request.prompt:
                raise VisualPermanentAdapterError("CONTENT_VISUALS_DRAFT_CONFLICT")
            file_name = f"visual-{request.index:03d}.png"
            target = destination / file_name
            shutil.copyfile(image.image_path, target)
            if _sha256(target) != image.sha256:
                raise VisualPermanentAdapterError("CONTENT_VISUALS_DRAFT_HASH_MISMATCH")
            seed = asset.get("seed")
            results.append(
                VisualAssetResult(
                    file_name=file_name,
                    source_type="generated",
                    provider=str(asset["provider"]),
                    model_id=str(asset["model_id"]),
                    source_id=str(asset["source_id"]),
                    source_uri=str(asset["source_uri"]),
                    license_id=str(asset["license_id"]),
                    commercial_use=asset.get("commercial_use") is True,
                    attribution=str(asset["attribution"]),
                    generation_prompt=str(asset.get("generation_prompt") or ""),
                    seed=seed if isinstance(seed, int) and not isinstance(seed, bool) else None,
                )
            )
        return tuple(results)

    return import_drafts


def _prepare_round(
    *,
    plan: VisualRequestPlan,
    round_number: int,
    previous: PreviousRound | None,
    destination: Path,
    storage_root: Path,
    adapter_factory: DraftAdapterFactory,
    style_prompt_suffix: str,
) -> dict[str, object]:
    if destination.exists():
        return _load_round(destination, plan, round_number, storage_root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    temporary.mkdir()
    try:
        if previous is None:
            adapter = adapter_factory(None, plan.narration, ())
            assets = validate_generated_visuals(
                adapter(plan.requests, temporary), plan.requests, temporary
            )
        else:
            assets = _redo_round(
                plan=plan,
                previous=previous,
                temporary=temporary,
                adapter_factory=adapter_factory,
                style_prompt_suffix=style_prompt_suffix,
            )
        manifest: dict[str, object] = {
            "schema_version": 1,
            "round": round_number,
            "script_artifact_id": plan.script_artifact_id,
            "script_artifact_sha256": plan.script_artifact_sha256,
            "audio_artifact_id": plan.audio_artifact_id,
            "audio_artifact_sha256": plan.audio_artifact_sha256,
            "audio_duration_seconds": str(plan.duration_seconds),
            "format": plan.format,
            "visual_count": len(plan.requests),
            "assets": assets,
            **(
                {"usage_scope": QUALITY_TEST_USAGE_SCOPE}
                if any(asset.get("commercial_use") is not True for asset in assets)
                else {}
            ),
        }
        with (temporary / "manifest.json").open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        return _round_output(destination, manifest, storage_root)
    except (VisualPermanentAdapterError, VisualRetryableAdapterError):
        raise
    except Exception as exc:
        raise VisualPermanentAdapterError("CONTENT_VISUALS_INVALID_OUTPUT") from exc
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def _redo_round(
    *,
    plan: VisualRequestPlan,
    previous: PreviousRound,
    temporary: Path,
    adapter_factory: DraftAdapterFactory,
    style_prompt_suffix: str,
) -> list[dict[str, object]]:
    by_index = {image.index: (image, asset) for image, asset in previous.images}
    if set(by_index) != {request.index for request in plan.requests}:
        raise VisualPermanentAdapterError("CONTENT_VISUALS_DRAFT_CONFLICT")
    if not set(previous.notes) <= set(by_index):
        raise VisualPermanentAdapterError("CONTENT_VISUALS_REVISION_MISSING")
    redo_requests = tuple(r for r in plan.requests if r.index in previous.notes)
    kept_scenes = [
        _scene(asset, style_prompt_suffix)
        for index, (_image, asset) in sorted(by_index.items())
        if index not in previous.notes
    ]
    revisions = {
        index: (_scene(by_index[index][1], style_prompt_suffix), note)
        for index, note in previous.notes.items()
    }
    adapter = adapter_factory(revisions, plan.narration, kept_scenes)
    new_assets = {
        int(str(asset["index"])): {**asset, "revision_note": previous.notes[int(str(asset["index"]))]}
        for asset in validate_generated_visuals(
            adapter(redo_requests, temporary), redo_requests, temporary
        )
    }
    assets: list[dict[str, object]] = []
    for request in plan.requests:
        if request.index in new_assets:
            assets.append(new_assets[request.index])
            continue
        image, asset = by_index[request.index]
        file_name = f"visual-{request.index:03d}.png"
        shutil.copyfile(image.image_path, temporary / file_name)
        if _sha256(temporary / file_name) != image.sha256:
            raise VisualPermanentAdapterError("CONTENT_VISUALS_DRAFT_HASH_MISMATCH")
        kept = {key: value for key, value in asset.items() if key != "revision_note"}
        assets.append({**kept, "file_name": file_name})
    return assets


def _load_round(
    destination: Path,
    plan: VisualRequestPlan,
    round_number: int,
    storage_root: Path,
) -> dict[str, object]:
    """Replay a published round after checking it still matches its sources."""
    try:
        encoded = (destination / "manifest.json").read_bytes()
        if len(encoded) > _MAX_MANIFEST_BYTES:
            raise ValueError("draft manifest too large")
        manifest = json.loads(encoded.decode("utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise VisualPermanentAdapterError("CONTENT_VISUALS_EXISTING_BUNDLE_INVALID") from exc
    identity = (
        manifest.get("round"),
        manifest.get("script_artifact_sha256"),
        manifest.get("audio_artifact_sha256"),
        manifest.get("visual_count"),
    )
    if identity != (
        round_number,
        plan.script_artifact_sha256,
        plan.audio_artifact_sha256,
        len(plan.requests),
    ):
        raise VisualPermanentAdapterError("CONTENT_VISUALS_EXISTING_BUNDLE_CONFLICT")
    for asset in _asset_list({"assets": manifest.get("assets")}):
        if _sha256(destination / str(asset["file_name"])) != asset.get("sha256"):
            raise VisualPermanentAdapterError("CONTENT_VISUALS_EXISTING_BUNDLE_TAMPERED")
    return _round_output(destination, manifest, storage_root)


def _round_output(
    directory: Path,
    manifest: dict[str, object],
    storage_root: Path,
) -> dict[str, object]:
    relative = directory.relative_to(storage_root.resolve())
    return {
        "manifest_relative_path": (relative / "manifest.json").as_posix(),
        "assets": [
            {
                "index": asset["index"],
                "sha256": asset["sha256"],
                "relative_path": (relative / str(asset["file_name"])).as_posix(),
            }
            for asset in _asset_list(manifest)
        ],
    }


def _asset_list(payload: dict[str, object]) -> list[dict[str, object]]:
    assets = payload.get("assets")
    if not isinstance(assets, list) or not assets or not all(
        isinstance(asset, dict) for asset in assets
    ):
        raise VisualPermanentAdapterError("CONTENT_VISUALS_INVALID_MANIFEST")
    return assets


def _scene(asset: dict[str, object], style_prompt_suffix: str) -> str:
    prompt = str(asset.get("generation_prompt") or "")
    return prompt.removesuffix(style_prompt_suffix)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _result(
    run: Run,
    round_number: int,
    step_run: StepRun,
    manifest_id: int | None,
    image_ids: tuple[int, ...],
    replayed: bool,
) -> DraftRoundResult:
    return DraftRoundResult(
        run_id=_persisted_id(run.id, "run"),
        round_number=round_number,
        step_run_id=step_run.id,
        manifest_artifact_id=manifest_id,
        image_artifact_ids=image_ids,
        run_status=run.status,
        replayed=replayed,
    )


def _persisted_id(value: int | None, kind: str) -> int:
    if not isinstance(value, int):
        raise ValueError(f"{kind} must be persisted")
    return value
