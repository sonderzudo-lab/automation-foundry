"""Step A5a: animated scenes with a fake video runtime, a fake clock and a real database."""

from __future__ import annotations

import hashlib
import json
import wave
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.config import Settings
from src.core.database import Base
from src.pipeline import a1_executor
from src.pipeline.a1_executor import execute_content_script_run
from src.pipeline.captions import execute_approved_caption_step
from src.pipeline.comfyui_runtime import (
    ClipRenderRequest,
    ClipRenderResult,
    SceneClipFailedError,
    SceneClipTimeoutError,
    SceneVideoError,
    SceneVideoMisconfiguredError,
    SceneVideoUnavailableError,
)
from src.pipeline.scene_animation import (
    SCENE_ANIMATION_BUDGET_EXHAUSTED,
    SCENE_ANIMATION_STEP_NAME,
    SceneAnimationLimits,
    SceneAnimationResult,
    execute_scene_animation_step,
    scene_clips_directory,
)
from src.pipeline.scene_clips import SceneClipsManifest
from src.pipeline.text_model import TextModelProviderName, TextModelRequest, TextModelResult
from src.pipeline.tts import TTSAdapterResult, execute_approved_tts_step
from src.pipeline.visuals import VisualAssetResult, VisualRequest, execute_approved_visual_step
from src.platform.approval_service import decide_approval
from src.platform.models import (
    Approval,
    ApprovalStatus,
    Artifact,
    LedgerEntry,
    MetricPoint,
    QueueClass,
    Run,
    RunStatus,
    StepRun,
)
from tests.pipeline.test_captions import _caption_adapter, _script, _visual_adapter

_MOTIONS = [
    "The adult slowly lifts the folded shirt from the bed while the camera pushes in",
    "Steam rises gently from the cup on the table as the camera pans to the left",
    "Leaves move softly in the wind outside the window while the camera holds still",
]
_AUDIO_SECONDS = 15


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as database_session:
        yield database_session
    await engine.dispose()


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class _TextModel:
    provider: TextModelProviderName = "ollama"
    model_id = "qwen3:14b"

    def __init__(self) -> None:
        self.requests: list[TextModelRequest] = []

    def complete(self, request: TextModelRequest) -> TextModelResult:
        self.requests.append(request)
        count = request.user.count("Scene ")
        answer = {
            "schema_version": 1,
            "scenes": [
                {"index": index, "motion": _MOTIONS[(index - 1) % len(_MOTIONS)]}
                for index in range(1, count + 1)
            ],
        }
        return TextModelResult(
            text=json.dumps(answer),
            provider=self.provider,
            model_id=self.model_id,
            duration_seconds=Decimal("1"),
        )


@dataclass
class _Renderer:
    """A video runtime that takes ``seconds`` of the fake clock per clip."""

    clock: _Clock
    outcomes: dict[int, str] = field(default_factory=dict)
    seconds: float = 100.0
    start_error: SceneVideoError | None = None
    starts: int = 0
    closes: int = 0
    rendered: list[int] = field(default_factory=list)
    requests: list[ClipRenderRequest] = field(default_factory=list)
    workspaces: list[Path] = field(default_factory=list)
    is_running: bool = False

    provider = "local-comfyui-ltx23"
    model_id = "fake-ltx23@test"
    graph_version = "fake-graph-v1"
    license_id = "LTX-2 Community License Agreement (test)"
    inventory_sha256 = "c" * 64

    @property
    def running(self) -> bool:
        return self.is_running

    def factory(self, workspace: Path) -> _Renderer:
        assert workspace.is_dir() and not any(workspace.iterdir())
        self.workspaces.append(workspace)
        return self

    def start(self, *, timeout_seconds: float) -> None:
        self.starts += 1
        if self.start_error is not None:
            raise self.start_error
        self.clock.now += 5
        self.is_running = True

    def render(self, request: ClipRenderRequest, *, timeout_seconds: float) -> ClipRenderResult:
        assert self.is_running
        self.rendered.append(request.index)
        self.requests.append(request)
        outcome = self.outcomes.get(request.index, "ok")
        if outcome == "fail":
            raise SceneClipFailedError("CONTENT_SCENE_ANIMATION_CLIP_FAILED")
        if outcome == "timeout":
            self.clock.now += timeout_seconds
            self.is_running = False
            raise SceneClipTimeoutError("CONTENT_SCENE_ANIMATION_CLIP_TIMEOUT")
        self.clock.now += self.seconds
        request.output_path.write_bytes(
            b"\x00\x00\x00\x18ftypisom" + f"{request.index}:{request.seed}".encode()
        )
        return ClipRenderResult(
            seconds=self.seconds, peak_vram_mib=20_000, peak_ram_bytes=50 * 1024**3
        )

    def close(self) -> None:
        self.closes += 1
        self.is_running = False


def _limits(timeout: float = 1_000.0, *, max_attempts: int = 6) -> SceneAnimationLimits:
    return SceneAnimationLimits(
        timeout_seconds=timeout,
        clip_timeout_seconds=300,
        startup_timeout_seconds=10,
        seconds_per_frame=0.01,
        max_attempts=max_attempts,
    )


def _write_long_wav(
    _text: str, destination: Path, voice_id: str, language_code: str
) -> TTSAdapterResult:
    with wave.open(str(destination), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24_000)
        audio.writeframes(b"\x00\x00" * 24_000 * _AUDIO_SECONDS)
    return TTSAdapterResult(
        backend="fake-local",
        model_id="fake-tts-v1",
        voice_id=voice_id,
        language_code=language_code,
        license_id="TEST-ONLY",
    )


def _quality_test_visuals(
    requests: tuple[VisualRequest, ...], destination: Path
) -> tuple[VisualAssetResult, ...]:
    return tuple(
        VisualAssetResult(
            file_name=result.file_name,
            source_type="generated",
            provider="local-flux2-klein",
            model_id="fake-flux2",
            source_id=result.source_id,
            source_uri=result.source_uri,
            license_id="Apache-2.0; scope=quality-test-only",
            commercial_use=False,
            attribution="teste de qualidade",
            generation_prompt=f"An adult in a calm room, scene {index}",
            seed=index,
        )
        for index, result in enumerate(_visual_adapter(requests, destination), start=1)
    )


@dataclass(frozen=True)
class _Evidence:
    run: Run
    approval: Approval
    approval_input: dict[str, object]
    audio: Artifact
    manifest: Artifact
    visuals: tuple[Artifact, ...]
    caption: Artifact


async def _after_a4(
    session: AsyncSession,
    storage_root: Path,
    *,
    key: str,
    quality_test: bool = False,
) -> _Evidence:
    generated = await execute_content_script_run(
        session,
        idempotency_key=key,
        input_payload={
            "topic": "Sono e memória",
            "persona": "Ciência acessível",
            "format": "short",
            "recent_openings": [],
        },
        script_generator=lambda *_args: _script(),
        storage_root=storage_root,
    )
    run = await session.get(Run, generated.run_id)
    approval = await session.get(Approval, generated.approval_id)
    script = await session.get(Artifact, generated.artifact_id)
    assert run is not None and approval is not None and script is not None
    await decide_approval(
        session,
        approval=approval,
        decision=ApprovalStatus.APPROVED,
        actor="local-owner",
        reason="roteiro revisado",
    )
    approval_input: dict[str, object] = {
        "schema_version": 1,
        "run_id": run.id,
        "step_run_id": generated.step_run_id,
        "artifact_id": script.id,
        "artifact_sha256": script.sha256,
        "narration_sha256": hashlib.sha256(_script().narration.encode("utf-8")).hexdigest(),
    }
    tts = await execute_approved_tts_step(
        session,
        run=run,
        approval=approval,
        approval_input_payload=approval_input,
        source_artifact=script,
        narration=_script().narration,
        idempotency_key=key,
        storage_root=storage_root,
        synthesizer=_write_long_wav,
        voice_id="pf_test",
        language_code="p",
        gpu_power_watts=400,
        timeout_seconds=5,
        finalize_run=False,
    )
    audio = await session.get(Artifact, tts.artifact_id)
    assert audio is not None
    visual_result = await execute_approved_visual_step(
        session,
        run=run,
        approval=approval,
        approval_input_payload=approval_input,
        script_artifact=script,
        audio_artifact=audio,
        idempotency_key=key,
        storage_root=storage_root,
        adapter=_quality_test_visuals if quality_test else _visual_adapter,
        allow_quality_test_assets=quality_test,
        queue=QueueClass.GPU,
        gpu_power_watts=400,
        timeout_seconds=5,
        finalize_run=False,
    )
    manifest = await session.get(Artifact, visual_result.manifest_artifact_id)
    visuals = tuple(
        [
            artifact
            for artifact_id in visual_result.visual_artifact_ids
            if (artifact := await session.get(Artifact, artifact_id)) is not None
        ]
    )
    assert manifest is not None and len(visuals) == 3
    caption_result = await execute_approved_caption_step(
        session,
        run=run,
        approval=approval,
        approval_input_payload=approval_input,
        audio_artifact=audio,
        narration=_script().narration,
        idempotency_key=key,
        storage_root=storage_root,
        adapter=_caption_adapter,
        language_code="pt",
        gpu_power_watts=400,
        timeout_seconds=5,
        visual_manifest_artifact_id=manifest.id,
        visual_artifact_ids=tuple(artifact.id for artifact in visuals if artifact.id),
        finalize_run=False,
    )
    caption = await session.get(Artifact, caption_result.artifact_id)
    assert caption is not None and run.status == RunStatus.RUNNING.value
    return _Evidence(run, approval, approval_input, audio, manifest, visuals, caption)


async def _animate(
    session: AsyncSession,
    evidence: _Evidence,
    storage_root: Path,
    renderer: _Renderer,
    *,
    limits: SceneAnimationLimits | None = None,
    model: _TextModel | None = None,
    key: str | None = None,
) -> SceneAnimationResult:
    async def no_sleep(_seconds: float) -> None:
        return None

    return await execute_scene_animation_step(
        session,
        run=evidence.run,
        approval=evidence.approval,
        approval_input_payload=evidence.approval_input,
        audio_artifact=evidence.audio,
        visual_manifest_artifact=evidence.manifest,
        visual_artifacts=evidence.visuals,
        caption_artifact=evidence.caption,
        idempotency_key=key or evidence.run.idempotency_key,
        storage_root=storage_root,
        renderer_factory=renderer.factory,
        motion_model=model or _TextModel(),
        limits=limits or _limits(),
        gpu_power_watts=400,
        sleep=no_sleep,
        clock=renderer.clock,
    )


def _manifest(storage_root: Path, run: Run) -> SceneClipsManifest:
    assert run.id is not None
    path = scene_clips_directory(storage_root, run_id=run.id) / "manifest.json"
    return SceneClipsManifest.model_validate_json(path.read_text(encoding="utf-8"))


async def _attempts(session: AsyncSession, run: Run) -> list[StepRun]:
    return list(
        (
            await session.scalars(
                select(StepRun)
                .where(StepRun.run_id == run.id, StepRun.name == SCENE_ANIMATION_STEP_NAME)
                .order_by(StepRun.attempt)
            )
        ).all()
    )


async def test_every_scene_that_fits_becomes_a_published_clip(
    session: AsyncSession, tmp_path: Path
) -> None:
    storage = tmp_path / "storage"
    evidence = await _after_a4(session, storage, key="a5a-happy")
    renderer = _Renderer(clock=_Clock())
    model = _TextModel()

    result = await _animate(session, evidence, storage, renderer, model=model)

    manifest = _manifest(storage, evidence.run)
    kinds = [scene.kind for scene in manifest.scenes]
    assert kinds == ["clip", "clip", "still"]
    still = manifest.scenes[2]
    assert still.kind == "still" and still.reason == "too_long"
    assert manifest.scenes[-1].end_ms == _AUDIO_SECONDS * 1000
    assert manifest.usage_scope is None
    assert manifest.provider == "local-comfyui-ltx23"
    assert manifest.model_inventory_sha256 == "c" * 64
    assert manifest.graph_version == "fake-graph-v1"
    assert renderer.rendered == [1, 2] and renderer.starts == 1
    assert renderer.closes == 1
    # The motions come from the scene descriptions and the narrated stretches.
    assert len(model.requests) == 1 and "Narration:" in model.requests[0].user
    first = renderer.requests[0]
    assert first.motion == _MOTIONS[0] and first.frames == manifest.scenes[0].frames  # type: ignore[union-attr]
    assert (first.width, first.height, first.format) == (1080, 1920, "short")

    step = await session.get(StepRun, result.step_run_id)
    assert step is not None and step.queue == "gpu" and step.status == "succeeded"
    output = step.output_payload or {}
    assert output["animated_scenes"] == 2 and output["still_scenes"] == {"3": "too_long"}
    assert output["gpu_seconds"] == "200.000" and output["peak_vram_mib"] == 20_000
    assert evidence.run.status == RunStatus.RUNNING.value

    manifest_artifact = await session.get(Artifact, result.manifest_artifact_id)
    assert manifest_artifact is not None
    assert manifest_artifact.artifact_type == "scene_clips_manifest"
    clips = [await session.get(Artifact, artifact_id) for artifact_id in result.clip_artifact_ids]
    assert [clip.artifact_type for clip in clips if clip] == ["scene_clip_video"] * 2
    for clip, scene in zip(clips, manifest.scenes[:2], strict=True):
        assert clip is not None and scene.kind == "clip"
        assert clip.sha256 == scene.video_sha256
        assert clip.relative_path.endswith(scene.video)
        assert clip.media_type == "video/mp4"

    names = {
        point.name
        for point in (await session.scalars(select(MetricPoint))).all()
        if point.name.startswith("content_scene_")
    }
    assert {
        "content_scene_clip_gpu_seconds",
        "content_scene_clip_peak_vram_mib",
        "content_scene_clip_peak_ram_bytes",
        "content_scene_clip_energy_estimate_kwh",
        "content_scene_animation_gpu_seconds",
        "content_scene_animation_animated_scenes",
        "content_scene_animation_still_scenes",
        "content_scene_animation_energy_estimate_kwh",
        "content_scene_animation_peak_vram_mib",
        "content_scene_animation_peak_ram_bytes",
    } <= names
    energy = await session.scalar(
        select(MetricPoint).where(MetricPoint.name == "content_scene_animation_energy_estimate_kwh")
    )
    assert energy is not None and energy.value == Decimal("0.0222222222")
    ledger = await session.scalar(
        select(LedgerEntry).where(LedgerEntry.idempotency_key.like("%a5a%external-api-cost"))
    )
    assert ledger is not None and ledger.amount == 0


async def test_ready_clips_are_reused_and_a_changed_clip_is_made_again(
    session: AsyncSession, tmp_path: Path
) -> None:
    storage = tmp_path / "storage"
    evidence = await _after_a4(session, storage, key="a5a-cache")
    renderer = _Renderer(clock=_Clock())
    model = _TextModel()
    await _animate(session, evidence, storage, renderer, model=model)
    first = _manifest(storage, evidence.run)

    again = await _animate(session, evidence, storage, renderer, model=model, key="a5a-cache-2")

    assert renderer.rendered == [1, 2]
    assert len(model.requests) == 1  # the motion plan is written once per run
    assert _manifest(storage, evidence.run) == first
    assert again.manifest_artifact_id is not None

    clip = first.scenes[0]
    assert clip.kind == "clip"
    assert evidence.run.id is not None
    path = scene_clips_directory(storage, run_id=evidence.run.id) / "clips" / clip.video
    path.write_bytes(b"\x00\x00\x00\x18ftypisom tampered")
    await _animate(session, evidence, storage, renderer, model=model, key="a5a-cache-3")

    assert renderer.rendered == [1, 2, 1]
    assert _manifest(storage, evidence.run).scenes[0] == clip


async def test_an_exhausted_budget_publishes_ready_clips_and_the_next_attempt_continues(
    session: AsyncSession, tmp_path: Path
) -> None:
    storage = tmp_path / "storage"
    evidence = await _after_a4(session, storage, key="a5a-budget")
    renderer = _Renderer(clock=_Clock())

    result = await _animate(session, evidence, storage, renderer, limits=_limits(140.0))

    attempts = await _attempts(session, evidence.run)
    assert [attempt.status for attempt in attempts] == ["failed", "succeeded"]
    assert attempts[0].error is not None
    assert attempts[0].error["code"] == SCENE_ANIMATION_BUDGET_EXHAUSTED
    assert attempts[0].error["retryable"] is True
    # Each clip was made once: the second attempt started from the first one's clip.
    assert renderer.rendered == [1, 2] and renderer.starts == 2
    clip_artifacts = [await session.get(Artifact, a) for a in result.clip_artifact_ids]
    assert [artifact.step_run_id for artifact in clip_artifacts if artifact] == [
        attempts[0].id,
        attempts[1].id,
    ]
    assert [scene.kind for scene in _manifest(storage, evidence.run).scenes] == [
        "clip",
        "clip",
        "still",
    ]


async def test_a_video_that_never_fits_fails_closed_at_the_attempt_ceiling(
    session: AsyncSession, tmp_path: Path
) -> None:
    storage = tmp_path / "storage"
    evidence = await _after_a4(session, storage, key="a5a-ceiling")
    renderer = _Renderer(clock=_Clock())

    result = await _animate(
        session, evidence, storage, renderer, limits=_limits(140.0, max_attempts=1)
    )

    assert result.manifest_artifact_id is None
    assert evidence.run.status == RunStatus.FAILED.value
    assert evidence.run.error is not None
    assert evidence.run.error["code"] == SCENE_ANIMATION_BUDGET_EXHAUSTED
    # The finished clip stays published for a retry of the run.
    assert len(result.clip_artifact_ids) == 0
    published = (
        await session.scalars(select(Artifact).where(Artifact.artifact_type == "scene_clip_video"))
    ).all()
    assert len(published) == 1


async def test_a_scene_the_model_fails_on_keeps_its_image(
    session: AsyncSession, tmp_path: Path
) -> None:
    storage = tmp_path / "storage"
    evidence = await _after_a4(session, storage, key="a5a-fallback")
    renderer = _Renderer(clock=_Clock(), outcomes={1: "fail"})

    await _animate(session, evidence, storage, renderer)

    manifest = _manifest(storage, evidence.run)
    first = manifest.scenes[0]
    assert first.kind == "still" and first.reason == "generation_failed"
    assert manifest.scenes[1].kind == "clip"
    assert evidence.run.status == RunStatus.RUNNING.value

    # A later step does not try the failed scene again.
    await _animate(session, evidence, storage, renderer, key="a5a-fallback-2")
    assert renderer.rendered == [1, 2]


async def test_the_step_fails_only_when_no_scene_could_be_animated(
    session: AsyncSession, tmp_path: Path
) -> None:
    storage = tmp_path / "storage"
    evidence = await _after_a4(session, storage, key="a5a-none")
    renderer = _Renderer(clock=_Clock(), outcomes={1: "fail", 2: "timeout"})

    result = await _animate(session, evidence, storage, renderer)

    assert result.manifest_artifact_id is None
    assert evidence.run.status == RunStatus.FAILED.value
    assert evidence.run.error is not None
    assert evidence.run.error["code"] == "CONTENT_SCENE_ANIMATION_NO_SCENE_ANIMATED"
    # The clip that ran past its own timeout restarted the runtime for nothing else.
    assert renderer.rendered == [1, 2]


async def test_a_runtime_that_cannot_start_is_retried_a_bounded_number_of_times(
    session: AsyncSession, tmp_path: Path
) -> None:
    storage = tmp_path / "storage"
    evidence = await _after_a4(session, storage, key="a5a-ram")
    renderer = _Renderer(
        clock=_Clock(),
        start_error=SceneVideoUnavailableError("CONTENT_SCENE_ANIMATION_RAM_INSUFFICIENT"),
    )

    await _animate(session, evidence, storage, renderer)

    attempts = await _attempts(session, evidence.run)
    errors = [attempt.error or {} for attempt in attempts]
    assert [error.get("code") for error in errors] == [
        "CONTENT_SCENE_ANIMATION_RAM_INSUFFICIENT"
    ] * 3
    assert [error.get("retryable") for error in errors] == [True, True, False]
    assert renderer.rendered == [] and renderer.closes == 3
    assert evidence.run.status == RunStatus.FAILED.value


async def test_wrong_weights_fail_the_step_at_once(session: AsyncSession, tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    evidence = await _after_a4(session, storage, key="a5a-weights")
    renderer = _Renderer(
        clock=_Clock(),
        start_error=SceneVideoMisconfiguredError("CONTENT_SCENE_ANIMATION_MODEL_HASH_MISMATCH"),
    )

    await _animate(session, evidence, storage, renderer)

    attempts = await _attempts(session, evidence.run)
    assert len(attempts) == 1
    assert attempts[0].error is not None
    assert attempts[0].error["code"] == "CONTENT_SCENE_ANIMATION_MODEL_HASH_MISMATCH"
    assert evidence.run.status == RunStatus.FAILED.value


async def test_clips_inherit_the_usage_scope_of_the_a3_images(
    session: AsyncSession, tmp_path: Path
) -> None:
    storage = tmp_path / "storage"
    evidence = await _after_a4(session, storage, key="a5a-scope", quality_test=True)
    model = _TextModel()

    result = await _animate(session, evidence, storage, _Renderer(clock=_Clock()), model=model)

    assert _manifest(storage, evidence.run).usage_scope == "quality-test-only"
    step = await session.get(StepRun, result.step_run_id)
    assert step is not None and (step.output_payload or {})["usage_scope"] == "quality-test-only"
    # The description used to draw each image goes to the motion request.
    assert "Image: An adult in a calm room, scene 1" in model.requests[0].user


async def test_a1_runs_a5a_with_the_configured_limits(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = tmp_path / "storage"
    evidence = await _after_a4(session, storage, key="a5a-wiring")
    monkeypatch.setattr(
        a1_executor,
        "settings",
        a1_executor.settings.model_copy(update={"celery_soft_time_limit_seconds": 900}),
    )
    model = _TextModel()
    monkeypatch.setattr(a1_executor, "motion_text_model", lambda: model)
    renderer = _Renderer(clock=_Clock())
    assert evidence.caption.id is not None

    replayed = await a1_executor._animate_scenes(
        session,
        run=evidence.run,
        approval=evidence.approval,
        approval_input=evidence.approval_input,
        audio_artifact=evidence.audio,
        visual_manifest_artifact_id=evidence.manifest.id,
        visual_artifact_ids=tuple(a.id for a in evidence.visuals if a.id is not None),
        caption_artifact_id=evidence.caption.id,
        idempotency_key=evidence.run.idempotency_key,
        storage_root=storage,
        renderer_factory=renderer.factory,
    )

    assert replayed is False and renderer.rendered == [1, 2]
    assert len(model.requests) == 1


def _patched_settings(monkeypatch: pytest.MonkeyPatch, **values: object) -> None:
    base = {
        "content_scene_animation_backend": "ltx23_quality_test",
        "content_scene_animation_comfyui_dir": "D:/lab/ComfyUI",
        "content_scene_animation_python_path": "D:/lab/venv/Scripts/python.exe",
        "content_ffmpeg_expected_sha256": "a" * 64,
        "content_text_model_provider": "ollama",
        "celery_soft_time_limit_seconds": 900,
    }
    base.update(values)
    monkeypatch.setattr(a1_executor, "settings", a1_executor.settings.model_copy(update=base))


def test_a1_builds_the_renderer_config_from_the_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    _patched_settings(
        monkeypatch,
        content_scene_animation_min_free_ram_gb=48.0,
        content_scene_animation_sage_attention=True,
    )

    config = a1_executor._scene_renderer_config()
    limits = a1_executor._scene_animation_limits()

    assert a1_executor._scene_animation_enabled()
    assert config.comfyui_dir == "D:/lab/ComfyUI" and config.models_dir is None
    assert config.min_free_ram_bytes == 48 * 1024**3 and config.sage_attention
    assert config.ffmpeg_sha256 == "a" * 64
    assert limits.timeout_seconds == 885 and limits.max_attempts == 6
    assert limits.text_call_seconds == 120


def test_a1_refuses_a5a_without_its_paths_or_a_pinned_ffmpeg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patched_settings(monkeypatch, content_scene_animation_python_path=None)
    with pytest.raises(a1_executor.ContentScriptRunNotRunnableError, match="ComfyUI"):
        a1_executor._scene_renderer_config()

    _patched_settings(monkeypatch, content_ffmpeg_expected_sha256=None)
    with pytest.raises(a1_executor.ContentScriptRunNotRunnableError, match="FFmpeg"):
        a1_executor._scene_renderer_config()


def test_a1_refuses_a5a_that_cannot_fit_one_clip_in_an_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patched_settings(
        monkeypatch,
        content_text_model_provider="codex_exec",
        content_text_model_codex_timeout_seconds=300,
        celery_soft_time_limit_seconds=600,
    )

    # 45 s reserve + 420 s text call + 180 s start + 60 s load + 201 frames x 0.8 s.
    with pytest.raises(a1_executor.ContentScriptRunNotRunnableError, match="at least 881 s"):
        a1_executor._scene_animation_limits()


def test_a1_refuses_a5a_without_the_stages_around_it(monkeypatch: pytest.MonkeyPatch) -> None:
    _patched_settings(
        monkeypatch,
        content_visual_backend="local_assets_quality_test",
        content_caption_backend="approved_text_timing_quality_test",
        content_assembly_backend="ffmpeg_quality_test",
        content_tts_backend="kokoro_quality_test",
    )

    with pytest.raises(a1_executor.ContentScriptRunNotRunnableError, match="Remotion A5"):
        a1_executor._validate_pipeline_backend_dependencies()


def test_a5a_is_disabled_by_default() -> None:
    assert Settings.model_fields["content_scene_animation_backend"].default == "disabled"
