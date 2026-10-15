"""Animated A5 edit (remotion_quality_test) with a fake renderer that checks the call.

Nothing here runs Node.js, Remotion, a browser or a text model: the Remotion
project is a synthetic directory, the renderer is a fake that inspects the
boundary call and writes a minimal MP4, and the text model answers scripted plans.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.database import Base
from src.pipeline import a1_executor, assembly
from src.pipeline.assembly import (
    REMOTION_COMPOSITION,
    REMOTION_VERSION,
    AssemblyPermanentAdapterError,
    AssemblyRequest,
    AssemblyRetryableAdapterError,
    RemotionQualityTestConfig,
    build_remotion_quality_test_adapter,
    caption_karaoke_words,
    execute_approved_assembly_step,
    get_configured_assembly_adapter,
)
from src.pipeline.text_model import (
    TextModelPermanentError,
    TextModelProviderName,
    TextModelRequest,
    TextModelResult,
)
from src.pipeline.timeline import TimelineV2, timeline_v2_json_schema
from src.platform.models import MetricPoint, QueueClass, StepRun
from tests.pipeline.test_assembly import _running_after_a4, _write_fake_mp4
from tests.pipeline.test_captions import _script

_FFPROBE_BYTES = b"fake ffprobe binary"
_STRETCHES = (
    "Repara nessa sequência: primeiro, a pausa. A pausa ajuda.",
    "Segundo, a distância. Deixar longe ajuda, mas decidir importa.",
)
_NARRATION = " ".join(_STRETCHES)
_CAPTIONS = """[Script Info]
PlayResX: 1080
PlayResY: 1920

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.20,0:00:03.80,Default,,0,0,0,,{\\kf40}Repara {\\kf40}nessa {\\kf60}sequência: {\\kf50}primeiro, {\\kf20}a {\\kf150}pausa.
Dialogue: 0,0:00:04.00,0:00:06.00,Default,,0,0,0,,{\\kf30}A {\\kf50}pausa {\\kf120}ajuda.
Dialogue: 0,0:00:06.50,0:00:09.00,Default,,0,0,0,,{\\kf60}Segundo, {\\kf20}a {\\kf170}distância.
Dialogue: 0,0:00:09.50,0:00:14.00,Default,,0,0,0,,{\\kf50}Deixar {\\kf50}longe {\\kf60}ajuda, {\\kf50}mas {\\kf60}decidir {\\kf150}importa.
"""
_DURATION = Decimal("15")
_PLAN: dict[str, Any] = {
    "schema_version": 1,
    "steps": {
        "title": "A sequência",
        "items": [
            {"label": "a pausa", "anchor": "primeiro, a pausa"},
            {"label": "a distância", "anchor": "Segundo, a distância"},
        ],
    },
    "highlights": [],
    "closing_sentence": "Deixar longe ajuda, mas decidir importa.",
}


class _TextModel:
    provider: TextModelProviderName = "codex_exec"
    model_id = "codex-default"

    def __init__(self, *outcomes: str | Exception) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[TextModelRequest] = []

    def complete(self, request: TextModelRequest) -> TextModelResult:
        self.requests.append(request)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return TextModelResult(
            text=outcome,
            provider=self.provider,
            model_id=self.model_id,
            duration_seconds=Decimal("2"),
        )


@dataclass
class _FakeRenderer:
    """Answers ``node --version`` and checks the render call before writing an MP4."""

    width: int = 1080
    height: int = 1920
    duration_seconds: Decimal = _DURATION
    render_returncode: int = 0
    render_raises: Exception | None = None
    calls: list[tuple[list[str], Path, float]] = field(default_factory=list)
    timeline: dict[str, Any] | None = None
    public_files: set[str] = field(default_factory=set)

    def __call__(
        self, arguments: list[str], cwd: Path, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(arguments), cwd, timeout))
        if arguments[1:] == ["--version"]:
            return subprocess.CompletedProcess(arguments, 0, "v22.11.0\n", "")
        if self.render_raises is not None:
            raise self.render_raises
        props = Path(arguments[6].removeprefix("--props="))
        public = Path(arguments[7].removeprefix("--public-dir="))
        self.timeline = json.loads(props.read_text(encoding="utf-8"))
        self.public_files = {path.name for path in public.iterdir()}
        if self.render_returncode == 0:
            _write_fake_mp4(
                Path(arguments[5]),
                duration_seconds=self.duration_seconds,
                width=self.width,
                height=self.height,
            )
        return subprocess.CompletedProcess(arguments, self.render_returncode, "", "boom")


def _project(root: Path, *, locked: str = REMOTION_VERSION, browser: bool = True) -> Path:
    project = root / "remotion"
    (project / "src").mkdir(parents=True)
    (project / "src" / "index.ts").write_text("// entry\n", encoding="utf-8")
    packages = {f"node_modules/{name}": {"version": locked} for name in assembly.REMOTION_PACKAGES}
    (project / "package-lock.json").write_text(
        json.dumps({"lockfileVersion": 3, "packages": {"": {}, **packages}}), encoding="utf-8"
    )
    for name in assembly.REMOTION_PACKAGES:
        package = project / "node_modules" / name
        package.mkdir(parents=True)
        (package / "package.json").write_text(
            json.dumps({"name": name, "version": REMOTION_VERSION}), encoding="utf-8"
        )
    (project / "node_modules" / "@remotion" / "cli" / "remotion-cli.js").write_text(
        "// cli\n", encoding="utf-8"
    )
    if browser:
        shell = project / "node_modules" / ".remotion" / "chrome-headless-shell" / "win64"
        shell.mkdir(parents=True)
        (shell / "chrome-headless-shell.exe").write_bytes(b"browser")
    return project


def _config(tmp_path: Path, project: Path) -> RemotionQualityTestConfig:
    ffprobe = tmp_path / "ffprobe.exe"
    ffprobe.write_bytes(_FFPROBE_BYTES)
    return RemotionQualityTestConfig(
        node_path=sys.executable,
        ffprobe_path=str(ffprobe),
        ffprobe_sha256=hashlib.sha256(_FFPROBE_BYTES).hexdigest(),
        project_dir=str(project),
        timeout_seconds=600,
    )


def _request(tmp_path: Path) -> AssemblyRequest:
    sources = tmp_path / "sources"
    sources.mkdir()
    audio = sources / "narration-source.wav"
    audio.write_bytes(b"RIFF fake wav")
    captions = sources / "captions.ass"
    captions.write_text(_CAPTIONS, encoding="utf-8")
    assets = []
    paths = []
    for index, stretch in enumerate(_STRETCHES, start=1):
        image = sources / f"visual-{index:03d}.png"
        image.write_bytes(f"png {index}".encode())
        paths.append(image)
        assets.append(
            {
                "index": index,
                "file_name": image.name,
                "sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
                "narration_text": stretch,
            }
        )
    manifest = sources / "manifest.json"
    manifest.write_text(json.dumps({"assets": assets}), encoding="utf-8")
    return AssemblyRequest(
        audio_path=audio,
        visual_paths=tuple(paths),
        caption_path=captions,
        format="short",
        width=1080,
        height=1920,
        duration_seconds=_DURATION,
        visual_manifest_path=manifest,
    )


@pytest.fixture(autouse=True)
def _fake_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    def probe(_ffprobe: Path, path: Path, *, timeout_seconds: float) -> tuple[Any, ...]:
        assert path.is_file() and timeout_seconds <= 60
        return (_DURATION, 1080, 1920, "h264", "aac")

    monkeypatch.setattr(assembly, "_probe_rendered_video", probe)


def _adapter(
    tmp_path: Path,
    renderer: _FakeRenderer,
    model: _TextModel,
    *,
    project: Path | None = None,
    released: list[int] | None = None,
) -> assembly.AssemblyAdapter:
    return build_remotion_quality_test_adapter(
        _config(tmp_path, project or _project(tmp_path)),
        narration=_NARRATION,
        text_model=model,
        release_text_model=(lambda: released.append(len(model.requests)))
        if released is not None
        else None,
        run_process=renderer,
    )


def test_caption_words_come_from_the_karaoke_tags_in_milliseconds() -> None:
    words = caption_karaoke_words(_CAPTIONS, duration_ms=15_000)

    assert [word.text for word in words[:4]] == ["Repara", "nessa", "sequência:", "primeiro,"]
    assert (words[0].start_ms, words[0].end_ms) == (200, 600)
    assert (words[3].start_ms, words[3].end_ms) == (1_600, 2_100)
    assert words[-1].text == "importa." and words[-1].end_ms == 13_700
    # A word heard past the end of the audio is cut at the end.
    clipped = caption_karaoke_words(_CAPTIONS, duration_ms=13_000)
    assert clipped[-1].end_ms == 13_000 and clipped[-1].start_ms <= 13_000


def test_untimed_events_and_overlaps_never_make_words_go_back_in_time() -> None:
    text = (
        "[Events]\n"
        "Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,{\\kf100}um {\\kf100}dois\n"
        "Dialogue: 0,0:00:01.50,0:00:03.00,Default,,0,0,0,,{\\kf50}três\n"
        "Dialogue: 0,0:00:04.00,0:00:05.00,Default,,0,0,0,,Sem marcação.\n"
    )

    words = caption_karaoke_words(text, duration_ms=10_000)

    assert [(w.text, w.start_ms, w.end_ms) for w in words] == [
        ("um", 1_000, 2_000),
        ("três", 2_000, 2_000),
        ("dois", 2_000, 3_000),
    ]


def test_the_render_is_the_boundary_call_with_a_checked_timeline(tmp_path: Path) -> None:
    project = _project(tmp_path)
    renderer = _FakeRenderer()
    model = _TextModel(json.dumps(_PLAN, ensure_ascii=False))
    released: list[int] = []
    request = _request(tmp_path)
    destination = tmp_path / "out" / ".video.tmp.mp4"

    result = _adapter(tmp_path, renderer, model, project=project, released=released)(
        request, destination
    )

    version, render = renderer.calls
    assert version[0] == [sys.executable, "--version"]
    arguments, cwd, timeout = render
    workspace = Path(arguments[6].removeprefix("--props=")).parent
    assert arguments == [
        sys.executable,
        str(project / "node_modules" / "@remotion" / "cli" / "remotion-cli.js"),
        "render",
        str(project / "src" / "index.ts"),
        REMOTION_COMPOSITION,
        str(destination),
        f"--props={workspace / 'timeline.json'}",
        f"--public-dir={workspace / 'public'}",
        "--gl=angle",
    ]
    assert cwd == project.resolve() and timeout == 600
    # The public folder holds the narration and every image under its timeline name.
    assert renderer.public_files == {"narration.wav", "visual-001.png", "visual-002.png"}
    assert not workspace.exists()
    # The props are a valid TimelineV2 with no field beyond the schema.
    assert renderer.timeline is not None
    timeline = TimelineV2.model_validate(renderer.timeline)
    assert set(renderer.timeline) == set(timeline_v2_json_schema()["properties"])
    assert [clip.image for clip in timeline.clips] == ["visual-001.png", "visual-002.png"]
    assert [overlay.kind for overlay in timeline.overlays] == [
        "list", "list", "closing_sentence",
    ]
    assert timeline.audio_sha256 == hashlib.sha256(request.audio_path.read_bytes()).hexdigest()
    assert timeline.caption_ass_sha256 == hashlib.sha256(
        request.caption_path.read_bytes()
    ).hexdigest()
    # The local text model is released after the plan and before the render.
    assert released == [1]
    stored = destination.parent / "timeline.json"
    assert result.edit == "animated"
    assert result.timeline_sha256 == hashlib.sha256(stored.read_bytes()).hexdigest()
    assert json.loads(stored.read_text(encoding="utf-8")) == renderer.timeline
    assert result.backend == "remotion_quality_test"
    assert result.tool_id.startswith(f"remotion@{REMOTION_VERSION} node@v22.11.0 lock-sha256:")
    assert result.animation_text_model == "codex_exec:codex-default"
    assert (result.video_codec, result.audio_codec) == ("h264", "aac")


def test_a_plan_that_does_not_fit_the_spoken_words_is_asked_again(tmp_path: Path) -> None:
    misplaced = {
        **_PLAN,
        "steps": {
            "title": "A sequência",
            "items": [
                {"label": "a pausa", "anchor": "primeiro, a pausa"},
                {"label": "decidir importa", "anchor": "mas decidir importa"},
            ],
        },
    }
    model = _TextModel(
        json.dumps(misplaced, ensure_ascii=False), json.dumps(_PLAN, ensure_ascii=False)
    )

    result = _adapter(tmp_path, _FakeRenderer(), model)(
        _request(tmp_path), tmp_path / "out" / "video.mp4"
    )

    assert result.edit == "animated" and len(model.requests) == 2
    assert "does not fit the spoken words" in model.requests[1].user


@pytest.mark.parametrize(
    ("outcome", "error", "code"),
    [
        ("not json", AssemblyPermanentAdapterError, "ANIMATION_PLAN_REJECTED"),
        (TextModelPermanentError("no login"), AssemblyPermanentAdapterError, "MISCONFIGURED"),
        (TimeoutError("slow"), AssemblyRetryableAdapterError, "ANIMATION_MODEL_UNAVAILABLE"),
    ],
)
def test_no_render_happens_without_an_accepted_plan(
    tmp_path: Path,
    outcome: str | Exception,
    error: type[Exception],
    code: str,
) -> None:
    renderer = _FakeRenderer()

    with pytest.raises(error, match=code):
        _adapter(tmp_path, renderer, _TextModel(outcome))(
            _request(tmp_path), tmp_path / "out" / "video.mp4"
        )

    assert [call[0][1:] for call in renderer.calls] == [["--version"]]


@pytest.mark.parametrize(
    ("project_kwargs", "code"),
    [
        ({"locked": "4.0.999"}, "REMOTION_VERSION_MISMATCH"),
        ({"browser": False}, "REMOTION_BROWSER_MISSING"),
    ],
)
def test_an_unpinned_or_incomplete_install_is_refused_before_any_call(
    tmp_path: Path,
    project_kwargs: dict[str, Any],
    code: str,
) -> None:
    renderer = _FakeRenderer()
    model = _TextModel(json.dumps(_PLAN, ensure_ascii=False))
    project = _project(tmp_path, **project_kwargs)

    with pytest.raises(AssemblyPermanentAdapterError, match=code):
        _adapter(tmp_path, renderer, model, project=project)(
            _request(tmp_path), tmp_path / "out" / "video.mp4"
        )

    assert renderer.calls == [] and model.requests == []


def test_an_installed_version_that_differs_from_the_lock_is_refused(tmp_path: Path) -> None:
    project = _project(tmp_path)
    (project / "node_modules" / "remotion" / "package.json").write_text(
        json.dumps({"version": "4.0.532"}), encoding="utf-8"
    )

    with pytest.raises(AssemblyPermanentAdapterError, match="REMOTION_VERSION_MISMATCH"):
        _adapter(tmp_path, _FakeRenderer(), _TextModel("{}"), project=project)(
            _request(tmp_path), tmp_path / "out" / "video.mp4"
        )


def test_a_wrong_ffprobe_is_refused(tmp_path: Path) -> None:
    config = _config(tmp_path, _project(tmp_path))
    adapter = build_remotion_quality_test_adapter(
        RemotionQualityTestConfig(
            node_path=config.node_path,
            ffprobe_path=config.ffprobe_path,
            ffprobe_sha256="0" * 64,
            project_dir=config.project_dir,
        ),
        narration=_NARRATION,
        text_model=_TextModel("{}"),
        run_process=_FakeRenderer(),
    )

    with pytest.raises(AssemblyPermanentAdapterError, match="FFPROBE_HASH_MISMATCH"):
        adapter(_request(tmp_path), tmp_path / "out" / "video.mp4")


@pytest.mark.parametrize(
    ("renderer", "error", "code"),
    [
        (_FakeRenderer(render_returncode=1), AssemblyPermanentAdapterError, "RENDER_FAILED"),
        (
            _FakeRenderer(render_raises=subprocess.TimeoutExpired(["node"], 600)),
            AssemblyRetryableAdapterError,
            "REMOTION_UNAVAILABLE",
        ),
    ],
)
def test_a_failed_render_is_reported_without_a_timeline(
    tmp_path: Path,
    renderer: _FakeRenderer,
    error: type[Exception],
    code: str,
) -> None:
    destination = tmp_path / "out" / "video.mp4"

    with pytest.raises(error, match=code):
        _adapter(tmp_path, renderer, _TextModel(json.dumps(_PLAN, ensure_ascii=False)))(
            _request(tmp_path), destination
        )

    assert not (destination.parent / "timeline.json").exists()
    assert [path.name for path in destination.parent.iterdir()] == []


def test_images_that_differ_from_the_manifest_are_refused(tmp_path: Path) -> None:
    request = _request(tmp_path)
    renamed = request.visual_paths[1].with_name("visual-009.png")
    request.visual_paths[1].rename(renamed)
    swapped = replace(request, visual_paths=(request.visual_paths[0], renamed))

    with pytest.raises(AssemblyPermanentAdapterError, match="TIMELINE_INVALID"):
        _adapter(tmp_path, _FakeRenderer(), _TextModel(json.dumps(_PLAN)))(
            swapped, tmp_path / "out" / "video.mp4"
        )


def test_the_configured_backend_needs_its_config_and_the_narration(tmp_path: Path) -> None:
    request = _request(tmp_path)

    with pytest.raises(AssemblyPermanentAdapterError, match="REMOTION_CONFIG_REQUIRED"):
        get_configured_assembly_adapter("remotion_quality_test")(request, tmp_path / "v.mp4")
    with pytest.raises(AssemblyPermanentAdapterError, match="REMOTION_CONFIG_REQUIRED"):
        get_configured_assembly_adapter(
            "remotion_quality_test",
            remotion_config=_config(tmp_path, _project(tmp_path)),
        )(request, tmp_path / "v.mp4")


def test_the_process_runner_uses_no_shell_and_stops_on_timeout(tmp_path: Path) -> None:
    done = assembly._run_process_tree(
        [sys.executable, "-c", "print('v1.2.3')"], tmp_path, 30.0
    )
    assert done.returncode == 0 and done.stdout.strip() == "v1.2.3"

    with pytest.raises(subprocess.TimeoutExpired):
        assembly._run_process_tree(
            [sys.executable, "-c", "import time; time.sleep(30)"], tmp_path, 0.5
        )


def test_a1_runs_the_animated_edit_on_the_gpu_queue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        a1_executor,
        "settings",
        SimpleNamespace(
            content_assembly_backend="remotion_quality_test",
            content_ffprobe_path="ffprobe",
            content_ffprobe_expected_sha256="a" * 64,
            content_remotion_node_path="node",
            celery_soft_time_limit_seconds=1_800,
        ),
    )

    assert a1_executor._assembly_queue() is QueueClass.GPU
    config = a1_executor._remotion_quality_test_config()
    assert config is not None
    assert (config.node_path, config.ffprobe_sha256, config.timeout_seconds) == (
        "node", "a" * 64, 1_785.0,
    )
    assert a1_executor._ffmpeg_quality_test_config() is None

    monkeypatch.setattr(
        a1_executor, "settings", SimpleNamespace(content_assembly_backend="ffmpeg_quality_test")
    )
    assert a1_executor._assembly_queue() is QueueClass.CPU
    assert a1_executor._remotion_quality_test_config() is None


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as database_session:
        yield database_session
    await engine.dispose()


async def test_a5_records_the_animated_edit_on_the_gpu_queue(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    run, approval, audio, manifest, visuals, caption, approval_input = await _running_after_a4(
        session, storage_root, idempotency_key="content-a5-animated"
    )
    narration = _script().narration
    plan = {
        "schema_version": 1,
        "steps": None,
        "highlights": [{"text": "fortalece o aprendizado"}],
        "closing_sentence": narration,
    }
    duration = Decimal("5")
    renderer = _FakeRenderer(duration_seconds=duration)

    def probe(_ffprobe: Path, _path: Path, *, timeout_seconds: float) -> tuple[Any, ...]:
        return (duration, 1080, 1920, "h264", "aac")

    adapter = build_remotion_quality_test_adapter(
        _config(tmp_path, _project(tmp_path)),
        narration=narration,
        text_model=_TextModel(json.dumps(plan, ensure_ascii=False)),
        run_process=renderer,
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(assembly, "_probe_rendered_video", probe)
        result = await execute_approved_assembly_step(
            session,
            run=run,
            approval=approval,
            approval_input_payload=approval_input,
            audio_artifact=audio,
            visual_manifest_artifact=manifest,
            visual_artifacts=visuals,
            caption_artifact=caption,
            idempotency_key=run.idempotency_key,
            storage_root=storage_root,
            adapter=adapter,
            cpu_power_watts=150,
            gpu_power_watts=400,
            queue=QueueClass.GPU,
            timeout_seconds=5,
            finalize_run=False,
        )

    step = await session.get(StepRun, result.step_run_id)
    assert step is not None and step.queue == "gpu" and step.status == "succeeded"
    output = step.output_payload or {}
    timeline_file = storage_root / "content-engine" / f"run-{run.id}" / "assembly" / "timeline.json"
    assert output["edit"] == "animated"
    assert output["timeline_sha256"] == hashlib.sha256(timeline_file.read_bytes()).hexdigest()
    assert output["animation_text_model"] == "codex_exec:codex-default"
    # The closing sentence holds the whole narration, so the highlight had no room.
    assert output["dropped_highlights"] == 1
    metrics = list(
        (
            await session.scalars(
                select(MetricPoint).where(
                    MetricPoint.name == "content_assembly_energy_estimate_kwh"
                )
            )
        ).all()
    )
    assert len(metrics) == 1 and metrics[0].dimensions["queue"] == "gpu"
    render_seconds = Decimal(str(output["render_seconds"]))
    assert metrics[0].value == ((render_seconds / 3600) * Decimal("0.4")).quantize(
        Decimal("0.0000000001")
    )


async def test_a5_refuses_a_gpu_queue_without_gpu_power(session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="gpu_power_watts"):
        await execute_approved_assembly_step(
            session,
            run=SimpleNamespace(),  # type: ignore[arg-type]
            approval=SimpleNamespace(),  # type: ignore[arg-type]
            approval_input_payload={},
            audio_artifact=SimpleNamespace(),  # type: ignore[arg-type]
            visual_manifest_artifact=SimpleNamespace(),  # type: ignore[arg-type]
            visual_artifacts=(),
            caption_artifact=SimpleNamespace(),  # type: ignore[arg-type]
            idempotency_key="k",
            storage_root=Path("."),
            adapter=lambda _request, _destination: pytest.fail("must not render"),
            cpu_power_watts=150,
            timeout_seconds=5,
            queue=QueueClass.GPU,
        )
