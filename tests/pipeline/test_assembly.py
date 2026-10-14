"""Content Engine A5 assembly contract with a fake local MP4 adapter."""

from __future__ import annotations

import hashlib
import struct
from collections.abc import AsyncGenerator
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.core.database import Base
from src.pipeline import a1_executor, assembly
from src.pipeline.a1_executor import execute_content_script_run
from src.pipeline.assembly import (
    AssemblyAdapterResult,
    AssemblyPermanentAdapterError,
    AssemblyRequest,
    AssemblyRetryableAdapterError,
    FFmpegQualityTestConfig,
    execute_approved_assembly_step,
    get_configured_assembly_adapter,
)
from src.pipeline.captions import execute_approved_caption_step
from src.pipeline.tts import execute_approved_tts_step
from src.pipeline.visuals import execute_approved_visual_step
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
from tests.pipeline.test_captions import (
    _caption_adapter,
    _script,
    _visual_adapter,
    _write_wav,
)


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as database_session:
        yield database_session
    await engine.dispose()


def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def _write_fake_mp4(
    destination: Path,
    *,
    duration_seconds: Decimal,
    width: int,
    height: int,
) -> None:
    timescale = 1_000
    duration = int(duration_seconds * timescale)
    mvhd = _box(
        b"mvhd",
        b"\x00\x00\x00\x00"
        + (b"\x00" * 8)
        + struct.pack(">II", timescale, duration),
    )
    video_tkhd = _box(b"tkhd", struct.pack(">II", width << 16, height << 16))
    video_hdlr = _box(b"hdlr", b"\x00" * 8 + b"vide")
    video_trak = _box(b"trak", video_tkhd + _box(b"mdia", video_hdlr))
    audio_tkhd = _box(b"tkhd", struct.pack(">II", 0, 0))
    audio_hdlr = _box(b"hdlr", b"\x00" * 8 + b"soun")
    audio_trak = _box(b"trak", audio_tkhd + _box(b"mdia", audio_hdlr))
    encoded = (
        _box(b"ftyp", b"isom" + struct.pack(">I", 0) + b"isommp42")
        + _box(b"moov", mvhd + video_trak + audio_trak)
        + _box(b"mdat", b"fake-media-payload")
    )
    destination.write_bytes(encoded)


def _assembly_adapter(
    request: AssemblyRequest,
    destination: Path,
) -> AssemblyAdapterResult:
    _write_fake_mp4(
        destination,
        duration_seconds=request.duration_seconds,
        width=request.width,
        height=request.height,
    )
    return AssemblyAdapterResult(
        backend="fake-assembler",
        provider="fake-local",
        tool_id="fake-ffmpeg-v1",
        license_id="TEST-ONLY",
        commercial_use=True,
        duration_seconds=request.duration_seconds,
        width=request.width,
        height=request.height,
        video_codec="h264",
        audio_codec="aac",
        subtitles_burned=True,
    )


async def _running_after_a4(
    session: AsyncSession,
    storage_root: Path,
    *,
    idempotency_key: str,
) -> tuple[Run, Approval, Artifact, Artifact, tuple[Artifact, ...], Artifact, dict[str, object]]:
    generated = await execute_content_script_run(
        session,
        idempotency_key=idempotency_key,
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
    script_artifact = await session.get(Artifact, generated.artifact_id)
    assert run is not None and approval is not None and script_artifact is not None
    await decide_approval(
        session,
        approval=approval,
        decision=ApprovalStatus.APPROVED,
        actor="local-owner",
        reason="roteiro integral revisado",
    )
    approval_input: dict[str, object] = {
        "schema_version": 1,
        "run_id": run.id,
        "step_run_id": generated.step_run_id,
        "artifact_id": script_artifact.id,
        "artifact_sha256": script_artifact.sha256,
        "narration_sha256": hashlib.sha256(
            _script().narration.encode("utf-8")
        ).hexdigest(),
    }
    tts_result = await execute_approved_tts_step(
        session,
        run=run,
        approval=approval,
        approval_input_payload=approval_input,
        source_artifact=script_artifact,
        narration=_script().narration,
        idempotency_key=idempotency_key,
        storage_root=storage_root,
        synthesizer=_write_wav,
        voice_id="pf_test",
        language_code="p",
        gpu_power_watts=400,
        timeout_seconds=5,
        finalize_run=False,
    )
    audio_artifact = await session.get(Artifact, tts_result.artifact_id)
    assert audio_artifact is not None
    visual_result = await execute_approved_visual_step(
        session,
        run=run,
        approval=approval,
        approval_input_payload=approval_input,
        script_artifact=script_artifact,
        audio_artifact=audio_artifact,
        idempotency_key=idempotency_key,
        storage_root=storage_root,
        adapter=_visual_adapter,
        queue=QueueClass.GPU,
        gpu_power_watts=400,
        timeout_seconds=5,
        finalize_run=False,
    )
    manifest = await session.get(Artifact, visual_result.manifest_artifact_id)
    loaded_visuals: list[Artifact] = []
    for artifact_id in visual_result.visual_artifact_ids:
        visual = await session.get(Artifact, artifact_id)
        if visual is not None:
            loaded_visuals.append(visual)
    visuals = tuple(loaded_visuals)
    assert manifest is not None and len(visuals) == len(visual_result.visual_artifact_ids)
    caption_result = await execute_approved_caption_step(
        session,
        run=run,
        approval=approval,
        approval_input_payload=approval_input,
        audio_artifact=audio_artifact,
        narration=_script().narration,
        idempotency_key=idempotency_key,
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
    return run, approval, audio_artifact, manifest, visuals, caption, approval_input


@pytest.mark.parametrize(
    ("backend", "expected_code"),
    [
        ("disabled", "CONTENT_ASSEMBLY_DISABLED"),
        ("ffmpeg", "CONTENT_ASSEMBLY_BACKEND_UNREVIEWED"),
    ],
)
def test_configured_assembly_backend_fails_closed(
    tmp_path: Path,
    backend: str,
    expected_code: str,
) -> None:
    request = AssemblyRequest(
        audio_path=tmp_path / "audio.wav",
        visual_paths=(tmp_path / "visual.png",),
        caption_path=tmp_path / "captions.ass",
        format="short",
        width=1080,
        height=1920,
        duration_seconds=Decimal("5"),
    )
    with pytest.raises(AssemblyPermanentAdapterError, match=expected_code):
        get_configured_assembly_adapter(backend)(request, tmp_path / "video.mp4")


def test_ffmpeg_quality_test_requires_configuration_and_exact_hash(
    tmp_path: Path,
) -> None:
    request = AssemblyRequest(
        audio_path=tmp_path / "audio.wav",
        visual_paths=(tmp_path / "visual.png",),
        caption_path=tmp_path / "captions.ass",
        format="short",
        width=1080,
        height=1920,
        duration_seconds=Decimal("5"),
    )
    with pytest.raises(
        AssemblyPermanentAdapterError,
        match="CONTENT_ASSEMBLY_FFMPEG_CONFIG_REQUIRED",
    ):
        get_configured_assembly_adapter("ffmpeg_quality_test")(
            request,
            tmp_path / "video.mp4",
        )

    fake_binary = tmp_path / "ffmpeg.exe"
    fake_binary.write_bytes(b"not-ffmpeg")
    with pytest.raises(
        AssemblyPermanentAdapterError,
        match="CONTENT_ASSEMBLY_FFMPEG_HASH_MISMATCH",
    ):
        get_configured_assembly_adapter(
            "ffmpeg_quality_test",
            quality_test_config=FFmpegQualityTestConfig(
                ffmpeg_path=str(fake_binary),
                ffprobe_path=str(fake_binary),
                ffmpeg_sha256="0" * 64,
                ffprobe_sha256="0" * 64,
            ),
        )(request, tmp_path / "video.mp4")


_KARAOKE_CAPTIONS = """[Script Info]
ScriptType: v4.00+

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:02.00,Default,,0,0,0,,{\\kf50}Você {\\kf50}já {\\kf60}reparou? {\\kf40}É
Dialogue: 0,0:00:02.00,0:00:05.00,Default,,0,0,0,,{\\kf100}a {\\kf100}dívida {\\kf100}cresce.
Dialogue: 0,0:00:05.20,0:00:08.00,Default,,0,0,0,,{\\kf140}‘Cada {\\kf140}troca’
Dialogue: 0,0:00:08.00,0:00:12.00,Default,,0,0,0,,Sem marcação por palavra.
Dialogue: 0,0:00:12.00,0:00:18.00,Default,,0,0,0,,{\\kf300}Fecho {\\kf300}final.
"""


def test_sentence_starts_come_from_word_timings_and_tolerate_untimed_events() -> None:
    starts = assembly._caption_sentence_starts(_KARAOKE_CAPTIONS)

    # "reparou?" ends at 1.60 s, "cresce." at 5.00 s (next word starts at 5.20 s) and
    # the event without karaoke tags counts as one unit ending at its successor.
    assert starts == (Decimal("1.6"), Decimal("5.2"), Decimal("12"))
    assert assembly._caption_sentence_starts("not an ASS file") == ()


def test_motion_cuts_move_to_a_close_sentence_start_and_keep_images_on_screen() -> None:
    duration = Decimal("36")
    sentence_starts = (
        Decimal("1"),      # too early: the first image would last under 2.5 s
        Decimal("6.9"),    # close to the equal cut at 6 s
        Decimal("17.5"),   # close to the equal cut at 18 s
        Decimal("35.5"),   # too late: the last image would last under 2.5 s
    )

    cuts = assembly._motion_cuts(duration, 6, sentence_starts)

    assert cuts == (
        Decimal("6.9"),
        Decimal("12"),     # no sentence start within half a share: equal cut kept
        Decimal("17.5"),
        Decimal("24"),
        Decimal("30"),
    )
    assert assembly._motion_cuts(duration, 6, ()) == tuple(
        Decimal(6 * index) for index in range(1, 6)
    )
    assert assembly._motion_cuts(Decimal("5"), 1, sentence_starts) == ()


def test_motion_inputs_overlap_by_half_a_fade_around_each_cut() -> None:
    cuts = (Decimal("6.9"), Decimal("12"))
    fade = assembly._motion_fade(Decimal("18"), cuts)

    assert fade == Decimal("0.4")
    assert assembly._motion_segment_durations(Decimal("18"), cuts, fade) == (
        Decimal("7.1"),
        Decimal("5.5"),
        Decimal("6.2"),
    )
    # An image that stays on screen briefly shortens the fade instead of breaking it.
    assert assembly._motion_fade(Decimal("1.2"), (Decimal("0.6"),)) == Decimal("0.3")
    assert assembly._motion_fade(Decimal("5"), ()) == Decimal("0.4")


def test_motion_filter_graph_uses_subpixel_motion_and_centred_crossfades() -> None:
    cuts = (Decimal("6.9"), Decimal("12"))
    graph = assembly._ffmpeg_motion_filter_graph(
        segment_durations=(Decimal("7.1"), Decimal("5.5"), Decimal("6.2")),
        cuts=cuts,
        fade=Decimal("0.4"),
        width=1080,
        height=1920,
        caption_name="captions.ass",
    )

    assert graph.count("perspective=") == 3
    assert "zoompan" not in graph
    assert "interpolation=cubic:eval=frame" in graph
    # 7.1 s at 30 fps; the first image zooms in and the second zooms out.
    assert "(1+0.08*on/213)" in graph
    assert "(1+0.08-0.08*on/165)" in graph
    assert "xfade=transition=fade:duration=0.400000:offset=6.700000[mixed_1]" in graph
    assert "xfade=transition=fade:duration=0.400000:offset=11.800000[mixed_2]" in graph
    assert graph.endswith("[mixed_2]ass=filename=captions.ass[video_out]")

    single = assembly._ffmpeg_motion_filter_graph(
        segment_durations=(Decimal("5"),),
        cuts=(),
        fade=Decimal("0.4"),
        width=1080,
        height=1920,
        caption_name="captions.ass",
    )
    assert "xfade" not in single
    assert single.endswith("[visual_0]ass=filename=captions.ass[video_out]")

    with pytest.raises(AssemblyPermanentAdapterError, match="FILTER_INVALID"):
        assembly._ffmpeg_motion_filter_graph(
            segment_durations=(Decimal("5"), Decimal("5")),
            cuts=(),
            fade=Decimal("0.4"),
            width=1080,
            height=1920,
            caption_name="captions.ass",
        )


async def test_a1_continuation_runs_through_a5_local_export(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_root = tmp_path / "storage"
    monkeypatch.setattr(
        a1_executor,
        "settings",
        SimpleNamespace(
            storage_root=str(storage_root),
            ollama_model="fake-local-model",
            celery_soft_time_limit_seconds=300,
            content_tts_backend="test",
            content_tts_voice_id="pf_test",
            content_tts_language_code="p",
            content_visual_backend="test",
            content_caption_backend="test",
            content_assembly_backend="test",
            gpu_power_watts=400,
            cpu_power_watts=150,
        ),
    )
    first = await execute_content_script_run(
        session,
        idempotency_key="content-a1-through-a5",
        input_payload={
            "topic": "Sono e memória",
            "persona": "Ciência acessível",
            "format": "short",
            "recent_openings": [],
        },
        script_generator=lambda *_args: _script(),
        storage_root=storage_root,
    )
    approval = await session.get(Approval, first.approval_id)
    assert approval is not None
    await decide_approval(
        session,
        approval=approval,
        decision=ApprovalStatus.APPROVED,
        actor="local-owner",
        reason="roteiro integral revisado",
    )
    continued = await execute_content_script_run(
        session,
        idempotency_key="content-a1-through-a5",
        input_payload={
            "topic": "Sono e memória",
            "persona": "Ciência acessível",
            "format": "short",
            "recent_openings": [],
        },
        script_generator=lambda *_args: pytest.fail("A1 must replay"),
        tts_synthesizer=_write_wav,
        visual_adapter=_visual_adapter,
        caption_adapter=_caption_adapter,
        assembly_adapter=_assembly_adapter,
        storage_root=storage_root,
    )
    run = await session.get(Run, continued.run_id)
    steps = list(
        (
            await session.scalars(
                select(StepRun)
                .where(StepRun.run_id == continued.run_id)
                .order_by(StepRun.ordinal, StepRun.attempt)
            )
        ).all()
    )

    assert run is not None and run.status == RunStatus.SUCCEEDED.value
    assert [(step.ordinal, step.queue) for step in steps] == [
        (1, "gpu"),
        (2, "gpu"),
        (3, "gpu"),
        (4, "gpu"),
        (5, "cpu"),
        (6, "cpu"),
    ]
    assert run.output_payload is not None
    assert isinstance(run.output_payload["final_video_artifact_id"], int)
    assert isinstance(run.output_payload["originality_report_artifact_id"], int)

    replayed = await execute_content_script_run(
        session,
        idempotency_key="content-a1-through-a5",
        input_payload={
            "topic": "Sono e memória",
            "persona": "Ciência acessível",
            "format": "short",
            "recent_openings": [],
        },
        script_generator=lambda *_args: pytest.fail("terminal pipeline must replay"),
        storage_root=storage_root,
    )
    replayed_steps = list(
        (
            await session.scalars(
                select(StepRun).where(StepRun.run_id == continued.run_id)
            )
        ).all()
    )
    assert replayed.replayed is True
    assert len(replayed_steps) == 6


async def test_a5_creates_verified_mp4_and_observations(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    run, approval, audio, manifest, visuals, caption, approval_input = (
        await _running_after_a4(
            session,
            storage_root,
            idempotency_key="content-a5-success",
        )
    )
    source_hashes = {
        artifact.id: hashlib.sha256(
            (storage_root / artifact.relative_path).read_bytes()
        ).hexdigest()
        for artifact in (audio, manifest, caption, *visuals)
    }
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
        adapter=_assembly_adapter,
        cpu_power_watts=150,
        timeout_seconds=5,
    )
    step = await session.get(StepRun, result.step_run_id)
    artifact = await session.get(Artifact, result.artifact_id)
    metrics = list(
        (
            await session.scalars(
                select(MetricPoint).where(MetricPoint.source == "content-engine:a5")
            )
        ).all()
    )
    ledger = list(
        (
            await session.scalars(
                select(LedgerEntry).where(LedgerEntry.source == "content-engine:a5")
            )
        ).all()
    )

    assert run.status == RunStatus.SUCCEEDED.value
    assert step is not None and step.queue == "cpu" and step.ordinal == 5
    assert artifact is not None and artifact.artifact_type == "final_video"
    assert artifact.media_type == "video/mp4"
    assert {metric.name for metric in metrics} == {
        "content_assembly_video_duration_seconds",
        "content_assembly_render_duration_seconds",
        "content_assembly_output_bytes",
        "content_assembly_energy_estimate_kwh",
    }
    assert len(ledger) == 1 and str(ledger[0].amount) == "0E-10"
    for source in (audio, manifest, caption, *visuals):
        assert hashlib.sha256(
            (storage_root / source.relative_path).read_bytes()
        ).hexdigest() == source_hashes[source.id]
    assert run.output_payload is not None
    assert run.output_payload["final_video_artifact_id"] == artifact.id
    assert not list((storage_root / "content-engine").rglob("*.tmp.mp4"))


async def test_a5_rejects_truncated_mp4_without_artifact(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    run, approval, audio, manifest, visuals, caption, approval_input = (
        await _running_after_a4(
            session,
            storage_root,
            idempotency_key="content-a5-invalid-mp4",
        )
    )

    def truly_invalid(
        request: AssemblyRequest,
        destination: Path,
    ) -> AssemblyAdapterResult:
        result = _assembly_adapter(request, destination)
        destination.write_bytes(b"not-an-mp4")
        return result

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
        adapter=truly_invalid,
        cpu_power_watts=150,
        timeout_seconds=5,
    )
    artifacts = list(
        (
            await session.scalars(
                select(Artifact).where(Artifact.artifact_type == "final_video")
            )
        ).all()
    )

    assert result.artifact_id is None and run.status == RunStatus.FAILED.value
    assert artifacts == []


async def test_a5_rejects_duration_mismatch(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    run, approval, audio, manifest, visuals, caption, approval_input = (
        await _running_after_a4(
            session,
            storage_root,
            idempotency_key="content-a5-duration",
        )
    )

    def wrong_duration(
        request: AssemblyRequest,
        destination: Path,
    ) -> AssemblyAdapterResult:
        result = _assembly_adapter(request, destination)
        return replace(result, duration_seconds=request.duration_seconds + Decimal("2"))

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
        adapter=wrong_duration,
        cpu_power_watts=150,
        timeout_seconds=5,
    )
    step = await session.get(StepRun, result.step_run_id)

    assert run.status == RunStatus.FAILED.value
    assert step is not None and step.error is not None
    assert step.error["code"] == "CONTENT_ASSEMBLY_DURATION_MISMATCH"


@pytest.mark.parametrize(
    ("field", "value", "expected_code"),
    [
        (
            "commercial_use",
            False,
            "CONTENT_ASSEMBLY_COMMERCIAL_RIGHTS_REQUIRED",
        ),
        ("subtitles_burned", False, "CONTENT_ASSEMBLY_SUBTITLES_REQUIRED"),
        ("video_codec", "hevc", "CONTENT_ASSEMBLY_H264_REQUIRED"),
        ("edit", "unreviewed", "CONTENT_ASSEMBLY_INVALID_PROVENANCE"),
    ],
)
async def test_a5_rejects_unsafe_adapter_contract(
    session: AsyncSession,
    tmp_path: Path,
    field: str,
    value: object,
    expected_code: str,
) -> None:
    storage_root = tmp_path / "storage"
    run, approval, audio, manifest, visuals, caption, approval_input = (
        await _running_after_a4(
            session,
            storage_root,
            idempotency_key=f"content-a5-unsafe-{field}",
        )
    )

    def unsafe(request: AssemblyRequest, destination: Path) -> AssemblyAdapterResult:
        result = _assembly_adapter(request, destination)
        if field == "commercial_use":
            return replace(result, commercial_use=bool(value))
        if field == "subtitles_burned":
            return replace(result, subtitles_burned=bool(value))
        if field == "video_codec":
            return replace(result, video_codec=str(value))
        if field == "edit":
            return replace(result, edit=str(value))
        raise AssertionError("unsupported test field")

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
        adapter=unsafe,
        cpu_power_watts=150,
        timeout_seconds=5,
    )
    step = await session.get(StepRun, result.step_run_id)

    assert run.status == RunStatus.FAILED.value
    assert step is not None and step.error is not None
    assert step.error["code"] == expected_code


async def test_a5_retries_transient_adapter_failure_once(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    run, approval, audio, manifest, visuals, caption, approval_input = (
        await _running_after_a4(
            session,
            storage_root,
            idempotency_key="content-a5-retry",
        )
    )
    attempts = 0

    def transient(request: AssemblyRequest, destination: Path) -> AssemblyAdapterResult:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise AssemblyRetryableAdapterError("temporary ffmpeg failure")
        return _assembly_adapter(request, destination)

    async def no_sleep(_seconds: float) -> None:
        return None

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
        adapter=transient,
        cpu_power_watts=150,
        timeout_seconds=5,
        sleep=no_sleep,
    )
    steps = list(
        (
            await session.scalars(
                select(StepRun)
                .where(StepRun.name == "assemble-video-a5")
                .order_by(StepRun.attempt)
            )
        ).all()
    )

    assert attempts == 2 and result.artifact_id is not None
    assert [step.status for step in steps] == [
        RunStatus.FAILED.value,
        RunStatus.SUCCEEDED.value,
    ]


async def test_a5_replays_without_rendering_again(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    run, approval, audio, manifest, visuals, caption, approval_input = (
        await _running_after_a4(
            session,
            storage_root,
            idempotency_key="content-a5-replay",
        )
    )
    calls = 0

    def counted(request: AssemblyRequest, destination: Path) -> AssemblyAdapterResult:
        nonlocal calls
        calls += 1
        return _assembly_adapter(request, destination)

    first = await execute_approved_assembly_step(
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
        adapter=counted,
        cpu_power_watts=150,
        timeout_seconds=5,
        finalize_run=False,
    )
    second = await execute_approved_assembly_step(
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
        adapter=counted,
        cpu_power_watts=150,
        timeout_seconds=5,
        finalize_run=False,
    )

    assert run.status == RunStatus.RUNNING.value
    assert calls == 1
    assert first.replayed is False and second.replayed is True
    assert first.artifact_id == second.artifact_id


async def test_a5_rejects_tampered_caption_before_creating_step(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    run, approval, audio, manifest, visuals, caption, approval_input = (
        await _running_after_a4(
            session,
            storage_root,
            idempotency_key="content-a5-tampered-caption",
        )
    )
    with (storage_root / caption.relative_path).open("a", encoding="utf-8") as handle:
        handle.write("tampered")

    with pytest.raises(ValueError, match="size does not match"):
        await execute_approved_assembly_step(
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
            adapter=_assembly_adapter,
            cpu_power_watts=150,
            timeout_seconds=5,
        )
    steps = list(
        (
            await session.scalars(
                select(StepRun).where(StepRun.name == "assemble-video-a5")
            )
        ).all()
    )

    assert run.status == RunStatus.RUNNING.value and steps == []


async def test_a5_marks_run_failed_when_evidence_registration_fails(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_root = tmp_path / "storage"
    run, approval, audio, manifest, visuals, caption, approval_input = (
        await _running_after_a4(
            session,
            storage_root,
            idempotency_key="content-a5-evidence-failure",
        )
    )

    async def fail_registration(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("simulated artifact registry failure")

    monkeypatch.setattr(assembly, "register_local_artifact", fail_registration)
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
        adapter=_assembly_adapter,
        cpu_power_watts=150,
        timeout_seconds=5,
    )

    assert result.artifact_id is None and run.status == RunStatus.FAILED.value
    assert run.error is not None
    assert run.error["code"] == "CONTENT_ASSEMBLY_EVIDENCE_PERSIST_FAILED"
