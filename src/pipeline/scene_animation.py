"""Step A5a: turn each approved A3 image into a short clip with a local video model.

Runs on the ``gpu`` queue after A4 (the cuts need the spoken words) and before
A5. For the cuts of ``timeline_builder.scene_spans`` it decides the frames of
every clip (``scene_clips.scene_clip_frames``), asks the text model for the
motion of each scene once (``motion_generation``), and renders the clips through
a ``SceneVideoRenderer`` (``comfyui_runtime``). The result is a
``SceneClipsManifest`` and one MP4 per animated scene, published as artifacts
checked by SHA-256.

Time. One attempt never outlives its task timeout: a clip that does not fit in
what is left is not started. The attempt then publishes the clips it has, and
ends with the retryable ``CONTENT_SCENE_ANIMATION_BUDGET_EXHAUSTED``; the next
attempt finds them in the cache and continues. ``max_attempts`` caps how many
attempts a video may take, so one that never finishes fails closed.

Cache. Each clip is stored under a key made of the image SHA-256, the motion,
the seed, the frames, the format, the weight inventory and the graph version.
A clip already made and still matching its SHA-256 is never made again; the
motion plan is written once per run so retries ask for the same clips.

Failures. A clip the model fails on, or that runs past its own timeout, keeps
its still image (``generation_failed``) and the video goes on; the step fails
only when no scene could be animated. A runtime that cannot start (not enough
RAM, did not answer) is retryable up to ``_INFRA_FAILURE_LIMIT`` times; wrong
weights or versions fail at once.

Scope. A clip is never more usable than its image: the manifest carries the
``usage_scope`` of the A3 bundle.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast
from uuid import uuid4

import structlog
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.operations.retention_policy import resolve_retention_days
from src.pipeline.assembly import _load_assembly_context, caption_karaoke_words
from src.pipeline.comfyui_runtime import (
    ClipRenderRequest,
    SceneClipFailedError,
    SceneClipTimeoutError,
    SceneVideoMisconfiguredError,
    SceneVideoRenderer,
    SceneVideoUnavailableError,
)
from src.pipeline.motion_generation import MotionScene, MotionTimeError, write_scene_motions
from src.pipeline.scene_clips import (
    MAX_SCENE_CLIP_FRAMES,
    AnimatedScene,
    SceneClipsManifest,
    SceneEntry,
    StillReason,
    StillScene,
    scene_clip_frames,
    scene_clip_window,
)
from src.pipeline.text_model import TextModel, TextModelPermanentError, TextModelResult
from src.pipeline.timeline import milliseconds
from src.pipeline.timeline_builder import (
    TRANSITION_MS,
    SceneSpan,
    TimelineBuildError,
    TimelineImage,
    scene_cuts_sha256,
    scene_spans,
    timeline_images_from_manifest,
)
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
    MetricPoint,
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
    TaskOperationError,
    TaskStepSpec,
    execute_task_step,
)

log = structlog.get_logger(__name__)

SCENE_ANIMATION_STEP_NAME = "animate-scenes-a5a"
LTX23_QUALITY_TEST_BACKEND = "ltx23_quality_test"
SCENE_ANIMATION_BUDGET_EXHAUSTED = "CONTENT_SCENE_ANIMATION_BUDGET_EXHAUSTED"
SCENE_CLIPS_MANIFEST_ARTIFACT = "scene_clips_manifest"
SCENE_CLIP_ARTIFACT = "scene_clip_video"
_SOURCE = "content-engine:a5a"
_MANIFEST_RETENTION_DAYS = resolve_retention_days(SCENE_CLIPS_MANIFEST_ARTIFACT)
_CLIP_RETENTION_DAYS = resolve_retention_days(SCENE_CLIP_ARTIFACT)
# Retryable runtime failures (not the budget) after which the step gives up.
_INFRA_FAILURE_LIMIT = 3
# Kept at the end of every attempt to stop the runtime and publish the evidence.
_PUBLISH_RESERVE_SECONDS = 45.0
# The first clip after a start also loads about 40 GB of weights.
_MODEL_LOAD_SECONDS = 60.0
_ENERGY_QUANTUM = Decimal("0.0000000001")
_SECONDS_QUANTUM = Decimal("0.001")
_MAX_SIDE_FILE_BYTES = 256 * 1024

# Builds the renderer of one attempt in the given empty workspace.
RendererFactory = Callable[[Path], SceneVideoRenderer]


@dataclass(frozen=True, slots=True)
class SceneAnimationLimits:
    """Time limits of the step; every attempt has ``timeout_seconds`` in all."""

    timeout_seconds: float
    clip_timeout_seconds: float
    startup_timeout_seconds: float
    seconds_per_frame: float
    max_attempts: int
    # Longest text model call, so the motion plan is never asked too late to finish.
    text_call_seconds: float = 0.0

    def __post_init__(self) -> None:
        if self.timeout_seconds <= _PUBLISH_RESERVE_SECONDS:
            raise ValueError("the attempt timeout must exceed the publication reserve")
        if self.clip_timeout_seconds <= 0 or self.startup_timeout_seconds <= 0:
            raise ValueError("timeouts must be positive")
        if self.seconds_per_frame <= 0:
            raise ValueError("seconds_per_frame must be positive")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.text_call_seconds < 0:
            raise ValueError("text_call_seconds must not be negative")

    @property
    def shortest_useful_attempt_seconds(self) -> float:
        """An attempt shorter than this cannot start the runtime and render one long clip."""
        return (
            _PUBLISH_RESERVE_SECONDS
            + self.text_call_seconds
            + self.startup_timeout_seconds
            + _MODEL_LOAD_SECONDS
            + MAX_SCENE_CLIP_FRAMES * self.seconds_per_frame
        )


@dataclass(frozen=True, slots=True)
class SceneAnimationResult:
    """Observable identifiers of the A5a step."""

    run_id: int
    step_run_id: int | None
    manifest_artifact_id: int | None
    clip_artifact_ids: tuple[int, ...]
    run_status: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class _Scene:
    image: TimelineImage
    image_path: Path
    description: str
    span: SceneSpan
    frames: int | None

    @property
    def index(self) -> int:
        return self.span.index


@dataclass(frozen=True, slots=True)
class _Clip:
    index: int
    key: str
    path: Path
    sha256: str
    seconds: Decimal
    peak_vram_mib: int | None
    peak_ram_bytes: int | None


@dataclass(frozen=True, slots=True)
class _Motions:
    motions: tuple[str, ...]
    neutral: tuple[int, ...]
    text_model: str | None
    attempts: int
    written_now: bool


async def execute_scene_animation_step(
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
    renderer_factory: RendererFactory,
    motion_model: TextModel,
    limits: SceneAnimationLimits,
    gpu_power_watts: int,
    sleep: Sleeper = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> SceneAnimationResult:
    """Animate every scene of an A4-ready run and publish the clips and manifest.

    Never finalizes the run: A5 follows. A failed step fails the run.
    """
    if gpu_power_watts < 1:
        raise ValueError("gpu_power_watts must be positive")
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
    usage_scope = _usage_scope(context.visual_usage_scope)
    run_id = _persisted_id(run.id, "run")
    directory = scene_clips_directory(storage_root, run_id=run_id)
    step_key = f"{idempotency_key}:{SCENE_ANIMATION_STEP_NAME}"
    duration_ms = milliseconds(context.duration_seconds)

    async def operation(attempt: TaskAttemptContext) -> dict[str, object]:
        deadline = clock() + limits.timeout_seconds - _PUBLISH_RESERVE_SECONDS
        step_run = await session.get(StepRun, attempt.step_run_id)
        if step_run is None:
            raise ValueError("scene animation attempt was not persisted")
        try:
            scenes = await asyncio.to_thread(
                _plan_scenes,
                manifest_path=context.manifest_path,
                visual_paths=context.visual_paths,
                caption_path=context.caption_path,
                duration_ms=duration_ms,
            )
        except (OSError, UnicodeError, ValueError) as exc:
            raise PermanentTaskError("CONTENT_SCENE_ANIMATION_CUTS_INVALID") from exc
        spans = [scene.span for scene in scenes]
        try:
            motions = await asyncio.to_thread(
                _load_or_write_motions,
                directory / "motion-plan.json",
                scenes,
                motion_model,
                # The text model counts time on the monotonic clock.
                time.monotonic() + (deadline - clock()) - limits.text_call_seconds,
            )
        except MotionTimeError as exc:
            raise RetryableTaskError(SCENE_ANIMATION_BUDGET_EXHAUSTED) from exc
        except TextModelPermanentError as exc:
            raise PermanentTaskError("CONTENT_SCENE_ANIMATION_MOTION_MODEL_MISCONFIGURED") from exc
        except Exception as exc:
            raise await _runtime_failure(
                session,
                run_id=run_id,
                step_key=step_key,
                code="CONTENT_SCENE_ANIMATION_MOTION_MODEL_UNAVAILABLE",
            ) from exc

        render = _Rendering(
            scenes=scenes,
            motions=motions.motions,
            video_format=context.format,
            width=context.width,
            height=context.height,
            directory=directory,
            renderer_factory=renderer_factory,
            limits=limits,
            deadline=deadline,
            clock=clock,
        )
        failure: TaskOperationError | None = None
        try:
            await render.run()
        except SceneVideoMisconfiguredError as exc:
            failure = PermanentTaskError(exc.code)
        except SceneVideoUnavailableError as exc:
            failure = await _runtime_failure(
                session, run_id=run_id, step_key=step_key, code=exc.code
            )
        # Every finished clip is published, whatever stopped the attempt.
        await _publish_attempt(
            session,
            run=run,
            step_run=step_run,
            step_key=step_key,
            storage_root=storage_root,
            render=render,
            gpu_power_watts=gpu_power_watts,
            dimensions={
                "queue": QueueClass.GPU.value,
                "provider": render.identity["provider"],
                "graph_version": render.identity["graph_version"],
                "format": context.format,
            },
        )
        if failure is not None:
            raise failure
        if render.exhausted:
            if not render.progressed and not motions.written_now:
                # A whole attempt cannot fit the next clip: retrying would loop.
                raise PermanentTaskError("CONTENT_SCENE_ANIMATION_BUDGET_TOO_SHORT")
            log.info(
                "scene_animation_budget_exhausted",
                attempt=attempt.attempt,
                ready=len(render.clips),
                remaining=len(render.pending),
            )
            raise RetryableTaskError(SCENE_ANIMATION_BUDGET_EXHAUSTED)
        if not render.clips:
            raise PermanentTaskError("CONTENT_SCENE_ANIMATION_NO_SCENE_ANIMATED")
        try:
            manifest = SceneClipsManifest(
                schema_version=1,
                format=context.format,
                width=context.width,
                height=context.height,
                duration_ms=duration_ms,
                transition_ms=TRANSITION_MS,
                fps=25,
                cuts_sha256=scene_cuts_sha256(spans),
                provider=render.identity["provider"],
                model_id=render.identity["model_id"],
                model_inventory_sha256=render.identity["inventory_sha256"],
                graph_version=render.identity["graph_version"],
                license_id=render.identity["license_id"],
                usage_scope=usage_scope,
                scenes=render.entries(),
            )
        except ValidationError as exc:
            raise PermanentTaskError("CONTENT_SCENE_ANIMATION_MANIFEST_INVALID") from exc
        manifest_path = directory / "manifest.json"
        manifest_sha256 = await asyncio.to_thread(
            _write_json_atomic, manifest_path, manifest.model_dump(mode="json")
        )
        clip_seconds = [clip.seconds for clip in render.clips.values()]
        vram = [c.peak_vram_mib for c in render.clips.values() if c.peak_vram_mib is not None]
        ram = [c.peak_ram_bytes for c in render.clips.values() if c.peak_ram_bytes is not None]
        root = storage_root.resolve()
        return {
            "backend": LTX23_QUALITY_TEST_BACKEND,
            "provider": manifest.provider,
            "model_id": manifest.model_id,
            "graph_version": manifest.graph_version,
            "license_id": manifest.license_id,
            "usage_scope": usage_scope,
            "format": context.format,
            "cuts_sha256": manifest.cuts_sha256,
            "manifest_relative_path": manifest_path.relative_to(root).as_posix(),
            "manifest_sha256": manifest_sha256,
            "clips": [
                {
                    "index": clip.index,
                    "relative_path": clip.path.relative_to(root).as_posix(),
                    "sha256": clip.sha256,
                }
                for clip in sorted(render.clips.values(), key=lambda item: item.index)
            ],
            "animated_scenes": len(render.clips),
            "still_scenes": {str(index): reason for index, reason in sorted(render.stills.items())},
            "neutral_motion_scenes": list(motions.neutral),
            "motion_text_model": motions.text_model,
            "motion_attempts": motions.attempts,
            "gpu_seconds": str(sum(clip_seconds, Decimal(0)).quantize(_SECONDS_QUANTUM)),
            "peak_vram_mib": max(vram) if vram else None,
            "peak_ram_bytes": max(ram) if ram else None,
            "external_cost": "0",
        }

    task_result = await execute_task_step(
        session,
        run=run,
        spec=TaskStepSpec(
            name=SCENE_ANIMATION_STEP_NAME,
            queue=QueueClass.GPU,
            ordinal=5,
            idempotency_key=step_key,
            input_payload={
                **approval_input_payload,
                "audio_artifact_id": audio_artifact.id,
                "audio_artifact_sha256": audio_artifact.sha256,
                "visual_manifest_artifact_id": visual_manifest_artifact.id,
                "visual_manifest_artifact_sha256": visual_manifest_artifact.sha256,
                "visual_artifact_ids": [artifact.id for artifact in visual_artifacts],
                "caption_artifact_id": caption_artifact.id,
                "caption_artifact_sha256": caption_artifact.sha256,
                "backend": LTX23_QUALITY_TEST_BACKEND,
            },
        ),
        policy=RetryPolicy(
            max_attempts=limits.max_attempts,
            timeout_seconds=limits.timeout_seconds,
            initial_backoff_seconds=5.0,
            max_backoff_seconds=30.0,
        ),
        operation=operation,
        sleep=sleep,
    )
    step_run = await session.get(StepRun, task_result.step_run_ids[-1])
    if step_run is None:
        raise ValueError("scene animation step result was not persisted")
    if task_result.status is RunStatus.CANCELLED:
        return _result(run, step_run, None, (), task_result.replayed)
    if task_result.status is RunStatus.FAILED:
        if RunStatus(run.status) is RunStatus.RUNNING:
            await transition_run(
                session,
                run,
                RunStatus.FAILED,
                error=task_result.error,
                note="Content Engine A5a scene animation failed",
            )
        return _result(run, step_run, None, (), task_result.replayed)

    output = task_result.output_payload or {}
    try:
        async with session.begin_nested():
            manifest_artifact = (
                await register_local_artifact(
                    session,
                    run=run,
                    step_run=step_run,
                    storage_root=storage_root,
                    file_path=storage_root / str(output["manifest_relative_path"]),
                    idempotency_key=f"{step_key}:manifest",
                    artifact_type=SCENE_CLIPS_MANIFEST_ARTIFACT,
                    media_type="application/json",
                    origin=_SOURCE,
                    sensitivity=ArtifactSensitivity.INTERNAL,
                    retention_days=_MANIFEST_RETENTION_DAYS,
                    expected_sha256=str(output["manifest_sha256"]),
                )
            ).artifact
            clip_ids: list[int] = []
            for clip in cast(list[dict[str, Any]], output["clips"]):
                artifact = await session.scalar(
                    select(Artifact).where(
                        Artifact.run_id == run_id,
                        Artifact.idempotency_key
                        == _clip_artifact_key(step_key, int(clip["index"]), str(clip["sha256"])),
                    )
                )
                if artifact is None or artifact.sha256 != clip["sha256"]:
                    raise ValueError("a clip of the manifest was not published")
                clip_ids.append(_persisted_id(artifact.id, "artifact"))
            await _record_step_observations(
                session,
                run=run,
                step_run=step_run,
                step_key=step_key,
                output=output,
                gpu_power_watts=gpu_power_watts,
            )
    except Exception as exc:
        if RunStatus(run.status) is RunStatus.RUNNING:
            await transition_run(
                session,
                run,
                RunStatus.FAILED,
                error={
                    "code": "CONTENT_SCENE_ANIMATION_EVIDENCE_PERSIST_FAILED",
                    "exception_type": type(exc).__name__,
                    "retryable": True,
                    "timed_out": False,
                },
                note="Content Engine A5a evidence persistence failed",
            )
        return _result(run, step_run, None, (), task_result.replayed)
    return _result(
        run,
        step_run,
        _persisted_id(manifest_artifact.id, "artifact"),
        tuple(clip_ids),
        task_result.replayed,
    )


def scene_clips_directory(storage_root: Path, *, run_id: int) -> Path:
    """Where the clips, the motion plan and the manifest of one run are kept."""
    root = storage_root.resolve()
    if root == Path(root.anchor):
        raise ValueError("storage root must not be a filesystem root")
    return root / "content-engine" / f"run-{run_id}" / "scene-clips"


def scene_clip_seed(index: int, image_sha256: str, motion: str) -> int:
    """Stable seed per scene, so the same approved content renders the same clip."""
    return int(hashlib.sha256(f"{index}:{image_sha256}:{motion}".encode()).hexdigest()[:8], 16)


def scene_clip_cache_key(
    *,
    image_sha256: str,
    motion: str,
    seed: int,
    frames: int,
    video_format: str,
    inventory_sha256: str,
    graph_version: str,
) -> str:
    """Identity of one clip: what was asked and what made it."""
    canonical = json.dumps(
        {
            "frames": frames,
            "format": video_format,
            "graph_version": graph_version,
            "image_sha256": image_sha256,
            "model_inventory_sha256": inventory_sha256,
            "motion": motion,
            "seed": seed,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class _Rendering:
    """The clips of one attempt: cached, rendered now, failed or left for later."""

    def __init__(
        self,
        *,
        scenes: Sequence[_Scene],
        motions: Sequence[str],
        video_format: Literal["short", "long"],
        width: int,
        height: int,
        directory: Path,
        renderer_factory: RendererFactory,
        limits: SceneAnimationLimits,
        deadline: float,
        clock: Callable[[], float],
    ) -> None:
        self._scenes = scenes
        self._motions = motions
        self._format = video_format
        self._width = width
        self._height = height
        self._clips_dir = directory / "clips"
        self._failed_dir = directory / "failed"
        self._runtime_dir = directory / "runtime"
        self._factory = renderer_factory
        self._limits = limits
        self._deadline = deadline
        self._clock = clock
        self._renderer: SceneVideoRenderer | None = None
        self._workspace: Path | None = None
        self._loaded = False
        self.clips: dict[int, _Clip] = {}
        self.stills: dict[int, StillReason] = {}
        self.pending: list[_Scene] = []
        self.exhausted = False
        self.progressed = False
        self.identity: dict[str, str] = {}
        # GPU time of every render call of this attempt, and of the ones that failed.
        self.render_seconds = Decimal(0)
        self.failures: list[tuple[int, str, Decimal]] = []
        self._seeds: dict[int, int] = {}
        self._keys: dict[int, str] = {}

    async def run(self) -> None:
        renderer = self._new_renderer()
        try:
            self.identity = {
                "provider": renderer.provider,
                "model_id": renderer.model_id,
                "graph_version": renderer.graph_version,
                "license_id": renderer.license_id,
                "inventory_sha256": renderer.inventory_sha256,
            }
            await asyncio.to_thread(self._sort_scenes, renderer)
            for scene in list(self.pending):
                if not await self._render_one(renderer, scene):
                    self.exhausted = True
                    break
                self.pending.remove(scene)
        finally:
            # Synchronous on purpose: the tree is gone even if the attempt is cancelled.
            renderer.close()
            if self._workspace is not None:
                shutil.rmtree(self._workspace, ignore_errors=True)

    def entries(self) -> list[SceneEntry]:
        entries: list[SceneEntry] = []
        for scene in self._scenes:
            span = scene.span
            clip = self.clips.get(scene.index)
            if clip is None:
                entries.append(
                    StillScene(
                        kind="still",
                        index=scene.index,
                        image=scene.image.file_name,
                        image_sha256=scene.image.sha256,
                        start_ms=span.start_ms,
                        end_ms=span.end_ms,
                        reason=self.stills.get(scene.index, "generation_failed"),
                    )
                )
                continue
            entries.append(
                AnimatedScene(
                    kind="clip",
                    index=scene.index,
                    image=scene.image.file_name,
                    image_sha256=scene.image.sha256,
                    start_ms=span.start_ms,
                    end_ms=span.end_ms,
                    motion=self._motions[scene.index - 1],
                    seed=self._seeds[scene.index],
                    frames=cast(int, scene.frames),
                    video=clip.path.name,
                    video_sha256=clip.sha256,
                )
            )
        return entries

    def _new_renderer(self) -> SceneVideoRenderer:
        self._runtime_dir.mkdir(parents=True, exist_ok=True)
        self._workspace = self._runtime_dir / f"attempt-{uuid4().hex[:12]}"
        self._workspace.mkdir()
        return self._factory(self._workspace)

    def _sort_scenes(self, renderer: SceneVideoRenderer) -> None:
        """Split the scenes into still, cached, failed before, and still to render."""
        self._clips_dir.mkdir(parents=True, exist_ok=True)
        self._failed_dir.mkdir(parents=True, exist_ok=True)
        for scene in self._scenes:
            if scene.frames is None:
                self.stills[scene.index] = "too_long"
                continue
            motion = self._motions[scene.index - 1]
            seed = scene_clip_seed(scene.index, scene.image.sha256, motion)
            key = scene_clip_cache_key(
                image_sha256=scene.image.sha256,
                motion=motion,
                seed=seed,
                frames=scene.frames,
                video_format=self._format,
                inventory_sha256=renderer.inventory_sha256,
                graph_version=renderer.graph_version,
            )
            self._seeds[scene.index] = seed
            self._keys[scene.index] = key
            cached = self._cached_clip(scene.index, key)
            if cached is not None:
                self.clips[scene.index] = cached
            elif (self._failed_dir / f"{key}.json").is_file():
                self.stills[scene.index] = "generation_failed"
            else:
                self.pending.append(scene)

    async def _render_one(self, renderer: SceneVideoRenderer, scene: _Scene) -> bool:
        """Render one clip; False when it does not fit in the time that is left."""
        frames = cast(int, scene.frames)
        estimate = frames * self._limits.seconds_per_frame
        if not renderer.running:
            needed = self._limits.startup_timeout_seconds + estimate + _MODEL_LOAD_SECONDS
            if self._remaining() < needed:
                return False
            await asyncio.to_thread(
                renderer.start, timeout_seconds=self._limits.startup_timeout_seconds
            )
            self._loaded = False
        if not self._loaded:
            estimate += _MODEL_LOAD_SECONDS
        remaining = self._remaining()
        if remaining < estimate:
            return False
        timeout = min(self._limits.clip_timeout_seconds, remaining)
        key = self._keys[scene.index]
        # Rendered under a temporary name; the final name carries the SHA-256, so a
        # clip made again never overwrites one an earlier attempt published.
        output = self._clips_dir / f".scene-{scene.index:03d}-{key[:16]}.{uuid4().hex}.partial.mp4"
        request = ClipRenderRequest(
            index=scene.index,
            image_path=scene.image_path,
            image_sha256=scene.image.sha256,
            motion=self._motions[scene.index - 1],
            seed=self._seeds[scene.index],
            frames=frames,
            format=self._format,
            width=self._width,
            height=self._height,
            output_path=output,
        )
        began = self._clock()
        try:
            result = await asyncio.to_thread(renderer.render, request, timeout_seconds=timeout)
        except (SceneClipTimeoutError, SceneClipFailedError) as exc:
            output.unlink(missing_ok=True)
            spent = self._spent_since(began)
            if isinstance(exc, SceneClipTimeoutError):
                self._loaded = False
                if timeout < self._limits.clip_timeout_seconds:
                    # Stopped by the attempt's budget, not by its own limit: try again later.
                    return False
            self.failures.append((scene.index, exc.code, spent))
            await asyncio.to_thread(self._mark_failed, scene.index, key, exc.code)
            return True
        except BaseException:
            output.unlink(missing_ok=True)
            self._spent_since(began)
            raise
        self._spent_since(began)
        self._loaded = True
        clip = await asyncio.to_thread(
            self._store_clip,
            scene.index,
            key,
            output,
            Decimal(str(result.seconds)).quantize(_SECONDS_QUANTUM),
            result.peak_vram_mib,
            result.peak_ram_bytes,
        )
        if clip is None:
            await asyncio.to_thread(
                self._mark_failed, scene.index, key, "CONTENT_SCENE_ANIMATION_CLIP_INVALID"
            )
        else:
            self.clips[scene.index] = clip
        self.progressed = True
        return True

    def _remaining(self) -> float:
        return self._deadline - self._clock()

    def _spent_since(self, began: float) -> Decimal:
        spent = Decimal(str(max(0.0, self._clock() - began))).quantize(_SECONDS_QUANTUM)
        self.render_seconds += spent
        return spent

    def _mark_failed(self, index: int, key: str, code: str) -> None:
        log.warning("scene_animation_scene_kept_still", index=index, code=code)
        self.stills[index] = "generation_failed"
        self.progressed = True
        _write_json_atomic(self._failed_dir / f"{key}.json", {"index": index, "code": code})

    def _store_clip(
        self,
        index: int,
        key: str,
        path: Path,
        seconds: Decimal,
        peak_vram_mib: int | None,
        peak_ram_bytes: int | None,
    ) -> _Clip | None:
        if not _looks_like_mp4(path):
            path.unlink(missing_ok=True)
            return None
        with path.open("r+b") as handle:
            os.fsync(handle.fileno())
        sha256 = _sha256_file(path)
        final = self._clips_dir / f"scene-{index:03d}-{key[:16]}-{sha256[:16]}.mp4"
        os.replace(path, final)
        path = final
        _write_json_atomic(
            self._record_path(index, key),
            {
                "key": key,
                "video": final.name,
                "sha256": sha256,
                "seconds": str(seconds),
                "peak_vram_mib": peak_vram_mib,
                "peak_ram_bytes": peak_ram_bytes,
            },
        )
        return _Clip(
            index=index,
            key=key,
            path=path,
            sha256=sha256,
            seconds=seconds,
            peak_vram_mib=peak_vram_mib,
            peak_ram_bytes=peak_ram_bytes,
        )

    def _record_path(self, index: int, key: str) -> Path:
        return self._clips_dir / f"scene-{index:03d}-{key[:16]}.json"

    def _cached_clip(self, index: int, key: str) -> _Clip | None:
        """A clip made by an earlier attempt, only if it still matches its record.

        A clip that no longer matches is made again under a new name; the changed
        file is left in place, since an earlier attempt may have published it.
        """
        record_path = self._record_path(index, key)
        record = _read_json(record_path)
        if record is None:
            return None
        sha256, video = record.get("sha256"), record.get("video")
        expected = f"scene-{index:03d}-{key[:16]}-"
        path = self._clips_dir / str(video)
        if (
            record.get("key") != key
            or not isinstance(sha256, str)
            or not isinstance(video, str)
            or video != f"{expected}{sha256[:16]}.mp4"
            or not path.is_file()
            or _sha256_file(path) != sha256
        ):
            log.warning("scene_animation_cached_clip_discarded", index=index)
            record_path.unlink(missing_ok=True)
            return None
        vram, ram = record.get("peak_vram_mib"), record.get("peak_ram_bytes")
        return _Clip(
            index=index,
            key=key,
            path=path,
            sha256=sha256,
            seconds=Decimal(str(record.get("seconds", "0"))),
            peak_vram_mib=vram if isinstance(vram, int) else None,
            peak_ram_bytes=ram if isinstance(ram, int) else None,
        )


def _plan_scenes(
    *,
    manifest_path: Path,
    visual_paths: Sequence[Path],
    caption_path: Path,
    duration_ms: int,
) -> list[_Scene]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("the visual manifest is not an object")
    images = timeline_images_from_manifest(manifest)
    if [path.name for path in visual_paths] != [image.file_name for image in images]:
        raise ValueError("the visual artifacts do not match the manifest")
    descriptions = {
        asset.get("index"): str(asset.get("generation_prompt") or "")
        for asset in manifest["assets"]
        if isinstance(asset, dict)
    }
    words = caption_karaoke_words(caption_path.read_text(encoding="utf-8"), duration_ms=duration_ms)
    try:
        spans = scene_spans(images, words, duration_ms=duration_ms)
    except TimelineBuildError as exc:
        raise ValueError(str(exc)) from exc
    scenes: list[_Scene] = []
    for image, path, span in zip(images, visual_paths, spans, strict=True):
        start, end = scene_clip_window(span, duration_ms=duration_ms, transition_ms=TRANSITION_MS)
        scenes.append(
            _Scene(
                image=image,
                image_path=path,
                description=descriptions.get(span.index, ""),
                span=span,
                frames=scene_clip_frames(end - start),
            )
        )
    return scenes


def _load_or_write_motions(
    path: Path,
    scenes: Sequence[_Scene],
    model: TextModel,
    deadline: float,
) -> _Motions:
    """The motions written by an earlier attempt for the same images, or new ones."""
    images = [scene.image.sha256 for scene in scenes]
    saved = _read_json(path)
    if saved is not None and saved.get("images") == images:
        motions = saved.get("motions")
        neutral = saved.get("neutral")
        if (
            isinstance(motions, list)
            and len(motions) == len(scenes)
            and all(isinstance(item, str) for item in motions)
            and isinstance(neutral, list)
        ):
            text_model = saved.get("text_model")
            attempts = saved.get("attempts")
            return _Motions(
                motions=tuple(motions),
                neutral=tuple(int(item) for item in neutral),
                text_model=text_model if isinstance(text_model, str) else None,
                attempts=attempts if isinstance(attempts, int) else 0,
                written_now=False,
            )
    written = write_scene_motions(
        model,
        [
            MotionScene(
                index=scene.index,
                description=scene.description,
                narration_text=scene.image.narration_text,
            )
            for scene in scenes
        ],
        deadline=deadline,
    )
    label = _text_model_label(written.result) if written.result is not None else None
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(
        path,
        {
            "schema_version": 1,
            "images": images,
            "motions": list(written.motions),
            "neutral": list(written.neutral),
            "text_model": label,
            "attempts": written.attempts,
        },
    )
    return _Motions(
        motions=written.motions,
        neutral=written.neutral,
        text_model=label,
        attempts=written.attempts,
        written_now=True,
    )


async def _runtime_failure(
    session: AsyncSession,
    *,
    run_id: int,
    step_key: str,
    code: str,
) -> TaskOperationError:
    """Retryable, unless the runtime already failed this way too many times.

    Attempts that stopped because the budget ran out are not counted.
    """
    attempts = (
        await session.scalars(
            select(StepRun).where(
                StepRun.run_id == run_id,
                StepRun.idempotency_key == step_key,
                StepRun.status == RunStatus.FAILED.value,
            )
        )
    ).all()
    earlier = sum(
        1
        for attempt in attempts
        if (attempt.error or {}).get("code") != SCENE_ANIMATION_BUDGET_EXHAUSTED
    )
    if earlier + 1 >= _INFRA_FAILURE_LIMIT:
        return PermanentTaskError(code)
    return RetryableTaskError(code)


async def _publish_attempt(
    session: AsyncSession,
    *,
    run: Run,
    step_run: StepRun,
    step_key: str,
    storage_root: Path,
    render: _Rendering,
    gpu_power_watts: int,
    dimensions: dict[str, object],
) -> None:
    """Register every finished clip and the GPU time this attempt spent.

    The observations of a clip are recorded once per run, from its cache record,
    whichever attempt publishes it first: also a clip whose own attempt stopped
    before publishing it. Failed and abandoned renders are recorded per attempt.
    """
    run_id = _persisted_id(run.id, "run")
    automation = await session.get(Automation, run.automation_id)
    if automation is None:
        raise ValueError("scene animation automation does not exist")

    async def observe(
        key: str,
        name: str,
        kind: MetricKind,
        value: Decimal | int,
        unit: str,
        extra: dict[str, object],
    ) -> None:
        recorded = await session.scalar(
            select(MetricPoint.id).where(
                MetricPoint.automation_id == automation.id,
                MetricPoint.idempotency_key == key,
            )
        )
        if recorded is not None:
            return
        await record_metric_point(
            session,
            automation=automation,
            run=run,
            step_run=step_run,
            idempotency_key=key,
            name=name,
            kind=kind,
            value=value,
            unit=unit,
            source=_SOURCE,
            confidence="0.5" if name.endswith("_kwh") else None,
            dimensions={**dimensions, **extra},
        )

    async with session.begin_nested():
        for clip in sorted(render.clips.values(), key=lambda item: item.index):
            artifact_key = _clip_artifact_key(step_key, clip.index, clip.sha256)
            existing = await session.scalar(
                select(Artifact).where(
                    Artifact.run_id == run_id, Artifact.idempotency_key == artifact_key
                )
            )
            if existing is None:
                await register_local_artifact(
                    session,
                    run=run,
                    step_run=step_run,
                    storage_root=storage_root,
                    file_path=clip.path,
                    idempotency_key=artifact_key,
                    artifact_type=SCENE_CLIP_ARTIFACT,
                    media_type="video/mp4",
                    origin=_SOURCE,
                    sensitivity=ArtifactSensitivity.INTERNAL,
                    retention_days=_CLIP_RETENTION_DAYS,
                    expected_sha256=clip.sha256,
                )
            elif existing.sha256 != clip.sha256:
                raise ValueError("a published clip changed")
            prefix = f"run-{run_id}:a5a:clip-{clip.index:03d}-{clip.sha256[:16]}"
            for name, kind, value, unit in _clip_observations(clip, gpu_power_watts):
                await observe(
                    f"{prefix}:{name}", name, kind, value, unit, {"scene_index": clip.index}
                )
        attempt = f"{step_key}:attempt-{step_run.attempt}"
        for index, code, seconds in render.failures:
            await observe(
                f"{attempt}:scene-{index:03d}:failed-gpu-seconds",
                "content_scene_clip_failed_gpu_seconds",
                MetricKind.DURATION,
                seconds,
                "seconds",
                {"scene_index": index, "code": code},
            )
        if render.render_seconds > 0:
            for name, kind, value, unit in (
                (
                    "content_scene_animation_attempt_gpu_seconds",
                    MetricKind.DURATION,
                    render.render_seconds,
                    "seconds",
                ),
                (
                    "content_scene_animation_attempt_energy_estimate_kwh",
                    MetricKind.GAUGE,
                    _energy_kwh(render.render_seconds, gpu_power_watts),
                    "kWh",
                ),
            ):
                await observe(
                    f"{attempt}:{name}", name, kind, value, unit, {"attempt": step_run.attempt}
                )


def _clip_observations(
    clip: _Clip, gpu_power_watts: int
) -> list[tuple[str, MetricKind, Decimal | int, str]]:
    observations: list[tuple[str, MetricKind, Decimal | int, str]] = [
        ("content_scene_clip_gpu_seconds", MetricKind.DURATION, clip.seconds, "seconds"),
        (
            "content_scene_clip_energy_estimate_kwh",
            MetricKind.GAUGE,
            _energy_kwh(clip.seconds, gpu_power_watts),
            "kWh",
        ),
    ]
    if clip.peak_vram_mib is not None:
        observations.append(
            ("content_scene_clip_peak_vram_mib", MetricKind.GAUGE, clip.peak_vram_mib, "MiB")
        )
    if clip.peak_ram_bytes is not None:
        observations.append(
            ("content_scene_clip_peak_ram_bytes", MetricKind.GAUGE, clip.peak_ram_bytes, "bytes")
        )
    return observations


async def _record_step_observations(
    session: AsyncSession,
    *,
    run: Run,
    step_run: StepRun,
    step_key: str,
    output: dict[str, object],
    gpu_power_watts: int,
) -> None:
    automation = await session.get(Automation, run.automation_id)
    if automation is None:
        raise ValueError("scene animation automation does not exist")
    gpu_seconds = Decimal(str(output["gpu_seconds"]))
    still = output["still_scenes"]
    dimensions: dict[str, object] = {
        "queue": QueueClass.GPU.value,
        "backend": LTX23_QUALITY_TEST_BACKEND,
        "provider": str(output["provider"]),
        "graph_version": str(output["graph_version"]),
        "format": str(output["format"]),
    }
    observations: list[tuple[str, MetricKind, Decimal | int, str]] = [
        ("content_scene_animation_gpu_seconds", MetricKind.DURATION, gpu_seconds, "seconds"),
        (
            "content_scene_animation_animated_scenes",
            MetricKind.COUNTER,
            int(str(output["animated_scenes"])),
            "scenes",
        ),
        (
            "content_scene_animation_still_scenes",
            MetricKind.COUNTER,
            len(still) if isinstance(still, dict) else 0,
            "scenes",
        ),
        (
            "content_scene_animation_energy_estimate_kwh",
            MetricKind.GAUGE,
            _energy_kwh(gpu_seconds, gpu_power_watts),
            "kWh",
        ),
    ]
    peak_vram, peak_ram = output.get("peak_vram_mib"), output.get("peak_ram_bytes")
    if isinstance(peak_vram, int):
        observations.append(
            ("content_scene_animation_peak_vram_mib", MetricKind.GAUGE, peak_vram, "MiB")
        )
    if isinstance(peak_ram, int):
        observations.append(
            ("content_scene_animation_peak_ram_bytes", MetricKind.GAUGE, peak_ram, "bytes")
        )
    for name, kind, value, unit in observations:
        await record_metric_point(
            session,
            automation=automation,
            run=run,
            step_run=step_run,
            idempotency_key=f"{step_key}:{name}",
            name=name,
            kind=kind,
            value=value,
            unit=unit,
            source=_SOURCE,
            confidence="0.5" if name.endswith("_kwh") else None,
            dimensions=dimensions,
        )
    await record_ledger_entry(
        session,
        automation=automation,
        run=run,
        step_run=step_run,
        idempotency_key=f"{step_key}:external-api-cost",
        entry_type=LedgerEntryType.COST,
        category="external_api",
        amount=0,
        currency="BRL",
        source=_SOURCE,
        confidence=1,
    )


def _clip_artifact_key(step_key: str, index: int, sha256: str) -> str:
    return f"{step_key}:clip-{index:03d}-{sha256[:16]}"


def _energy_kwh(seconds: Decimal, gpu_power_watts: int) -> Decimal:
    energy = (seconds / Decimal(3600)) * (Decimal(gpu_power_watts) / Decimal(1000))
    return energy.quantize(_ENERGY_QUANTUM)


def _usage_scope(value: str | None) -> Literal["quality-test-only"] | None:
    if value is None:
        return None
    if value == "quality-test-only":
        return "quality-test-only"
    raise ValueError("the A3 bundle has an unknown usage scope")


def _text_model_label(result: TextModelResult) -> str:
    label = f"{result.provider}:{result.model_id}"
    if result.fallback_from is not None:
        label += f" (reserve after {result.fallback_from} limit)"
    return label


def _looks_like_mp4(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            header = handle.read(12)
    except OSError:
        return False
    return len(header) == 12 and header[4:8] == b"ftyp"


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        encoded = path.read_bytes()
        if len(encoded) > _MAX_SIDE_FILE_BYTES:
            return None
        decoded = json.loads(encoded.decode("utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> str:
    """Write a JSON file atomically and return its SHA-256."""
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return hashlib.sha256(encoded).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _result(
    run: Run,
    step_run: StepRun,
    manifest_id: int | None,
    clip_ids: tuple[int, ...],
    replayed: bool,
) -> SceneAnimationResult:
    return SceneAnimationResult(
        run_id=_persisted_id(run.id, "run"),
        step_run_id=step_run.id,
        manifest_artifact_id=manifest_id,
        clip_artifact_ids=clip_ids,
        run_status=run.status,
        replayed=replayed,
    )


def _persisted_id(value: int | None, kind: str) -> int:
    if not isinstance(value, int):
        raise ValueError(f"{kind} must be persisted")
    return value
