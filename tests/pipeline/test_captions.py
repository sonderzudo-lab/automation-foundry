"""Content Engine A4 caption contract with fake word-timing adapters."""

from __future__ import annotations

import hashlib
import struct
import wave
import zlib
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
from src.pipeline import a1_executor, captions
from src.pipeline.a1_executor import execute_content_script_run
from src.pipeline.captions import (
    FASTER_WHISPER_MODEL_REVISION,
    FASTER_WHISPER_QUALITY_TEST_BACKEND,
    CaptionAdapterResult,
    CaptionPermanentAdapterError,
    CaptionRetryableAdapterError,
    CaptionWord,
    FasterWhisperQualityTestConfig,
    execute_approved_caption_step,
    get_configured_caption_adapter,
)
from src.pipeline.script_gen import ScriptResult
from src.pipeline.tts import TTSAdapterResult, execute_approved_tts_step
from src.pipeline.visuals import VisualAssetResult, VisualRequest
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


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as database_session:
        yield database_session
    await engine.dispose()


def _script() -> ScriptResult:
    return ScriptResult(
        angle="Dormir reorganiza memórias.",
        outline="1. Gancho\n2. Explicação\n3. Fecho",
        full_script="O sono reorganiza memórias e fortalece o aprendizado.",
        hook="Seu cérebro edita memórias enquanto você dorme.",
        narration="O sono reorganiza memórias e fortalece o aprendizado.",
    )


def _write_wav(
    _text: str,
    destination: Path,
    voice_id: str,
    language_code: str,
) -> TTSAdapterResult:
    with wave.open(str(destination), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24_000)
        audio.writeframes(b"\x00\x00" * 24_000 * 5)
    return TTSAdapterResult(
        backend="fake-local",
        model_id="fake-tts-v1",
        voice_id=voice_id,
        language_code=language_code,
        license_id="TEST-ONLY",
    )


def _caption_result() -> CaptionAdapterResult:
    texts = ("O", "sono", "reorganiza", "memórias", "e", "fortalece", "o", "aprendizado.")
    words = tuple(
        CaptionWord(
            text=text,
            start_seconds=Decimal(index) * Decimal("0.45"),
            end_seconds=(Decimal(index) * Decimal("0.45")) + Decimal("0.35"),
        )
        for index, text in enumerate(texts)
    )
    return CaptionAdapterResult(
        backend="fake-word-timing",
        provider="fake-local",
        model_id="fake-caption-v1",
        language_code="pt",
        license_id="TEST-ONLY",
        commercial_use=True,
        words=words,
    )


def _caption_adapter(_audio_path: Path, _language_code: str) -> CaptionAdapterResult:
    return _caption_result()


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def _visual_adapter(
    requests: tuple[VisualRequest, ...],
    destination: Path,
) -> tuple[VisualAssetResult, ...]:
    results: list[VisualAssetResult] = []
    for request in requests:
        file_name = f"visual-{request.index:03d}.png"
        row = b"\x00" + (b"\x22\x44\x66" * request.width)
        encoded = (
            b"\x89PNG\r\n\x1a\n"
            + _png_chunk(
                b"IHDR",
                struct.pack(">IIBBBBB", request.width, request.height, 8, 2, 0, 0, 0),
            )
            + _png_chunk(b"IDAT", zlib.compress(row * request.height, level=9))
            + _png_chunk(b"IEND", b"")
        )
        (destination / file_name).write_bytes(encoded)
        results.append(
            VisualAssetResult(
                file_name=file_name,
                source_type="generated",
                provider="fake-local",
                model_id="fake-image-v1",
                source_id=f"request-{request.index}",
                source_uri=f"local-model://fake-image-v1/{request.index}",
                license_id="TEST-ONLY",
                commercial_use=True,
                attribution="test fixture",
            )
        )
    return tuple(results)


async def _running_after_tts(
    session: AsyncSession,
    storage_root: Path,
    *,
    idempotency_key: str,
) -> tuple[Run, Approval, Artifact, dict[str, object]]:
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
    approval_input = {
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
    assert audio_artifact is not None and run.status == RunStatus.RUNNING.value
    return run, approval, audio_artifact, approval_input


@pytest.mark.parametrize(
    ("backend", "expected_code"),
    [
        ("disabled", "CONTENT_CAPTIONS_DISABLED"),
        ("faster_whisper", "CONTENT_CAPTIONS_BACKEND_UNREVIEWED"),
        ("whisper", "CONTENT_CAPTIONS_BACKEND_UNREVIEWED"),
    ],
)
def test_configured_caption_backend_fails_closed(
    tmp_path: Path,
    backend: str,
    expected_code: str,
) -> None:
    with pytest.raises(CaptionPermanentAdapterError, match=expected_code):
        get_configured_caption_adapter(backend)(tmp_path / "audio.wav", "pt")


def test_approved_text_timing_adapter_uses_exact_text_and_wav_duration(
    tmp_path: Path,
) -> None:
    audio_path = tmp_path / "audio.wav"
    _write_wav(_script().narration, audio_path, "pf_test", "p")
    adapter = get_configured_caption_adapter(
        "approved_text_timing_quality_test",
        narration=_script().narration,
    )

    result = adapter(audio_path, "pt")

    assert tuple(word.text for word in result.words) == tuple(
        _script().narration.split()
    )
    assert result.words[0].start_seconds == 0
    assert result.words[-1].end_seconds == Decimal("5.0000000000")
    assert result.commercial_use is True
    assert result.model_id == "deterministic-proportional-v1"


def test_approved_text_timing_backend_requires_approved_narration() -> None:
    with pytest.raises(
        CaptionPermanentAdapterError,
        match="CONTENT_CAPTIONS_NARRATION_REQUIRED",
    ):
        get_configured_caption_adapter("approved_text_timing_quality_test")


def test_approved_text_timing_backend_routes_a4_to_cpu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        a1_executor,
        "settings",
        SimpleNamespace(
            content_caption_backend="approved_text_timing_quality_test"
        ),
    )

    assert a1_executor._captions_queue() is QueueClass.CPU


def test_faster_whisper_backend_requires_explicit_snapshot() -> None:
    with pytest.raises(
        CaptionPermanentAdapterError,
        match="CONTENT_CAPTIONS_FASTER_WHISPER_MODEL_PATH_REQUIRED",
    ):
        get_configured_caption_adapter(
            FASTER_WHISPER_QUALITY_TEST_BACKEND,
            narration=_script().narration,
        )


def test_faster_whisper_adapter_uses_audio_word_timestamps_and_pinned_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio_path = tmp_path / "audio.wav"
    model_path = tmp_path / "model"
    model_path.mkdir()
    _write_wav(_script().narration, audio_path, "pf_test", "p")
    calls: list[dict[str, object]] = []

    class FakeModel:
        def transcribe(self, path: str, **options: object) -> tuple[object, object]:
            calls.append({"path": path, **options})
            words = [
                SimpleNamespace(word=text, start=index * 0.45, end=index * 0.45 + 0.35)
                for index, text in enumerate(_script().narration.split())
            ]
            return iter([SimpleNamespace(words=words)]), SimpleNamespace(language="pt")

    monkeypatch.setattr(
        captions,
        "_verified_faster_whisper_snapshot",
        lambda path: path.resolve(),
    )
    monkeypatch.setattr(
        captions,
        "_load_faster_whisper_runtime",
        lambda _path: SimpleNamespace(model=FakeModel()),
    )
    adapter = get_configured_caption_adapter(
        FASTER_WHISPER_QUALITY_TEST_BACKEND,
        narration=_script().narration,
        faster_whisper_config=FasterWhisperQualityTestConfig(model_path=model_path),
    )

    result = adapter(audio_path, "pt")

    assert result.backend == FASTER_WHISPER_QUALITY_TEST_BACKEND
    assert FASTER_WHISPER_MODEL_REVISION in result.model_id
    assert result.license_id.startswith("MIT")
    assert result.commercial_use is True
    assert tuple(word.text for word in result.words) == tuple(
        _script().narration.split()
    )
    assert result.words[0].start_seconds == Decimal("0E-10")
    assert calls[0]["language"] == "pt"
    assert calls[0]["word_timestamps"] is True
    assert calls[0]["vad_filter"] is True
    assert calls[0]["initial_prompt"] == _script().narration


def test_faster_whisper_snapshot_digest_is_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = tmp_path / "model"
    model_path.mkdir()
    model = model_path / "model.bin"
    model.write_bytes(b"abc")
    monkeypatch.setattr(
        captions,
        "_FASTER_WHISPER_SNAPSHOT_FILES",
        {"model.bin": (3, "sha256", hashlib.sha256(b"abc").hexdigest())},
    )
    assert captions._verified_faster_whisper_snapshot(model_path) == model_path.resolve()

    model.write_bytes(b"abd")
    with pytest.raises(
        CaptionPermanentAdapterError,
        match="CONTENT_CAPTIONS_FASTER_WHISPER_MODEL_DIGEST_MISMATCH",
    ):
        captions._verified_faster_whisper_snapshot(model_path)


def test_faster_whisper_repairs_zero_duration_word_inside_next_interval() -> None:
    segments = [
        SimpleNamespace(
            words=[
                SimpleNamespace(word="prepara", start=3.54, end=4.04),
                SimpleNamespace(word="o", start=4.04, end=4.04),
                SimpleNamespace(word="cérebro", start=4.04, end=4.46),
            ]
        )
    ]

    words = captions._faster_whisper_words(iter(segments))

    assert words[1].start_seconds == Decimal("4.0400000000")
    assert words[1].end_seconds == Decimal("4.0600000000")
    assert words[2].start_seconds == Decimal("4.0600000000")
    assert words[2].end_seconds == Decimal("4.4600000000")


def test_faster_whisper_runtime_is_cuda_only_local_and_version_pinned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    constructor_calls: list[tuple[str, dict[str, object]]] = []

    class FakeFasterWhisper:
        @staticmethod
        def WhisperModel(model_path: str, **options: object) -> object:
            constructor_calls.append((model_path, options))
            return object()

    versions = {
        "faster-whisper": captions.FASTER_WHISPER_PACKAGE_VERSION,
        "ctranslate2": captions.CTRANSLATE2_PACKAGE_VERSION,
    }
    modules = {
        "faster_whisper": FakeFasterWhisper(),
        "ctranslate2": SimpleNamespace(get_cuda_device_count=lambda: 1),
    }
    captions._load_faster_whisper_runtime.cache_clear()
    monkeypatch.setattr(captions.importlib.metadata, "version", versions.__getitem__)
    monkeypatch.setattr(captions.importlib, "import_module", modules.__getitem__)

    runtime = captions._load_faster_whisper_runtime(str(tmp_path))

    assert runtime.model is not None
    assert constructor_calls == [
        (
            str(tmp_path),
            {
                "device": "cuda",
                "device_index": 0,
                "compute_type": "float16",
                "local_files_only": True,
            },
        )
    ]
    captions._load_faster_whisper_runtime.cache_clear()


def test_faster_whisper_backend_routes_a4_to_gpu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        a1_executor,
        "settings",
        SimpleNamespace(content_caption_backend=FASTER_WHISPER_QUALITY_TEST_BACKEND),
    )

    assert a1_executor._captions_queue() is QueueClass.GPU


async def test_a1_continuation_runs_a2_a3_then_a4_before_completing(
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
            gpu_power_watts=400,
        ),
    )
    first = await execute_content_script_run(
        session,
        idempotency_key="content-a1-a2-a4-chain",
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
        idempotency_key="content-a1-a2-a4-chain",
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
    assert [(step.ordinal, step.name) for step in steps] == [
        (1, "generate-script-a1"),
        (2, "synthesize-tts-a2"),
        (3, "prepare-visuals-a3"),
        (4, "generate-captions-a4"),
    ]
    assert run.output_payload is not None
    assert isinstance(run.output_payload["caption_artifact_id"], int)
    assert len(run.output_payload["visual_artifact_ids"]) == 1


async def test_a4_creates_verified_ass_observations_without_changing_audio(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    run, approval, audio_artifact, approval_input = await _running_after_tts(
        session,
        storage_root,
        idempotency_key="content-a4-success",
    )
    audio_path = storage_root / audio_artifact.relative_path
    digest_before = hashlib.sha256(audio_path.read_bytes()).hexdigest()

    result = await execute_approved_caption_step(
        session,
        run=run,
        approval=approval,
        approval_input_payload=approval_input,
        audio_artifact=audio_artifact,
        narration=_script().narration,
        idempotency_key=run.idempotency_key,
        storage_root=storage_root,
        adapter=_caption_adapter,
        language_code="pt",
        gpu_power_watts=400,
        timeout_seconds=5,
    )
    step = await session.get(StepRun, result.step_run_id)
    artifact = await session.get(Artifact, result.artifact_id)
    metrics = list(
        (
            await session.scalars(
                select(MetricPoint).where(MetricPoint.source == "content-engine:a4")
            )
        ).all()
    )
    ledger = list(
        (
            await session.scalars(
                select(LedgerEntry).where(LedgerEntry.source == "content-engine:a4")
            )
        ).all()
    )

    assert run.status == RunStatus.SUCCEEDED.value
    assert step is not None and step.status == RunStatus.SUCCEEDED.value
    assert step.queue == "gpu" and step.ordinal == 4
    assert artifact is not None and artifact.artifact_type == "caption_ass"
    caption_text = (storage_root / artifact.relative_path).read_text(encoding="utf-8")
    assert "[Events]" in caption_text and "{\\kf" in caption_text
    assert {metric.name for metric in metrics} == {
        "content_caption_timed_word_count",
        "content_caption_transcript_match_ratio",
        "content_caption_energy_estimate_kwh",
    }
    assert len(ledger) == 1 and str(ledger[0].amount) == "0E-10"
    assert hashlib.sha256(audio_path.read_bytes()).hexdigest() == digest_before
    assert run.output_payload is not None
    assert run.output_payload["caption_artifact_id"] == artifact.id
    assert not list((storage_root / "content-engine").rglob("*.tmp.ass"))


async def test_approved_text_timing_completes_a4_without_gpu_energy(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    run, approval, audio_artifact, approval_input = await _running_after_tts(
        session,
        storage_root,
        idempotency_key="content-a4-approved-text-timing",
    )
    adapter = get_configured_caption_adapter(
        "approved_text_timing_quality_test",
        narration=_script().narration,
    )

    result = await execute_approved_caption_step(
        session,
        run=run,
        approval=approval,
        approval_input_payload=approval_input,
        audio_artifact=audio_artifact,
        narration=_script().narration,
        idempotency_key=run.idempotency_key,
        storage_root=storage_root,
        adapter=adapter,
        language_code="pt",
        gpu_power_watts=400,
        timeout_seconds=5,
        queue=QueueClass.CPU,
    )

    step = await session.get(StepRun, result.step_run_id)
    metrics = (
        await session.scalars(
            select(MetricPoint).where(MetricPoint.run_id == result.run_id)
        )
    ).all()
    assert result.run_status == RunStatus.SUCCEEDED.value
    assert step is not None and step.queue == QueueClass.CPU.value
    assert "content_caption_timed_word_count" in {metric.name for metric in metrics}
    assert "content_caption_energy_estimate_kwh" not in {
        metric.name for metric in metrics
    }


async def test_a4_rejects_missing_commercial_rights_without_artifact(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    run, approval, audio_artifact, approval_input = await _running_after_tts(
        session,
        storage_root,
        idempotency_key="content-a4-rights",
    )

    def unlicensed(_audio: Path, _language: str) -> CaptionAdapterResult:
        return replace(_caption_result(), commercial_use=False)

    result = await execute_approved_caption_step(
        session,
        run=run,
        approval=approval,
        approval_input_payload=approval_input,
        audio_artifact=audio_artifact,
        narration=_script().narration,
        idempotency_key=run.idempotency_key,
        storage_root=storage_root,
        adapter=unlicensed,
        language_code="pt",
        gpu_power_watts=400,
        timeout_seconds=5,
    )
    step = await session.get(StepRun, result.step_run_id)
    artifacts = list(
        (
            await session.scalars(
                select(Artifact).where(Artifact.artifact_type == "caption_ass")
            )
        ).all()
    )

    assert run.status == RunStatus.FAILED.value
    assert step is not None and step.status == RunStatus.FAILED.value
    assert step.error is not None
    assert step.error["code"] == "CONTENT_CAPTIONS_COMMERCIAL_RIGHTS_REQUIRED"
    assert artifacts == []


async def test_a4_rejects_overlapping_word_timings(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    run, approval, audio_artifact, approval_input = await _running_after_tts(
        session,
        storage_root,
        idempotency_key="content-a4-timing",
    )

    def invalid_timing(_audio: Path, _language: str) -> CaptionAdapterResult:
        valid = _caption_result()
        words = list(valid.words)
        words[1] = replace(
            words[1],
            start_seconds=Decimal("0.20"),
            end_seconds=Decimal("0.60"),
        )
        return replace(valid, words=tuple(words))

    result = await execute_approved_caption_step(
        session,
        run=run,
        approval=approval,
        approval_input_payload=approval_input,
        audio_artifact=audio_artifact,
        narration=_script().narration,
        idempotency_key=run.idempotency_key,
        storage_root=storage_root,
        adapter=invalid_timing,
        language_code="pt",
        gpu_power_watts=400,
        timeout_seconds=5,
    )
    step = await session.get(StepRun, result.step_run_id)

    assert run.status == RunStatus.FAILED.value
    assert step is not None and step.error is not None
    assert step.error["code"] == "CONTENT_CAPTIONS_INVALID_TIMING"


async def test_a4_retries_transient_adapter_failure_once(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    run, approval, audio_artifact, approval_input = await _running_after_tts(
        session,
        storage_root,
        idempotency_key="content-a4-retry",
    )
    attempts = 0

    def transient(_audio: Path, _language: str) -> CaptionAdapterResult:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise CaptionRetryableAdapterError("temporary model failure")
        return _caption_result()

    async def no_sleep(_seconds: float) -> None:
        return None

    result = await execute_approved_caption_step(
        session,
        run=run,
        approval=approval,
        approval_input_payload=approval_input,
        audio_artifact=audio_artifact,
        narration=_script().narration,
        idempotency_key=run.idempotency_key,
        storage_root=storage_root,
        adapter=transient,
        language_code="pt",
        gpu_power_watts=400,
        timeout_seconds=5,
        sleep=no_sleep,
    )
    steps = list(
        (
            await session.scalars(
                select(StepRun)
                .where(StepRun.name == "generate-captions-a4")
                .order_by(StepRun.attempt)
            )
        ).all()
    )

    assert attempts == 2 and result.artifact_id is not None
    assert [step.status for step in steps] == [
        RunStatus.FAILED.value,
        RunStatus.SUCCEEDED.value,
    ]


async def test_a4_replays_completed_step_without_regenerating_artifact(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    run, approval, audio_artifact, approval_input = await _running_after_tts(
        session,
        storage_root,
        idempotency_key="content-a4-replay",
    )
    adapter_calls = 0

    def counted_adapter(_audio: Path, _language: str) -> CaptionAdapterResult:
        nonlocal adapter_calls
        adapter_calls += 1
        return _caption_result()

    first = await execute_approved_caption_step(
        session,
        run=run,
        approval=approval,
        approval_input_payload=approval_input,
        audio_artifact=audio_artifact,
        narration=_script().narration,
        idempotency_key=run.idempotency_key,
        storage_root=storage_root,
        adapter=counted_adapter,
        language_code="pt",
        gpu_power_watts=400,
        timeout_seconds=5,
        finalize_run=False,
    )
    second = await execute_approved_caption_step(
        session,
        run=run,
        approval=approval,
        approval_input_payload=approval_input,
        audio_artifact=audio_artifact,
        narration=_script().narration,
        idempotency_key=run.idempotency_key,
        storage_root=storage_root,
        adapter=counted_adapter,
        language_code="pt",
        gpu_power_watts=400,
        timeout_seconds=5,
        finalize_run=False,
    )
    artifacts = list(
        (
            await session.scalars(
                select(Artifact).where(Artifact.artifact_type == "caption_ass")
            )
        ).all()
    )

    assert run.status == RunStatus.RUNNING.value
    assert adapter_calls == 1
    assert first.replayed is False and second.replayed is True
    assert first.artifact_id == second.artifact_id
    assert len(artifacts) == 1


async def test_a4_rejects_tampered_audio_before_creating_step(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    run, approval, audio_artifact, approval_input = await _running_after_tts(
        session,
        storage_root,
        idempotency_key="content-a4-tampered-audio",
    )
    with (storage_root / audio_artifact.relative_path).open("ab") as audio:
        audio.write(b"tampered")

    with pytest.raises(ValueError, match="size does not match"):
        await execute_approved_caption_step(
            session,
            run=run,
            approval=approval,
            approval_input_payload=approval_input,
            audio_artifact=audio_artifact,
            narration=_script().narration,
            idempotency_key=run.idempotency_key,
            storage_root=storage_root,
            adapter=_caption_adapter,
            language_code="pt",
            gpu_power_watts=400,
            timeout_seconds=5,
        )
    caption_steps = list(
        (
            await session.scalars(
                select(StepRun).where(StepRun.name == "generate-captions-a4")
            )
        ).all()
    )

    assert run.status == RunStatus.RUNNING.value
    assert caption_steps == []


async def test_a4_marks_run_failed_when_evidence_registration_fails(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_root = tmp_path / "storage"
    run, approval, audio_artifact, approval_input = await _running_after_tts(
        session,
        storage_root,
        idempotency_key="content-a4-evidence-failure",
    )

    async def fail_registration(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("simulated artifact registry failure")

    monkeypatch.setattr(captions, "register_local_artifact", fail_registration)
    result = await execute_approved_caption_step(
        session,
        run=run,
        approval=approval,
        approval_input_payload=approval_input,
        audio_artifact=audio_artifact,
        narration=_script().narration,
        idempotency_key=run.idempotency_key,
        storage_root=storage_root,
        adapter=_caption_adapter,
        language_code="pt",
        gpu_power_watts=400,
        timeout_seconds=5,
    )
    caption_artifacts = list(
        (
            await session.scalars(
                select(Artifact).where(Artifact.artifact_type == "caption_ass")
            )
        ).all()
    )

    assert result.artifact_id is None
    assert run.status == RunStatus.FAILED.value
    assert run.error is not None
    assert run.error["code"] == "CONTENT_CAPTIONS_EVIDENCE_PERSIST_FAILED"
    assert caption_artifacts == []
