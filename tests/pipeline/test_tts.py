"""Approved Content Engine A2 TTS contract without a real model or GPU."""

from __future__ import annotations

import hashlib
import subprocess
import wave
from collections.abc import AsyncGenerator
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
from src.pipeline import tts
from src.pipeline.a1_executor import execute_content_script_run
from src.pipeline.script_gen import ScriptResult
from src.pipeline.tts import (
    KOKORO_QUALITY_TEST_BACKEND,
    QWEN3_CLONE_QUALITY_TEST_BACKEND,
    Qwen3CloneQualityTestConfig,
    TTSAdapterResult,
    TTSPermanentAdapterError,
    TTSRetryableAdapterError,
    execute_approved_tts_step,
    get_configured_tts_synthesizer,
    synthesize_kokoro_quality_test,
)
from src.platform.approval_service import ApprovalGateError, decide_approval
from src.platform.models import (
    Approval,
    ApprovalStatus,
    Artifact,
    LedgerEntry,
    MetricPoint,
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


def _input_payload() -> dict[str, object]:
    return {
        "topic": "Sono e memória",
        "persona": "Divulgação científica clara e rigorosa",
        "format": "short",
        "recent_openings": [],
    }


def _script() -> ScriptResult:
    return ScriptResult(
        angle="O sono reorganiza memórias.",
        outline="1. Hook\n2. Explicação\n3. Fecho",
        full_script="Roteiro completo sobre sono e memória.",
        hook="Seu cérebro trabalha enquanto você dorme.",
        narration="Seu cérebro reorganiza memórias enquanto você dorme.",
    )


def _write_valid_wav(
    _text: str,
    destination: Path,
    voice_id: str,
    language_code: str,
) -> TTSAdapterResult:
    with wave.open(str(destination), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24_000)
        audio.writeframes(b"\x00\x00" * 24_000)
    return TTSAdapterResult(
        backend="fake-local",
        model_id="fake-model-v1",
        voice_id=voice_id,
        language_code=language_code,
        license_id="TEST-ONLY",
    )


def test_configured_tts_backend_is_explicit_and_fail_closed(tmp_path: Path) -> None:
    assert (
        get_configured_tts_synthesizer(KOKORO_QUALITY_TEST_BACKEND)
        is synthesize_kokoro_quality_test
    )
    for backend, expected_code in (
        ("disabled", "CONTENT_TTS_DISABLED"),
        ("kokoro", "CONTENT_TTS_BACKEND_UNREVIEWED"),
    ):
        with pytest.raises(TTSPermanentAdapterError, match=expected_code):
            get_configured_tts_synthesizer(backend)(
                "texto aprovado",
                tmp_path / "must-not-exist.wav",
                "pf_dora",
                "p",
            )


def test_kokoro_quality_test_writes_one_bounded_pcm_wav(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class FakePipeline:
        def __call__(self, chunks: list[str], **_kwargs: object) -> list[object]:
            calls.extend(chunks)
            return [SimpleNamespace(audio=object())]

    runtime = SimpleNamespace(pipeline=FakePipeline(), numpy=object())
    monkeypatch.setattr(tts, "_load_kokoro_runtime", lambda: runtime)
    monkeypatch.setattr(tts, "_load_kokoro_voice", lambda _voice_id: object())
    monkeypatch.setattr(
        tts,
        "_kokoro_audio_to_pcm_s16le",
        lambda _audio, _numpy: b"\x00\x00" * 240,
    )
    narration = " ".join(["Frase aprovada sobre ciência."] * 80)
    destination = tmp_path / "kokoro-quality-test.wav"

    result = synthesize_kokoro_quality_test(
        narration,
        destination,
        "pf_dora",
        "p",
    )

    assert result.backend == KOKORO_QUALITY_TEST_BACKEND
    assert "scope=quality-test-only" in result.license_id
    assert "provenance-unverified" in result.license_id
    assert calls and all(1 <= len(chunk) <= 400 for chunk in calls)
    assert " ".join(calls).split() == narration.split()
    with wave.open(str(destination), "rb") as audio:
        assert audio.getnchannels() == 1
        assert audio.getsampwidth() == 2
        assert audio.getframerate() == 24_000
        assert audio.getnframes() == 240 * len(calls)


@pytest.mark.parametrize(
    ("voice_id", "language_code", "expected_code"),
    [
        ("pf_dora", "a", "CONTENT_TTS_KOKORO_REQUIRES_PT_BR"),
        ("af_heart", "p", "CONTENT_TTS_KOKORO_VOICE_NOT_REVIEWED"),
    ],
)
def test_kokoro_quality_test_rejects_unreviewed_profiles_before_loading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    voice_id: str,
    language_code: str,
    expected_code: str,
) -> None:
    monkeypatch.setattr(
        tts,
        "_load_kokoro_runtime",
        lambda: pytest.fail("runtime must not load"),
    )

    with pytest.raises(TTSPermanentAdapterError, match=expected_code):
        synthesize_kokoro_quality_test(
            "texto aprovado",
            tmp_path / "must-not-exist.wav",
            voice_id,
            language_code,
        )


async def _approved_a1(
    session: AsyncSession,
    storage_root: Path,
    *,
    idempotency_key: str,
) -> tuple[Run, Approval, Artifact, dict[str, object]]:
    result = await execute_content_script_run(
        session,
        idempotency_key=idempotency_key,
        input_payload=_input_payload(),
        script_generator=lambda *_args: _script(),
        storage_root=storage_root,
    )
    run = await session.get(Run, result.run_id)
    approval = await session.get(Approval, result.approval_id)
    artifact = await session.get(Artifact, result.artifact_id)
    assert run is not None and approval is not None and artifact is not None
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
        "step_run_id": result.step_run_id,
        "artifact_id": artifact.id,
        "artifact_sha256": artifact.sha256,
        "narration_sha256": hashlib.sha256(
            _script().narration.encode("utf-8")
        ).hexdigest(),
    }
    return run, approval, artifact, approval_input


def _qwen3_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Qwen3CloneQualityTestConfig:
    """Small stand-ins for the separate environment, the weights and the reference."""
    model_dir = tmp_path / "model"
    (model_dir / "speech_tokenizer").mkdir(parents=True)
    (model_dir / "model.safetensors").write_bytes(b"main-weights")
    (model_dir / "speech_tokenizer" / "model.safetensors").write_bytes(b"tokenizer-weights")
    monkeypatch.setattr(
        tts,
        "QWEN3_BASE_MODEL_SHA256",
        {
            "model.safetensors": hashlib.sha256(b"main-weights").hexdigest(),
            "speech_tokenizer/model.safetensors": hashlib.sha256(
                b"tokenizer-weights"
            ).hexdigest(),
        },
    )
    python = tmp_path / "python.exe"
    python.write_bytes(b"not-a-real-interpreter")
    reference_audio = tmp_path / "reference.wav"
    reference_audio.write_bytes(b"reference-voice")
    reference_text = tmp_path / "reference.txt"
    reference_text.write_text("Texto lido na gravação.", encoding="utf-8")
    return Qwen3CloneQualityTestConfig(
        python_path=str(python),
        model_dir=str(model_dir),
        reference_audio_path=str(reference_audio),
        reference_text_path=str(reference_text),
        timeout_seconds=30,
    )


def test_qwen3_clone_backend_requires_its_configuration(tmp_path: Path) -> None:
    with pytest.raises(TTSPermanentAdapterError, match="CONTENT_TTS_QWEN3_CONFIG_REQUIRED"):
        get_configured_tts_synthesizer(QWEN3_CLONE_QUALITY_TEST_BACKEND)(
            "Narração.",
            tmp_path / "narration.wav",
            "owner-reference",
            "pt",
        )


def test_qwen3_clone_runs_the_pinned_model_offline_in_the_separate_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _qwen3_files(tmp_path, monkeypatch)
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command: list[str], **options: object) -> SimpleNamespace:
        calls.append((command, options))
        arguments = dict(zip(command[2::2], command[3::2], strict=True))
        assert Path(arguments["--text-file"]).read_text(encoding="utf-8") == "Narração aprovada."
        _write_valid_wav("", Path(arguments["--output"]), "owner-reference", "pt")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(tts.subprocess, "run", fake_run)
    destination = tmp_path / "out" / "narration.wav"
    destination.parent.mkdir()

    result = get_configured_tts_synthesizer(
        QWEN3_CLONE_QUALITY_TEST_BACKEND,
        qwen3_clone_config=config,
    )("Narração aprovada.", destination, "owner-reference", "pt")

    command, options = calls[0]
    assert command[0] == str(Path(config.python_path).resolve())
    assert command[1].endswith("qwen3_clone_runner.py") and Path(command[1]).is_file()
    assert command[command.index("--model-dir") + 1] == config.model_dir
    assert command[command.index("--output") + 1] == str(destination)
    assert options["shell"] is False and options["timeout"] == 30
    environment = options["env"]
    assert isinstance(environment, dict)
    assert environment["HF_HUB_OFFLINE"] == "1" and environment["TRANSFORMERS_OFFLINE"] == "1"
    assert destination.is_file()
    assert result.backend == QWEN3_CLONE_QUALITY_TEST_BACKEND
    assert result.voice_id == "owner-reference" and result.language_code == "pt"
    assert result.model_id.startswith(
        "Qwen/Qwen3-TTS-12Hz-1.7B-Base@fd4b254389122332181a7c3db7f27e918eec64e3#sha256:"
    )
    reference_digest = hashlib.sha256(b"reference-voice").hexdigest()[:16]
    assert f"voice=reference-recording#sha256:{reference_digest}" in result.license_id
    assert "training-data=undisclosed" in result.license_id
    assert "scope=quality-test-only" in result.license_id
    assert max(len(result.model_id), len(result.license_id)) <= 200
    # The narration is handed over through a temporary file that does not outlive the call.
    assert list(destination.parent.iterdir()) == [destination]


@pytest.mark.parametrize(
    ("break_it", "expected_code"),
    [
        ("language", "CONTENT_TTS_QWEN3_REQUIRES_PT"),
        ("python", "CONTENT_TTS_QWEN3_PYTHON_UNAVAILABLE"),
        ("weights_missing", "CONTENT_TTS_QWEN3_MODEL_UNAVAILABLE"),
        ("weights_changed", "CONTENT_TTS_QWEN3_MODEL_HASH_MISMATCH"),
        ("reference_missing", "CONTENT_TTS_QWEN3_REFERENCE_UNAVAILABLE"),
        ("reference_empty", "CONTENT_TTS_QWEN3_REFERENCE_INVALID"),
    ],
)
def test_qwen3_clone_fails_closed_before_starting_the_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    break_it: str,
    expected_code: str,
) -> None:
    config = _qwen3_files(tmp_path, monkeypatch)
    language = "pt"
    if break_it == "language":
        language = "p"
    elif break_it == "python":
        Path(config.python_path).unlink()
    elif break_it == "weights_missing":
        (Path(config.model_dir) / "speech_tokenizer" / "model.safetensors").unlink()
    elif break_it == "weights_changed":
        (Path(config.model_dir) / "model.safetensors").write_bytes(b"other-weights!!")
    elif break_it == "reference_missing":
        Path(config.reference_audio_path).unlink()
    elif break_it == "reference_empty":
        Path(config.reference_text_path).write_bytes(b"")

    def must_not_run(*_args: object, **_options: object) -> SimpleNamespace:
        raise AssertionError("the model must not start")

    monkeypatch.setattr(tts.subprocess, "run", must_not_run)

    with pytest.raises(TTSPermanentAdapterError, match=expected_code):
        get_configured_tts_synthesizer(
            QWEN3_CLONE_QUALITY_TEST_BACKEND,
            qwen3_clone_config=config,
        )("Narração.", tmp_path / "narration.wav", "owner-reference", language)


@pytest.mark.parametrize("failure", ["exit_code", "timeout"])
def test_qwen3_clone_inference_failures_are_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    config = _qwen3_files(tmp_path, monkeypatch)

    def fake_run(command: list[str], **_options: object) -> SimpleNamespace:
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 30)
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(tts.subprocess, "run", fake_run)

    with pytest.raises(TTSRetryableAdapterError, match="CONTENT_TTS_QWEN3_INFERENCE_FAILED"):
        get_configured_tts_synthesizer(
            QWEN3_CLONE_QUALITY_TEST_BACKEND,
            qwen3_clone_config=config,
        )("Narração.", tmp_path / "narration.wav", "owner-reference", "pt")


async def test_approved_tts_creates_verified_audio_and_observations(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    run, approval, script_artifact, approval_input = await _approved_a1(
        session,
        storage_root,
        idempotency_key="content-a2-success",
    )

    result = await execute_approved_tts_step(
        session,
        run=run,
        approval=approval,
        approval_input_payload=approval_input,
        source_artifact=script_artifact,
        narration=_script().narration,
        idempotency_key=run.idempotency_key,
        storage_root=storage_root,
        synthesizer=_write_valid_wav,
        voice_id="pf_test",
        language_code="p",
        gpu_power_watts=400,
        timeout_seconds=5,
    )

    step = await session.get(StepRun, result.step_run_id)
    audio_artifact = await session.get(Artifact, result.artifact_id)
    metrics = list(
        (
            await session.scalars(
                select(MetricPoint).where(MetricPoint.source == "content-engine:a2")
            )
        ).all()
    )
    ledger = list(
        (
            await session.scalars(
                select(LedgerEntry).where(LedgerEntry.source == "content-engine:a2")
            )
        ).all()
    )

    assert run.status == RunStatus.SUCCEEDED.value, run.error
    assert step is not None and step.status == RunStatus.SUCCEEDED.value
    assert step.queue == "gpu" and step.ordinal == 2
    assert step.approval_id == approval.id
    assert audio_artifact is not None
    assert audio_artifact.artifact_type == "narration_audio"
    assert audio_artifact.media_type == "audio/wav"
    assert audio_artifact.step_run_id == step.id
    audio_artifact_path = storage_root / audio_artifact.relative_path
    assert audio_artifact_path.is_file()
    assert len(metrics) == 2
    assert {metric.name for metric in metrics} == {
        "content_tts_audio_duration_seconds",
        "content_tts_energy_estimate_kwh",
    }
    duration = next(
        metric for metric in metrics if metric.name == "content_tts_audio_duration_seconds"
    )
    assert str(duration.value) == "1.0000000000"
    assert len(ledger) == 1 and str(ledger[0].amount) == "0E-10"
    assert run.output_payload is not None
    assert run.output_payload["tts_artifact_id"] == audio_artifact.id
    assert not list(audio_artifact_path.parent.glob("*.tmp.wav"))


async def test_tts_invalid_wav_fails_closed_without_audio_artifact(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    run, approval, script_artifact, approval_input = await _approved_a1(
        session,
        storage_root,
        idempotency_key="content-a2-invalid-wav",
    )

    def invalid_audio(
        _text: str,
        destination: Path,
        voice_id: str,
        language_code: str,
    ) -> TTSAdapterResult:
        destination.write_bytes(b"not-a-wav")
        return TTSAdapterResult(
            backend="fake-local",
            model_id="fake-model-v1",
            voice_id=voice_id,
            language_code=language_code,
            license_id="TEST-ONLY",
        )

    result = await execute_approved_tts_step(
        session,
        run=run,
        approval=approval,
        approval_input_payload=approval_input,
        source_artifact=script_artifact,
        narration=_script().narration,
        idempotency_key=run.idempotency_key,
        storage_root=storage_root,
        synthesizer=invalid_audio,
        voice_id="pf_test",
        language_code="p",
        gpu_power_watts=400,
        timeout_seconds=5,
    )

    artifacts = list(
        (
            await session.scalars(
                select(Artifact).where(Artifact.artifact_type == "narration_audio")
            )
        ).all()
    )
    assert result.run_status == RunStatus.FAILED.value
    assert run.error is not None
    assert run.error["code"] == "CONTENT_TTS_INVALID_WAV_SIZE"
    assert run.error["retryable"] is False
    assert artifacts == []
    assert not list(storage_root.rglob("*.tmp.wav"))


async def test_tts_refuses_approval_payload_mismatch_before_adapter_call(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    run, approval, script_artifact, approval_input = await _approved_a1(
        session,
        storage_root,
        idempotency_key="content-a2-approval-mismatch",
    )
    called = False

    def must_not_run(*_args: object) -> TTSAdapterResult:
        nonlocal called
        called = True
        raise AssertionError("adapter must not run")

    with pytest.raises(ValueError, match="narration"):
        await execute_approved_tts_step(
            session,
            run=run,
            approval=approval,
            approval_input_payload=approval_input,
            source_artifact=script_artifact,
            narration="different narration",
            idempotency_key=run.idempotency_key,
            storage_root=storage_root,
            synthesizer=must_not_run,
            voice_id="pf_test",
            language_code="p",
            gpu_power_watts=400,
            timeout_seconds=5,
        )

    approval_input["artifact_sha256"] = "0" * 64
    with pytest.raises(ApprovalGateError, match="payload"):
        await execute_approved_tts_step(
            session,
            run=run,
            approval=approval,
            approval_input_payload=approval_input,
            source_artifact=script_artifact,
            narration=_script().narration,
            idempotency_key=run.idempotency_key,
            storage_root=storage_root,
            synthesizer=must_not_run,
            voice_id="pf_test",
            language_code="p",
            gpu_power_watts=400,
            timeout_seconds=5,
        )

    steps = list(
        (
            await session.scalars(
                select(StepRun).where(StepRun.name == "synthesize-tts-a2")
            )
        ).all()
    )
    assert called is False
    assert run.status == RunStatus.RUNNING.value
    assert steps == []


async def test_tts_retries_one_local_backend_failure_without_duplicate_audio(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    run, approval, script_artifact, approval_input = await _approved_a1(
        session,
        storage_root,
        idempotency_key="content-a2-retry",
    )
    calls = 0

    def flaky(
        text: str,
        destination: Path,
        voice_id: str,
        language_code: str,
    ) -> TTSAdapterResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TTSRetryableAdapterError("temporary local backend failure")
        return _write_valid_wav(text, destination, voice_id, language_code)

    async def no_sleep(_seconds: float) -> None:
        return None

    result = await execute_approved_tts_step(
        session,
        run=run,
        approval=approval,
        approval_input_payload=approval_input,
        source_artifact=script_artifact,
        narration=_script().narration,
        idempotency_key=run.idempotency_key,
        storage_root=storage_root,
        synthesizer=flaky,
        voice_id="pf_test",
        language_code="p",
        gpu_power_watts=400,
        timeout_seconds=5,
        sleep=no_sleep,
    )
    attempts = list(
        (
            await session.scalars(
                select(StepRun)
                .where(StepRun.name == "synthesize-tts-a2")
                .order_by(StepRun.attempt)
            )
        ).all()
    )

    assert calls == 2
    assert result.run_status == RunStatus.SUCCEEDED.value
    assert [attempt.status for attempt in attempts] == ["failed", "succeeded"]
    assert attempts[0].error is not None
    assert attempts[0].error["code"] == "CONTENT_TTS_BACKEND_UNAVAILABLE"
    assert len(list(storage_root.rglob("narration.wav"))) == 1


async def test_tts_evidence_failure_closes_run_as_retryable(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_root = tmp_path / "storage"
    run, approval, script_artifact, approval_input = await _approved_a1(
        session,
        storage_root,
        idempotency_key="content-a2-evidence-failure",
    )

    async def fail_registration(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("private filesystem detail")

    monkeypatch.setattr(tts, "register_local_artifact", fail_registration)
    result = await execute_approved_tts_step(
        session,
        run=run,
        approval=approval,
        approval_input_payload=approval_input,
        source_artifact=script_artifact,
        narration=_script().narration,
        idempotency_key=run.idempotency_key,
        storage_root=storage_root,
        synthesizer=_write_valid_wav,
        voice_id="pf_test",
        language_code="p",
        gpu_power_watts=400,
        timeout_seconds=5,
    )
    a2_metrics = list(
        (
            await session.scalars(
                select(MetricPoint).where(MetricPoint.source == "content-engine:a2")
            )
        ).all()
    )

    assert result.run_status == RunStatus.FAILED.value
    assert run.error == {
        "code": "CONTENT_TTS_EVIDENCE_PERSIST_FAILED",
        "exception_type": "RuntimeError",
        "retryable": True,
        "timed_out": False,
    }
    assert "private filesystem detail" not in str(run.error)
    assert a2_metrics == []
    assert len(list(storage_root.rglob("narration.wav"))) == 1
