"""Optional fail-closed A4 alignment gate in the A1 to A7 pipeline."""

from __future__ import annotations

import math
import struct
import wave
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.core.database import Base
from src.pipeline import a1_executor
from src.pipeline.a1_executor import execute_content_script_run
from src.pipeline.caption_alignment import STATUS_WARNING, load_caption_alignment_report
from src.pipeline.caption_alignment_gate import (
    CAPTION_ALIGNMENT_BLOCKED_CODE,
    CAPTION_ALIGNMENT_GATE_STEP_NAME,
)
from src.pipeline.final_review import FINAL_REVIEW_APPROVAL_ACTION
from src.pipeline.tts import TTSAdapterResult
from src.platform.approval_service import decide_approval
from src.platform.models import (
    Alert,
    Approval,
    ApprovalStatus,
    Artifact,
    Run,
    RunStatus,
    StepRun,
)
from tests.pipeline.test_assembly import _assembly_adapter
from tests.support.local_media_rehearsal import (
    REHEARSAL_LICENSE_ID,
    REHEARSAL_TTS_BACKEND,
    SYNTHETIC_NARRATION_SECONDS,
    SYNTHETIC_SAMPLE_RATE_HZ,
    expected_visual_count,
    rehearsal_input_payload,
    rehearsal_script,
    rehearsal_settings,
    write_synthetic_visual_import,
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


def _write_gapped_narration(
    _text: str,
    destination: Path,
    voice_id: str,
    language_code: str,
) -> TTSAdapterResult:
    frame_count = int(SYNTHETIC_NARRATION_SECONDS) * SYNTHETIC_SAMPLE_RATE_HZ
    samples = bytearray()
    for index in range(frame_count):
        seconds = index / SYNTHETIC_SAMPLE_RATE_HZ
        active = seconds < 3.0 or seconds >= 9.0
        value = (
            int(9_000 * math.sin(2.0 * math.pi * 220.0 * seconds))
            if active
            else 0
        )
        samples += struct.pack("<h", value)
    with wave.open(str(destination), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(SYNTHETIC_SAMPLE_RATE_HZ)
        audio.writeframes(bytes(samples))
    return TTSAdapterResult(
        backend=REHEARSAL_TTS_BACKEND,
        model_id="deterministic-gapped-tone-v1",
        voice_id=voice_id,
        language_code=language_code,
        license_id=REHEARSAL_LICENSE_ID,
    )


async def test_warning_blocks_a7_and_preserves_auditable_local_evidence(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_root = tmp_path / "storage"
    import_root = tmp_path / "imports" / "content-visuals"
    write_synthetic_visual_import(
        import_root,
        count=expected_visual_count(),
        width=1080,
        height=1920,
    )
    monkeypatch.setattr(
        a1_executor,
        "settings",
        rehearsal_settings(
            storage_root,
            import_root=import_root,
            caption_alignment_gate_enabled=True,
            final_review_enabled=True,
        ),
    )
    idempotency_key = "content-a4-alignment-gate-warning"
    first = await execute_content_script_run(
        session,
        idempotency_key=idempotency_key,
        input_payload=rehearsal_input_payload(),
        script_generator=lambda *_args: rehearsal_script(),
        storage_root=storage_root,
    )
    script_approval = await session.get(Approval, first.approval_id)
    assert script_approval is not None
    await decide_approval(
        session,
        approval=script_approval,
        decision=ApprovalStatus.APPROVED,
        actor="local-owner",
        reason="roteiro revisado",
    )

    result = await execute_content_script_run(
        session,
        idempotency_key=idempotency_key,
        input_payload=rehearsal_input_payload(),
        script_generator=lambda *_args: pytest.fail("A1 must replay"),
        tts_synthesizer=_write_gapped_narration,
        assembly_adapter=_assembly_adapter,
        storage_root=storage_root,
    )
    run = await session.get(Run, result.run_id)
    gate_step = await session.scalar(
        select(StepRun).where(
            StepRun.run_id == result.run_id,
            StepRun.name == CAPTION_ALIGNMENT_GATE_STEP_NAME,
        )
    )
    final_approval = await session.scalar(
        select(Approval).where(
            Approval.run_id == result.run_id,
            Approval.action == FINAL_REVIEW_APPROVAL_ACTION,
        )
    )
    report = await load_caption_alignment_report(
        session,
        run_id=result.run_id,
        storage_root=storage_root,
    )
    report_artifact_count = len(
        list(
            (
                await session.scalars(
                    select(Artifact).where(
                        Artifact.run_id == result.run_id,
                        Artifact.artifact_type == "caption_alignment_report",
                    )
                )
            ).all()
        )
    )
    alert_count = len(
        list(
            (
                await session.scalars(
                    select(Alert).where(Alert.run_id == result.run_id)
                )
            ).all()
        )
    )

    assert run is not None
    assert run.status == RunStatus.FAILED.value
    assert run.error is not None
    assert run.error["code"] == CAPTION_ALIGNMENT_BLOCKED_CODE
    assert run.error["reasons"] == ["CAPTIONS_OVER_SILENCE"]
    assert gate_step is not None
    assert gate_step.status == RunStatus.FAILED.value
    assert gate_step.error is not None
    assert gate_step.error["code"] == CAPTION_ALIGNMENT_BLOCKED_CODE
    assert final_approval is None
    assert report is not None and report.status == STATUS_WARNING
    assert report_artifact_count == 1
    assert alert_count == 1
    assert run.output_payload is None
    assert not (storage_root / "uploads").exists()
