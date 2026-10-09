"""Auditable A4 caption alignment diagnostic over verified local evidence."""

from __future__ import annotations

import hashlib
import json
import math
import wave
from collections.abc import AsyncGenerator
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.core.database import Base
from src.pipeline.a1_executor import execute_content_script_run
from src.pipeline.caption_alignment import (
    REASON_CAPTIONS_OVER_SILENCE,
    REASON_END_OFFSET,
    REASON_EVENTS_BEYOND_AUDIO,
    REASON_ONSET_OFFSET,
    STATUS_PASS,
    STATUS_WARNING,
    CaptionAlignmentError,
    CaptionAlignmentParameters,
    build_alignment_parameters,
    evaluate_caption_alignment,
    load_caption_alignment_report,
    measure_caption_alignment,
)
from src.pipeline.captions import execute_approved_caption_step, get_configured_caption_adapter
from src.pipeline.script_gen import ScriptResult
from src.pipeline.tts import TTSAdapterResult, execute_approved_tts_step
from src.platform.approval_service import decide_approval
from src.platform.models import (
    Alert,
    Approval,
    ApprovalStatus,
    Artifact,
    Automation,
    MetricPoint,
    Run,
    RunStatus,
)

_FRAMERATE = 24_000
_NARRATION = (
    "O sono reorganiza memórias e fortalece o aprendizado do dia seguinte com calma."
)


def _script() -> ScriptResult:
    return ScriptResult(
        angle="Dormir reorganiza memórias.",
        outline="1. Gancho\n2. Explicação\n3. Fecho",
        full_script=_NARRATION,
        hook="Seu cérebro edita memórias enquanto você dorme.",
        narration=_NARRATION,
    )


def _write_wav(
    destination: Path,
    *,
    duration_seconds: float,
    bursts: tuple[tuple[float, float], ...],
    channels: int = 1,
) -> None:
    total_frames = int(_FRAMERATE * duration_seconds)
    frames = bytearray()
    for index in range(total_frames):
        moment = index / _FRAMERATE
        voiced = any(start <= moment < end for start, end in bursts)
        value = int(12_000 * math.sin(2 * math.pi * 220 * moment)) if voiced else 0
        frames += int(value).to_bytes(2, "little", signed=True) * channels
    with wave.open(str(destination), "wb") as audio:
        audio.setnchannels(channels)
        audio.setsampwidth(2)
        audio.setframerate(_FRAMERATE)
        audio.writeframes(bytes(frames))


def _ass(events: tuple[tuple[str, str], ...]) -> str:
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for start, end in events:
        lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,texto")
    return "\n".join(lines) + "\n"


def _parameters() -> CaptionAlignmentParameters:
    return CaptionAlignmentParameters()


# ── Pure measurement ──────────────────────────────────────────────────────────


def test_measurement_accepts_captions_that_follow_the_speech(tmp_path: Path) -> None:
    audio_path = tmp_path / "audio.wav"
    _write_wav(
        audio_path,
        duration_seconds=8.0,
        bursts=((0.0, 1.5), (3.0, 4.5), (6.0, 7.5)),
    )

    measurement = measure_caption_alignment(
        audio_path=audio_path,
        caption_text=_ass(
            (
                ("0:00:00.00", "0:00:01.50"),
                ("0:00:03.00", "0:00:04.50"),
                ("0:00:06.00", "0:00:07.50"),
            )
        ),
        parameters=_parameters(),
    )

    assert measurement.status == STATUS_PASS
    assert measurement.reasons == ()
    assert measurement.speech_segment_count == 3
    assert measurement.caption_block_count == 3
    assert measurement.speech_coverage_ratio >= Decimal("0.95")
    assert measurement.caption_outside_speech_ratio <= Decimal("0.05")
    assert measurement.onset_offset_seconds.copy_abs() <= Decimal("0.05")
    assert measurement.max_block_onset_offset_seconds is not None


def test_measurement_flags_captions_that_span_silence(tmp_path: Path) -> None:
    audio_path = tmp_path / "audio.wav"
    _write_wav(
        audio_path,
        duration_seconds=8.0,
        bursts=((0.0, 1.5), (3.0, 4.5), (6.0, 7.5)),
    )

    measurement = measure_caption_alignment(
        audio_path=audio_path,
        caption_text=_ass((("0:00:00.00", "0:00:08.00"),)),
        parameters=_parameters(),
    )

    assert measurement.status == STATUS_WARNING
    assert REASON_CAPTIONS_OVER_SILENCE in measurement.reasons
    assert measurement.caption_block_count == 1
    assert measurement.max_block_onset_offset_seconds is None
    assert measurement.caption_outside_speech_ratio > Decimal("0.25")


def test_measurement_flags_shifted_captions(tmp_path: Path) -> None:
    audio_path = tmp_path / "audio.wav"
    _write_wav(
        audio_path,
        duration_seconds=10.0,
        bursts=((0.0, 1.5), (3.0, 4.5), (6.0, 7.5)),
    )

    measurement = measure_caption_alignment(
        audio_path=audio_path,
        caption_text=_ass(
            (
                ("0:00:01.00", "0:00:02.50"),
                ("0:00:04.00", "0:00:05.50"),
                ("0:00:07.00", "0:00:08.50"),
            )
        ),
        parameters=_parameters(),
    )

    assert measurement.status == STATUS_WARNING
    assert REASON_ONSET_OFFSET in measurement.reasons
    assert REASON_END_OFFSET in measurement.reasons
    assert measurement.onset_offset_seconds >= Decimal("0.9")
    assert measurement.end_offset_seconds >= Decimal("0.9")


def test_measurement_flags_events_beyond_the_audio(tmp_path: Path) -> None:
    audio_path = tmp_path / "audio.wav"
    _write_wav(audio_path, duration_seconds=2.0, bursts=((0.0, 2.0),))

    measurement = measure_caption_alignment(
        audio_path=audio_path,
        caption_text=_ass(
            (("0:00:00.00", "0:00:02.00"), ("0:00:02.50", "0:00:04.00"))
        ),
        parameters=_parameters(),
    )

    assert measurement.status == STATUS_WARNING
    assert REASON_EVENTS_BEYOND_AUDIO in measurement.reasons
    assert measurement.events_beyond_audio == 1


def test_measurement_reports_a_fully_silent_track(tmp_path: Path) -> None:
    audio_path = tmp_path / "audio.wav"
    _write_wav(audio_path, duration_seconds=3.0, bursts=())

    measurement = measure_caption_alignment(
        audio_path=audio_path,
        caption_text=_ass((("0:00:00.00", "0:00:03.00"),)),
        parameters=_parameters(),
    )

    assert measurement.status == STATUS_WARNING
    assert REASON_CAPTIONS_OVER_SILENCE in measurement.reasons
    assert measurement.speech_segment_count == 0
    assert measurement.speech_seconds == Decimal("0.000000")


@pytest.mark.parametrize(
    "caption_text",
    [
        "[Events]\nFormat: Layer, Start, End\n",
        "[Events]\nDialogue: 0,bad,0:00:01.00,Default,,0,0,0,,texto\n",
        "[Events]\nDialogue: 0,0:00:02.00,0:00:01.00,Default,,0,0,0,,texto\n",
        "[Events]\nDialogue: 0,0:00:00.00,0:00:01.00\n",
    ],
)
def test_measurement_rejects_unparsable_captions(
    tmp_path: Path,
    caption_text: str,
) -> None:
    audio_path = tmp_path / "audio.wav"
    _write_wav(audio_path, duration_seconds=2.0, bursts=((0.0, 2.0),))

    with pytest.raises(CaptionAlignmentError) as error:
        measure_caption_alignment(
            audio_path=audio_path,
            caption_text=caption_text,
            parameters=_parameters(),
        )

    assert error.value.code == "CAPTION_ALIGNMENT_CAPTION_INVALID"


def test_measurement_rejects_audio_that_is_not_mono_pcm(tmp_path: Path) -> None:
    audio_path = tmp_path / "stereo.wav"
    _write_wav(audio_path, duration_seconds=1.0, bursts=((0.0, 1.0),), channels=2)

    with pytest.raises(CaptionAlignmentError) as error:
        measure_caption_alignment(
            audio_path=audio_path,
            caption_text=_ass((("0:00:00.00", "0:00:01.00"),)),
            parameters=_parameters(),
        )

    assert error.value.code == "CAPTION_ALIGNMENT_AUDIO_INVALID"


def test_alignment_parameters_reject_impossible_thresholds() -> None:
    with pytest.raises(ValueError, match="min_speech_coverage"):
        CaptionAlignmentParameters(min_speech_coverage=Decimal("0"))
    with pytest.raises(ValueError, match="boundary_tolerance_seconds"):
        CaptionAlignmentParameters(boundary_tolerance_seconds=Decimal("0"))


def test_alignment_parameters_come_from_validated_settings() -> None:
    from src.core.config import Settings

    parameters = build_alignment_parameters(
        Settings(
            content_caption_alignment_min_speech_coverage=0.8,
            content_caption_alignment_max_outside_speech=0.1,
            content_caption_alignment_tolerance_seconds=0.25,
        )
    )

    assert parameters.min_speech_coverage == Decimal("0.8")
    assert parameters.max_outside_speech == Decimal("0.1")
    assert parameters.boundary_tolerance_seconds == Decimal("0.25")


# ── Persisted diagnostic ──────────────────────────────────────────────────────


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as database_session:
        yield database_session
    await engine.dispose()


async def _run_with_captions(
    session: AsyncSession,
    storage_root: Path,
    *,
    idempotency_key: str,
    duration_seconds: float,
    bursts: tuple[tuple[float, float], ...],
) -> Run:
    """Drive A1 → A2 → A4 with local fakes and the real proportional A4 backend."""

    def synthesize(
        _text: str,
        destination: Path,
        voice_id: str,
        language_code: str,
    ) -> TTSAdapterResult:
        _write_wav(destination, duration_seconds=duration_seconds, bursts=bursts)
        return TTSAdapterResult(
            backend="fake-local",
            model_id="fake-tts-v1",
            voice_id=voice_id,
            language_code=language_code,
            license_id="TEST-ONLY",
        )

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
        "narration_sha256": hashlib.sha256(_NARRATION.encode("utf-8")).hexdigest(),
    }
    tts_result = await execute_approved_tts_step(
        session,
        run=run,
        approval=approval,
        approval_input_payload=approval_input,
        source_artifact=script_artifact,
        narration=_NARRATION,
        idempotency_key=idempotency_key,
        storage_root=storage_root,
        synthesizer=synthesize,
        voice_id="pf_test",
        language_code="p",
        gpu_power_watts=400,
        timeout_seconds=10,
        finalize_run=False,
    )
    audio_artifact = await session.get(Artifact, tts_result.artifact_id)
    assert audio_artifact is not None
    await execute_approved_caption_step(
        session,
        run=run,
        approval=approval,
        approval_input_payload=approval_input,
        audio_artifact=audio_artifact,
        narration=_NARRATION,
        idempotency_key=idempotency_key,
        storage_root=storage_root,
        adapter=get_configured_caption_adapter(
            "approved_text_timing_quality_test",
            narration=_NARRATION,
        ),
        language_code="pt",
        gpu_power_watts=400,
        timeout_seconds=10,
    )
    assert run.status == RunStatus.SUCCEEDED.value
    return run


async def _alignment_metrics(
    session: AsyncSession,
    *,
    run_id: int,
) -> list[MetricPoint]:
    rows = await session.execute(
        select(MetricPoint)
        .where(MetricPoint.run_id == run_id)
        .order_by(MetricPoint.id)
    )
    return [
        metric
        for metric in rows.scalars().all()
        if metric.name.startswith("content_caption_alignment_")
    ]


async def test_diagnostic_publishes_evidence_and_replays_without_duplicates(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    run = await _run_with_captions(
        session,
        tmp_path,
        idempotency_key="alignment-pass",
        duration_seconds=6.0,
        bursts=((0.0, 6.0),),
    )
    assert run.id is not None

    result = await evaluate_caption_alignment(
        session,
        run_id=run.id,
        storage_root=tmp_path,
        parameters=_parameters(),
    )

    assert result.measurement.status == STATUS_PASS
    assert result.alert_id is None
    assert result.replayed is False
    assert run.status == RunStatus.SUCCEEDED.value
    report = await session.get(Artifact, result.report_artifact_id)
    assert report is not None
    assert report.artifact_type == "caption_alignment_report"
    assert report.step_run_id is None
    assert report.sha256 == result.report_sha256
    metrics = await _alignment_metrics(session, run_id=run.id)
    assert sorted(metric.name for metric in metrics) == [
        "content_caption_alignment_end_offset_seconds",
        "content_caption_alignment_onset_offset_seconds",
        "content_caption_alignment_outside_speech_ratio",
        "content_caption_alignment_speech_coverage_ratio",
        "content_caption_alignment_speech_segment_count",
    ]
    assert all(metric.step_run_id is None for metric in metrics)

    replay = await evaluate_caption_alignment(
        session,
        run_id=run.id,
        storage_root=tmp_path,
        parameters=_parameters(),
    )

    assert replay.replayed is True
    assert replay.report_artifact_id == result.report_artifact_id
    assert replay.parameters_digest == result.parameters_digest
    assert len(await _alignment_metrics(session, run_id=run.id)) == len(metrics)


async def test_diagnostic_opens_one_local_alert_when_captions_span_silence(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    run = await _run_with_captions(
        session,
        tmp_path,
        idempotency_key="alignment-warning",
        duration_seconds=8.0,
        bursts=((0.0, 1.5), (6.5, 8.0)),
    )
    assert run.id is not None

    result = await evaluate_caption_alignment(
        session,
        run_id=run.id,
        storage_root=tmp_path,
        parameters=_parameters(),
    )

    assert result.measurement.status == STATUS_WARNING
    assert REASON_CAPTIONS_OVER_SILENCE in result.measurement.reasons
    assert result.alert_id is not None
    assert run.status == RunStatus.SUCCEEDED.value
    alerts = (await session.execute(select(Alert))).scalars().all()
    assert len(alerts) == 1
    assert alerts[0].occurrence_count == 1

    await evaluate_caption_alignment(
        session,
        run_id=run.id,
        storage_root=tmp_path,
        parameters=_parameters(),
    )

    alerts = (await session.execute(select(Alert))).scalars().all()
    assert len(alerts) == 1
    assert alerts[0].occurrence_count == 1


async def test_report_keeps_private_editorial_context_out(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    run = await _run_with_captions(
        session,
        tmp_path,
        idempotency_key="alignment-redaction",
        duration_seconds=5.0,
        bursts=((0.0, 5.0),),
    )
    assert run.id is not None

    result = await evaluate_caption_alignment(
        session,
        run_id=run.id,
        storage_root=tmp_path,
        parameters=_parameters(),
    )

    report = await session.get(Artifact, result.report_artifact_id)
    assert report is not None
    payload = json.loads((tmp_path / report.relative_path).read_text(encoding="utf-8"))
    encoded = json.dumps(payload, ensure_ascii=False)
    assert "Ciência acessível" not in encoded
    assert "Sono e memória" not in encoded
    assert _NARRATION not in encoded
    assert payload["published"] is False
    assert payload["external_service_consulted"] is False
    assert payload["caption_sha256"] and payload["audio_sha256"]


async def test_diagnostic_fails_closed_when_the_audio_was_tampered(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    run = await _run_with_captions(
        session,
        tmp_path,
        idempotency_key="alignment-tampered",
        duration_seconds=5.0,
        bursts=((0.0, 5.0),),
    )
    assert run.id is not None
    audio = await session.scalar(
        select(Artifact).where(
            Artifact.run_id == run.id,
            Artifact.artifact_type == "narration_audio",
        )
    )
    assert audio is not None
    _write_wav(
        tmp_path / audio.relative_path,
        duration_seconds=5.0,
        bursts=((0.0, 2.0),),
    )

    with pytest.raises(CaptionAlignmentError) as error:
        await evaluate_caption_alignment(
            session,
            run_id=run.id,
            storage_root=tmp_path,
            parameters=_parameters(),
        )

    assert error.value.code == "CAPTION_ALIGNMENT_EVIDENCE_UNVERIFIED"
    assert await _alignment_metrics(session, run_id=run.id) == []
    assert (
        await session.scalar(
            select(Artifact).where(
                Artifact.run_id == run.id,
                Artifact.artifact_type == "caption_alignment_report",
            )
        )
    ) is None


async def test_diagnostic_requires_this_runs_caption_evidence(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    run = await _run_with_captions(
        session,
        tmp_path,
        idempotency_key="alignment-missing",
        duration_seconds=5.0,
        bursts=((0.0, 5.0),),
    )
    assert run.id is not None
    caption = await session.scalar(
        select(Artifact).where(
            Artifact.run_id == run.id,
            Artifact.artifact_type == "caption_ass",
        )
    )
    assert caption is not None
    await session.delete(caption)
    await session.flush()

    with pytest.raises(CaptionAlignmentError) as error:
        await evaluate_caption_alignment(
            session,
            run_id=run.id,
            storage_root=tmp_path,
            parameters=_parameters(),
        )

    assert error.value.code == "CAPTION_ALIGNMENT_CAPTION_ARTIFACT_MISSING"


async def test_report_projection_is_absent_verified_and_allowlisted(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    run = await _run_with_captions(
        session,
        tmp_path,
        idempotency_key="alignment-projection",
        duration_seconds=5.0,
        bursts=((0.0, 5.0),),
    )
    assert run.id is not None

    assert (
        await load_caption_alignment_report(
            session,
            run_id=run.id,
            storage_root=tmp_path,
        )
    ) is None

    result = await evaluate_caption_alignment(
        session,
        run_id=run.id,
        storage_root=tmp_path,
        parameters=_parameters(),
    )
    view = await load_caption_alignment_report(
        session,
        run_id=run.id,
        storage_root=tmp_path,
    )

    assert view is not None
    assert view.run_id == run.id
    assert view.status == STATUS_PASS
    assert view.reasons == ()
    assert view.report_artifact_id == result.report_artifact_id
    assert view.report_sha256 == result.report_sha256
    assert view.parameters_digest == result.parameters_digest
    assert view.speech_coverage_ratio == result.measurement.speech_coverage_ratio
    assert view.speech_segment_count == result.measurement.speech_segment_count
    rendered = repr(view)
    assert "Ciência acessível" not in rendered
    assert _NARRATION not in rendered


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ({"schema_version": 2}, "CAPTION_ALIGNMENT_REPORT_UNVERIFIED"),
        ({"published": True}, "CAPTION_ALIGNMENT_REPORT_UNVERIFIED"),
        ({"run_id": 9_999}, "CAPTION_ALIGNMENT_REPORT_UNVERIFIED"),
        ({"parameters_digest": "not-a-digest"}, "CAPTION_ALIGNMENT_REPORT_UNVERIFIED"),
    ],
)
async def test_report_projection_fails_closed_on_tampered_content(
    session: AsyncSession,
    tmp_path: Path,
    mutation: dict[str, object],
    expected: str,
) -> None:
    run = await _run_with_captions(
        session,
        tmp_path,
        idempotency_key="alignment-tampered-report",
        duration_seconds=5.0,
        bursts=((0.0, 5.0),),
    )
    assert run.id is not None
    result = await evaluate_caption_alignment(
        session,
        run_id=run.id,
        storage_root=tmp_path,
        parameters=_parameters(),
    )
    report = await session.get(Artifact, result.report_artifact_id)
    assert report is not None
    report_path = tmp_path / report.relative_path
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    payload.update(mutation)
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    report_path.write_bytes(encoded)
    # Keep the durable metadata consistent so the failure comes from the content
    # contract, not only from the checksum.
    report.sha256 = hashlib.sha256(encoded).hexdigest()
    report.size_bytes = len(encoded)
    await session.flush()

    with pytest.raises(CaptionAlignmentError) as error:
        await load_caption_alignment_report(
            session,
            run_id=run.id,
            storage_root=tmp_path,
        )

    assert error.value.code == expected


async def test_report_projection_rejects_a_rewritten_file(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    run = await _run_with_captions(
        session,
        tmp_path,
        idempotency_key="alignment-rewritten-report",
        duration_seconds=5.0,
        bursts=((0.0, 5.0),),
    )
    assert run.id is not None
    result = await evaluate_caption_alignment(
        session,
        run_id=run.id,
        storage_root=tmp_path,
        parameters=_parameters(),
    )
    report = await session.get(Artifact, result.report_artifact_id)
    assert report is not None
    (tmp_path / report.relative_path).write_text("{}", encoding="utf-8")

    with pytest.raises(CaptionAlignmentError) as error:
        await load_caption_alignment_report(
            session,
            run_id=run.id,
            storage_root=tmp_path,
        )

    assert error.value.code == "CAPTION_ALIGNMENT_EVIDENCE_UNVERIFIED"


async def test_report_projection_rechecks_its_audio_and_caption_sources(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    run = await _run_with_captions(
        session,
        tmp_path,
        idempotency_key="alignment-source-rewritten",
        duration_seconds=5.0,
        bursts=((0.0, 5.0),),
    )
    assert run.id is not None
    await evaluate_caption_alignment(
        session,
        run_id=run.id,
        storage_root=tmp_path,
        parameters=_parameters(),
    )
    audio = await session.scalar(
        select(Artifact).where(
            Artifact.run_id == run.id,
            Artifact.artifact_type == "narration_audio",
        )
    )
    assert audio is not None
    (tmp_path / audio.relative_path).write_bytes(b"rewritten")

    with pytest.raises(CaptionAlignmentError) as error:
        await load_caption_alignment_report(
            session,
            run_id=run.id,
            storage_root=tmp_path,
        )

    assert error.value.code == "CAPTION_ALIGNMENT_EVIDENCE_UNVERIFIED"


async def test_diagnostic_rejects_unknown_and_foreign_runs(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    automation = Automation(
        slug="platform-smoke",
        name="Smoke",
        owner="local-operator",
    )
    session.add(automation)
    await session.flush()
    other = Run(
        automation_id=automation.id,
        idempotency_key="smoke-1",
        input_payload={},
        status=RunStatus.SUCCEEDED.value,
    )
    session.add(other)
    await session.flush()
    assert other.id is not None

    with pytest.raises(CaptionAlignmentError) as missing:
        await evaluate_caption_alignment(
            session,
            run_id=other.id + 1_000,
            storage_root=tmp_path,
            parameters=_parameters(),
        )
    assert missing.value.code == "CAPTION_ALIGNMENT_RUN_NOT_FOUND"

    with pytest.raises(CaptionAlignmentError) as foreign:
        await evaluate_caption_alignment(
            session,
            run_id=other.id,
            storage_root=tmp_path,
            parameters=_parameters(),
        )
    assert foreign.value.code == "CAPTION_ALIGNMENT_RUN_NOT_ELIGIBLE"
