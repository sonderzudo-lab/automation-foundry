"""Animated A5 edit playing the clips of step A5a (timeline v3), with fakes only.

The renderer is the fake of ``test_assembly_remotion`` that inspects the boundary
call; the A5a clips are a few bytes each, made by hand or by the fake video
runtime of ``test_scene_animation``. Nothing runs Node.js, Remotion, ComfyUI or
a GPU.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncGenerator, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.database import Base
from src.pipeline import a1_executor, assembly, scene_animation
from src.pipeline.assembly import (
    AssemblyAdapterResult,
    AssemblyPermanentAdapterError,
    AssemblyRequest,
    build_remotion_quality_test_adapter,
    caption_karaoke_words,
    execute_approved_assembly_step,
)
from src.pipeline.scene_clips import (
    AnimatedScene,
    SceneClipsManifest,
    SceneEntry,
    StillScene,
    scene_clip_frames,
    scene_clip_ms,
    scene_clip_window,
)
from src.pipeline.scene_motion import NEUTRAL_MOTION
from src.pipeline.timeline import TimelineV2, TimelineV3, timeline_v3_json_schema
from src.pipeline.timeline_builder import (
    TRANSITION_MS,
    SceneSpan,
    SceneVideo,
    TimelineBuildError,
    scene_cuts_sha256,
    scene_spans,
    timeline_images_from_manifest,
    timeline_v3_with_videos,
)
from src.platform.models import Artifact, QueueClass, RunStatus, StepRun
from tests.pipeline.test_assembly import _write_fake_mp4
from tests.pipeline.test_assembly_remotion import (
    _CAPTIONS,
    _DURATION,
    _NARRATION,
    _PLAN,
    _config,
    _FakeRenderer,
    _project,
    _request,
    _TextModel,
)
from tests.pipeline.test_captions import _script
from tests.pipeline.test_scene_animation import _after_a4, _animate, _Clock, _Renderer

_DURATION_MS = 15_000


@pytest.fixture(autouse=True)
def _fake_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    def probe(_ffprobe: Path, path: Path, *, timeout_seconds: float) -> tuple[Any, ...]:
        assert path.is_file()
        return (_DURATION, 1080, 1920, "h264", "aac")

    monkeypatch.setattr(assembly, "_probe_rendered_video", probe)


def _spans(request: AssemblyRequest) -> tuple[SceneSpan, ...]:
    assert request.visual_manifest_path is not None
    images = timeline_images_from_manifest(
        json.loads(request.visual_manifest_path.read_text(encoding="utf-8"))
    )
    words = caption_karaoke_words(_CAPTIONS, duration_ms=_DURATION_MS)
    return scene_spans(images, words, duration_ms=_DURATION_MS)


def _with_clips(
    tmp_path: Path,
    request: AssemblyRequest,
    *,
    stills: frozenset[int] = frozenset(),
    spans: tuple[SceneSpan, ...] | None = None,
    transition_ms: int = TRANSITION_MS,
) -> AssemblyRequest:
    """The request plus an A5a manifest made for ``spans`` (by default, the montage's)."""
    spans = spans or _spans(request)
    clips_dir = tmp_path / "scene-clips"
    clips_dir.mkdir(exist_ok=True)
    scenes: list[SceneEntry] = []
    clip_paths: list[Path] = []
    for span, image in zip(spans, request.visual_paths, strict=True):
        image_sha256 = hashlib.sha256(image.read_bytes()).hexdigest()
        common: dict[str, Any] = {
            "index": span.index,
            "image": image.name,
            "image_sha256": image_sha256,
            "start_ms": span.start_ms,
            "end_ms": span.end_ms,
        }
        if span.index in stills:
            scenes.append(StillScene(kind="still", reason="generation_failed", **common))
            continue
        start, end = scene_clip_window(
            span, duration_ms=_DURATION_MS, transition_ms=transition_ms
        )
        frames = scene_clip_frames(end - start)
        assert frames is not None
        clip = clips_dir / f"scene-{span.index:03d}.mp4"
        clip.write_bytes(b"\x00\x00\x00\x18ftypisom" + f"clip {span.index}".encode())
        clip_paths.append(clip)
        scenes.append(
            AnimatedScene(
                kind="clip",
                motion=NEUTRAL_MOTION,
                seed=span.index,
                frames=frames,
                video=clip.name,
                video_sha256=hashlib.sha256(clip.read_bytes()).hexdigest(),
                **common,
            )
        )
    manifest = SceneClipsManifest(
        schema_version=1,
        format="short",
        width=1080,
        height=1920,
        duration_ms=_DURATION_MS,
        transition_ms=transition_ms,
        fps=25,
        cuts_sha256=scene_cuts_sha256(spans),
        provider="local-comfyui-ltx23",
        model_id="fake-ltx23@test",
        model_inventory_sha256="c" * 64,
        graph_version="fake-graph-v1",
        license_id="LTX-2 Community License Agreement (test)",
        usage_scope=None,
        scenes=scenes,
    )
    path = clips_dir / "manifest.json"
    path.write_text(json.dumps(manifest.model_dump(mode="json")), encoding="utf-8")
    return replace(
        request,
        scene_clips_manifest_path=path,
        scene_clips_manifest_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        scene_clip_paths=tuple(clip_paths),
    )


def _adapter(tmp_path: Path, renderer: _FakeRenderer, model: _TextModel) -> assembly.AssemblyAdapter:
    return build_remotion_quality_test_adapter(
        _config(tmp_path, _project(tmp_path)),
        narration=_NARRATION,
        text_model=model,
        run_process=renderer,
    )


def test_the_a5a_clips_make_a_v3_timeline_that_plays_them(tmp_path: Path) -> None:
    renderer = _FakeRenderer()
    request = _with_clips(tmp_path, _request(tmp_path))

    result = _adapter(tmp_path, renderer, _TextModel(json.dumps(_PLAN, ensure_ascii=False)))(
        request, tmp_path / "out" / "video.mp4"
    )

    # The props are a valid TimelineV3 with no field beyond its schema.
    assert renderer.timeline is not None
    timeline = TimelineV3.model_validate(renderer.timeline)
    assert set(renderer.timeline) == set(timeline_v3_json_schema()["properties"])
    assert timeline.schema_version == 3
    # The montage cuts where the clips were made for.
    spans = _spans(request)
    assert [(c.start_ms, c.end_ms) for c in timeline.clips] == [
        (s.start_ms, s.end_ms) for s in spans
    ]
    first, second = timeline.clips
    assert (first.video, first.effect, first.video_ms) == ("scene-001.mp4", "none", 169 * 40)
    # The second scene is longer than one clip, so its clip is played slower.
    assert (second.video, second.effect, second.video_ms) == (
        "scene-002.mp4", "none", scene_clip_ms(201),
    )
    assert first.video_sha256 == hashlib.sha256(request.scene_clip_paths[0].read_bytes()).hexdigest()
    # The overlays and words are those of the v2 edit.
    assert [overlay.kind for overlay in timeline.overlays] == ["list", "list", "closing_sentence"]
    # Every clip is in the public folder under its exact timeline name.
    assert renderer.public_files == {
        "narration.wav", "visual-001.png", "visual-002.png", "scene-001.mp4", "scene-002.mp4",
    }
    assert result.scene_clips_manifest_sha256 == request.scene_clips_manifest_sha256
    assert result.video_scenes == 2
    stored = tmp_path / "out" / "timeline.json"
    assert result.timeline_sha256 == hashlib.sha256(stored.read_bytes()).hexdigest()


def test_a_scene_that_kept_its_image_keeps_the_zoom(tmp_path: Path) -> None:
    renderer = _FakeRenderer()
    request = _with_clips(tmp_path, _request(tmp_path), stills=frozenset({2}))

    result = _adapter(tmp_path, renderer, _TextModel(json.dumps(_PLAN, ensure_ascii=False)))(
        request, tmp_path / "out" / "video.mp4"
    )

    timeline = TimelineV3.model_validate(renderer.timeline)
    first, second = timeline.clips
    assert first.video == "scene-001.mp4" and first.effect == "none"
    assert (second.video, second.video_sha256, second.video_ms) == (None, None, None)
    assert second.effect == "zoom_out" and second.image == "visual-002.png"
    assert "scene-002.mp4" not in renderer.public_files
    assert result.video_scenes == 1


def _shifted_cuts(tmp_path: Path, request: AssemblyRequest) -> AssemblyRequest:
    first, second = _spans(request)
    moved = (
        replace(first, end_ms=first.end_ms + 100),
        replace(second, start_ms=second.start_ms + 100),
    )
    return _with_clips(tmp_path, request, spans=moved)


def _changed_manifest(tmp_path: Path, request: AssemblyRequest) -> AssemblyRequest:
    ready = _with_clips(tmp_path, request)
    return replace(ready, scene_clips_manifest_sha256="0" * 64)


def _missing_manifest(tmp_path: Path, request: AssemblyRequest) -> AssemblyRequest:
    ready = _with_clips(tmp_path, request)
    assert ready.scene_clips_manifest_path is not None
    ready.scene_clips_manifest_path.unlink()
    return ready


def _invalid_manifest(tmp_path: Path, request: AssemblyRequest) -> AssemblyRequest:
    ready = _with_clips(tmp_path, request)
    assert ready.scene_clips_manifest_path is not None
    ready.scene_clips_manifest_path.write_text("{}", encoding="utf-8")
    return replace(ready, scene_clips_manifest_sha256=hashlib.sha256(b"{}").hexdigest())


def _other_crossfade(tmp_path: Path, request: AssemblyRequest) -> AssemblyRequest:
    return _with_clips(tmp_path, request, transition_ms=0)


def _other_image(tmp_path: Path, request: AssemblyRequest) -> AssemblyRequest:
    ready = _with_clips(tmp_path, request)
    assert ready.scene_clips_manifest_path is not None
    payload = json.loads(ready.scene_clips_manifest_path.read_text(encoding="utf-8"))
    payload["scenes"][1]["image_sha256"] = "d" * 64
    text = json.dumps(payload)
    ready.scene_clips_manifest_path.write_text(text, encoding="utf-8")
    return replace(
        ready, scene_clips_manifest_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest()
    )


def _deleted_clip(tmp_path: Path, request: AssemblyRequest) -> AssemblyRequest:
    ready = _with_clips(tmp_path, request)
    ready.scene_clip_paths[1].unlink()
    return ready


def _unpublished_clip(tmp_path: Path, request: AssemblyRequest) -> AssemblyRequest:
    ready = _with_clips(tmp_path, request)
    return replace(ready, scene_clip_paths=ready.scene_clip_paths[:1])


def _changed_clip(tmp_path: Path, request: AssemblyRequest) -> AssemblyRequest:
    ready = _with_clips(tmp_path, request)
    ready.scene_clip_paths[0].write_bytes(b"\x00\x00\x00\x18ftypisom another clip")
    return ready


def _extra_clip(tmp_path: Path, request: AssemblyRequest) -> AssemblyRequest:
    ready = _with_clips(tmp_path, request)
    extra = tmp_path / "scene-clips" / "scene-009.mp4"
    extra.write_bytes(b"\x00\x00\x00\x18ftypisom extra")
    return replace(ready, scene_clip_paths=(*ready.scene_clip_paths, extra))


def _clips_without_manifest(tmp_path: Path, request: AssemblyRequest) -> AssemblyRequest:
    ready = _with_clips(tmp_path, request)
    return replace(ready, scene_clips_manifest_path=None, scene_clips_manifest_sha256=None)


@pytest.mark.parametrize(
    ("make", "code"),
    [
        (_shifted_cuts, "CONTENT_ASSEMBLY_SCENE_CUTS_MISMATCH"),
        (_changed_manifest, "CONTENT_ASSEMBLY_SCENE_CLIPS_MANIFEST_HASH_MISMATCH"),
        (_missing_manifest, "CONTENT_ASSEMBLY_SCENE_CLIPS_MANIFEST_MISSING"),
        (_invalid_manifest, "CONTENT_ASSEMBLY_SCENE_CLIPS_INVALID"),
        (_other_crossfade, "CONTENT_ASSEMBLY_SCENE_CLIPS_MISMATCH"),
        (_other_image, "CONTENT_ASSEMBLY_SCENE_CLIPS_MISMATCH"),
        (_deleted_clip, "CONTENT_ASSEMBLY_SCENE_CLIP_MISSING"),
        (_unpublished_clip, "CONTENT_ASSEMBLY_SCENE_CLIP_MISSING"),
        (_changed_clip, "CONTENT_ASSEMBLY_SCENE_CLIP_HASH_MISMATCH"),
        (_extra_clip, "CONTENT_ASSEMBLY_SCENE_CLIPS_MISMATCH"),
        (_clips_without_manifest, "CONTENT_ASSEMBLY_SCENE_CLIPS_MISMATCH"),
    ],
)
def test_a_manifest_that_does_not_match_fails_before_the_text_model(
    tmp_path: Path,
    make: Callable[[Path, AssemblyRequest], AssemblyRequest],
    code: str,
) -> None:
    renderer = _FakeRenderer()
    model = _TextModel(json.dumps(_PLAN, ensure_ascii=False))
    request = make(tmp_path, _request(tmp_path))

    with pytest.raises(AssemblyPermanentAdapterError, match=f"^{code}$"):
        _adapter(tmp_path, renderer, model)(request, tmp_path / "out" / "video.mp4")

    assert model.requests == []
    assert [call[0][1:] for call in renderer.calls] == [["--version"]]


def test_an_edit_that_ignores_the_a5a_clips_is_refused(tmp_path: Path) -> None:
    request = _with_clips(tmp_path, _request(tmp_path))

    def static_edit(_request: AssemblyRequest, destination: Path) -> AssemblyAdapterResult:
        _write_fake_mp4(destination, duration_seconds=_DURATION, width=1080, height=1920)
        return AssemblyAdapterResult(
            backend="ffmpeg_quality_test",
            provider="ffmpeg-local",
            tool_id="ffmpeg@test",
            license_id="GPL",
            commercial_use=True,
            duration_seconds=_DURATION,
            width=1080,
            height=1920,
            video_codec="h264",
            audio_codec="aac",
            subtitles_burned=True,
        )

    with pytest.raises(AssemblyPermanentAdapterError, match="SCENE_CLIPS_NOT_PLAYED"):
        assembly._assemble_atomic(static_edit, request, tmp_path / "out" / "video.mp4")


def test_v3_videos_must_belong_to_a_clip_and_cover_it(tmp_path: Path) -> None:
    renderer = _FakeRenderer()
    _adapter(tmp_path, renderer, _TextModel(json.dumps(_PLAN, ensure_ascii=False)))(
        _request(tmp_path), tmp_path / "out" / "video.mp4"
    )
    timeline = TimelineV2.model_validate(renderer.timeline)
    video = SceneVideo(file_name="scene-001.mp4", sha256="a" * 64, duration_ms=6_760)

    with pytest.raises(TimelineBuildError, match="belongs to no clip"):
        timeline_v3_with_videos(timeline, {3: video})
    with pytest.raises(TimelineBuildError, match="too short"):
        timeline_v3_with_videos(timeline, {1: replace(video, duration_ms=1_000)})
    upgraded = timeline_v3_with_videos(timeline, {})
    assert [clip.video for clip in upgraded.clips] == [None, None]
    assert [clip.effect for clip in upgraded.clips] == [clip.effect for clip in timeline.clips]


def test_the_a5a_artifact_types_are_the_ones_a5a_publishes() -> None:
    assert assembly.SCENE_CLIPS_MANIFEST_ARTIFACT_TYPE == (
        scene_animation.SCENE_CLIPS_MANIFEST_ARTIFACT
    )
    assert assembly.SCENE_CLIP_ARTIFACT_TYPE == scene_animation.SCENE_CLIP_ARTIFACT


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as database_session:
        yield database_session
    await engine.dispose()


async def _a5_after_a5a(
    session: AsyncSession,
    tmp_path: Path,
    *,
    key: str,
    before_a5: Callable[[tuple[Artifact, ...]], None] | None = None,
) -> tuple[StepRun, Path, Any]:
    storage = tmp_path / "storage"
    evidence = await _after_a4(session, storage, key=key)
    animated = await _animate(session, evidence, storage, _Renderer(clock=_Clock()))
    manifest, clips = await a1_executor._load_scene_clips(session, animated)
    if before_a5 is not None:
        before_a5(clips)
    narration = _script().narration
    plan: dict[str, Any] = {
        "schema_version": 1, "steps": None, "highlights": [], "closing_sentence": narration,
    }
    renderer = _FakeRenderer()
    adapter = build_remotion_quality_test_adapter(
        _config(tmp_path, _project(tmp_path)),
        narration=narration,
        text_model=_TextModel(json.dumps(plan, ensure_ascii=False)),
        run_process=renderer,
    )
    result = await execute_approved_assembly_step(
        session,
        run=evidence.run,
        approval=evidence.approval,
        approval_input_payload=evidence.approval_input,
        audio_artifact=evidence.audio,
        visual_manifest_artifact=evidence.manifest,
        visual_artifacts=evidence.visuals,
        caption_artifact=evidence.caption,
        idempotency_key=evidence.run.idempotency_key,
        storage_root=storage,
        adapter=adapter,
        cpu_power_watts=150,
        gpu_power_watts=400,
        queue=QueueClass.GPU,
        timeout_seconds=5,
        finalize_run=False,
        scene_clips_manifest_artifact=manifest,
        scene_clip_artifacts=clips,
    )
    step = await session.get(StepRun, result.step_run_id)
    assert step is not None
    return step, storage, evidence.run


async def test_a5_plays_the_clips_a5a_published(session: AsyncSession, tmp_path: Path) -> None:
    step, storage, run = await _a5_after_a5a(session, tmp_path, key="a5-plays-a5a")

    assert step.queue == "gpu" and step.status == "succeeded"
    output = step.output_payload or {}
    manifest_file = storage / "content-engine" / f"run-{run.id}" / "scene-clips" / "manifest.json"
    assert output["scene_clips_manifest_sha256"] == hashlib.sha256(
        manifest_file.read_bytes()
    ).hexdigest()
    assert output["video_scenes"] == 2
    assert step.input_payload is not None
    assert len(step.input_payload["scene_clip_artifact_ids"]) == 2
    stored = storage / "content-engine" / f"run-{run.id}" / "assembly" / "timeline.json"
    timeline = TimelineV3.model_validate_json(stored.read_text(encoding="utf-8"))
    # The third scene is too long for a clip in the A5a fake and keeps its zoom.
    assert [clip.video is not None for clip in timeline.clips] == [True, True, False]
    assert timeline.clips[2].effect != "none"
    assert run.status == RunStatus.RUNNING.value


async def test_a5_fails_closed_when_a_published_clip_changed(
    session: AsyncSession, tmp_path: Path
) -> None:
    def tamper(clips: tuple[Artifact, ...]) -> None:
        path = tmp_path / "storage" / clips[0].relative_path
        path.write_bytes(path.read_bytes() + b"tampered")

    step, _storage, run = await _a5_after_a5a(
        session, tmp_path, key="a5-tampered-clip", before_a5=tamper
    )

    assert step.status == "failed"
    assert (step.error or {}).get("code") == "CONTENT_ASSEMBLY_SCENE_CLIP_HASH_MISMATCH"
    assert run.status == RunStatus.FAILED.value


async def test_a1_refuses_a_montage_without_the_a5a_manifest(session: AsyncSession) -> None:
    with pytest.raises(a1_executor.ContentScriptRunNotRunnableError, match="A5a"):
        await a1_executor._load_scene_clips(session, None)
