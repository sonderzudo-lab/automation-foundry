"""A1 to A6 rehearsal against the real A3 import and A4 timing backends.

This test never touches an external binary, model, download or API. A2 uses the
declared synthetic tone fixture and A5 uses the fake local MP4 adapter, so what
is actually proven here is the wiring of the real `local_assets_quality_test`
import and the real `approved_text_timing_quality_test` alignment inside the
shared run contract. Real narration, a reviewed visual package and a real
FFmpeg render remain covered only by the opt-in integration rehearsal.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
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
from src.pipeline import a1_executor
from src.pipeline.a1_executor import execute_content_script_run
from src.pipeline.caption_alignment import (
    STATUS_PASS,
    CaptionAlignmentParameters,
    evaluate_caption_alignment,
)
from src.platform.approval_service import decide_approval
from src.platform.models import (
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
    SYNTHETIC_NARRATION_SECONDS,
    expected_visual_count,
    rehearsal_input_payload,
    rehearsal_script,
    rehearsal_settings,
    write_synthetic_narration_wav,
    write_synthetic_visual_import,
)

_ASS_DIALOGUE = re.compile(
    r"^Dialogue: 0,(\d):(\d{2}):(\d{2}\.\d{2}),(\d):(\d{2}):(\d{2}\.\d{2}),",
    re.MULTILINE,
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


def _ass_seconds(hours: str, minutes: str, seconds: str) -> Decimal:
    return (
        (Decimal(hours) * Decimal("3600"))
        + (Decimal(minutes) * Decimal("60"))
        + Decimal(seconds)
    )


async def test_local_media_rehearsal_uses_real_a3_import_and_a4_timing(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_root = tmp_path / "storage"
    import_root = tmp_path / "imports" / "content-visuals"
    visual_count = expected_visual_count()
    write_synthetic_visual_import(
        import_root,
        count=visual_count,
        width=1080,
        height=1920,
    )
    imported_before = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(import_root.glob("*.png"))
    }
    monkeypatch.setattr(
        a1_executor,
        "settings",
        rehearsal_settings(storage_root, import_root=import_root),
    )

    first = await execute_content_script_run(
        session,
        idempotency_key="content-local-media-rehearsal",
        input_payload=rehearsal_input_payload(),
        script_generator=lambda *_args: rehearsal_script(),
        storage_root=storage_root,
    )
    approval = await session.get(Approval, first.approval_id)
    assert approval is not None
    await decide_approval(
        session,
        approval=approval,
        decision=ApprovalStatus.APPROVED,
        actor="local-owner",
        reason="roteiro integral revisado antes do ensaio local",
    )
    rehearsed = await execute_content_script_run(
        session,
        idempotency_key="content-local-media-rehearsal",
        input_payload=rehearsal_input_payload(),
        script_generator=lambda *_args: pytest.fail("A1 must replay after approval"),
        tts_synthesizer=write_synthetic_narration_wav,
        assembly_adapter=_assembly_adapter,
        storage_root=storage_root,
    )

    run = await session.get(Run, rehearsed.run_id)
    steps = list(
        (
            await session.scalars(
                select(StepRun)
                .where(StepRun.run_id == rehearsed.run_id)
                .order_by(StepRun.ordinal, StepRun.attempt)
            )
        ).all()
    )
    artifacts = list(
        (
            await session.scalars(
                select(Artifact)
                .where(Artifact.run_id == rehearsed.run_id)
                .order_by(Artifact.id)
            )
        ).all()
    )

    assert run is not None and run.status == RunStatus.SUCCEEDED.value
    assert [(step.name, step.queue) for step in steps] == [
        ("generate-script-a1", "gpu"),
        ("synthesize-tts-a2", "gpu"),
        ("prepare-visuals-a3", "io"),
        ("generate-captions-a4", "cpu"),
        ("assemble-video-a5", "cpu"),
        ("check-originality-a6", "cpu"),
    ]
    assert [artifact.artifact_type for artifact in artifacts] == [
        "script_bundle",
        "narration_audio",
        "visual_manifest",
        *(["visual_image"] * visual_count),
        "caption_ass",
        "final_video",
        "originality_report",
    ]
    assert run.output_payload is not None
    assert isinstance(run.output_payload["final_video_artifact_id"], int)
    assert isinstance(run.output_payload["originality_report_artifact_id"], int)

    manifest_artifact = next(
        artifact for artifact in artifacts if artifact.artifact_type == "visual_manifest"
    )
    manifest = json.loads((storage_root / manifest_artifact.relative_path).read_text("utf-8"))
    assert len(manifest["assets"]) == visual_count
    for asset in manifest["assets"]:
        assert asset["license_id"] == REHEARSAL_LICENSE_ID
        assert asset["commercial_use"] is True
        assert asset["source_type"] == "generated"
    published_hashes = {
        artifact.sha256
        for artifact in artifacts
        if artifact.artifact_type == "visual_image"
    }
    assert published_hashes == set(imported_before.values())

    caption_artifact = next(
        artifact for artifact in artifacts if artifact.artifact_type == "caption_ass"
    )
    caption_text = (storage_root / caption_artifact.relative_path).read_text("utf-8")
    dialogues = _ASS_DIALOGUE.findall(caption_text)
    narration_words = rehearsal_script().narration.split()
    assert len(dialogues) == math.ceil(len(narration_words) / 6)
    assert caption_text.count("{\\kf") == len(narration_words)
    assert all(word.strip(".,") in caption_text for word in narration_words)
    first_start = _ass_seconds(*dialogues[0][:3])
    last_end = _ass_seconds(*dialogues[-1][3:])
    assert first_start == Decimal("0")
    assert abs(last_end - SYNTHETIC_NARRATION_SECONDS) <= Decimal("0.02")

    imported_after = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(import_root.glob("*.png"))
    }
    assert imported_after == imported_before

    # The read-only A4 diagnostic runs over the evidence this chain produced. The
    # synthetic fixture is one continuous tone, so it is the easy case: it proves
    # the wiring and the fail-closed verification, not synchronism against a real
    # voice with pauses.
    alignment = await evaluate_caption_alignment(
        session,
        run_id=rehearsed.run_id,
        storage_root=storage_root,
        parameters=CaptionAlignmentParameters(),
    )

    assert alignment.measurement.status == STATUS_PASS
    assert alignment.measurement.reasons == ()
    assert alignment.measurement.speech_segment_count == 1
    assert alignment.measurement.speech_coverage_ratio >= Decimal("0.99")
    assert alignment.alert_id is None
    assert run.status == RunStatus.SUCCEEDED.value


async def test_local_media_rehearsal_fails_closed_when_manifest_count_diverges(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_root = tmp_path / "storage"
    import_root = tmp_path / "imports" / "content-visuals"
    write_synthetic_visual_import(
        import_root,
        count=expected_visual_count() + 1,
        width=1080,
        height=1920,
    )
    monkeypatch.setattr(
        a1_executor,
        "settings",
        rehearsal_settings(storage_root, import_root=import_root),
    )

    first = await execute_content_script_run(
        session,
        idempotency_key="content-local-media-rehearsal-mismatch",
        input_payload=rehearsal_input_payload(),
        script_generator=lambda *_args: rehearsal_script(),
        storage_root=storage_root,
    )
    approval = await session.get(Approval, first.approval_id)
    assert approval is not None
    await decide_approval(
        session,
        approval=approval,
        decision=ApprovalStatus.APPROVED,
        actor="local-owner",
        reason="roteiro integral revisado antes do ensaio local",
    )
    blocked = await execute_content_script_run(
        session,
        idempotency_key="content-local-media-rehearsal-mismatch",
        input_payload=rehearsal_input_payload(),
        script_generator=lambda *_args: pytest.fail("A1 must replay after approval"),
        tts_synthesizer=write_synthetic_narration_wav,
        assembly_adapter=_assembly_adapter,
        storage_root=storage_root,
    )

    run = await session.get(Run, blocked.run_id)
    steps = list(
        (
            await session.scalars(
                select(StepRun)
                .where(StepRun.run_id == blocked.run_id)
                .order_by(StepRun.ordinal, StepRun.attempt)
            )
        ).all()
    )
    artifacts = list(
        (
            await session.scalars(
                select(Artifact).where(Artifact.run_id == blocked.run_id)
            )
        ).all()
    )

    assert run is not None and run.status == RunStatus.FAILED.value
    assert run.error is not None
    assert run.error["code"] == "CONTENT_VISUALS_COUNT_MISMATCH"
    assert [step.name for step in steps] == [
        "generate-script-a1",
        "synthesize-tts-a2",
        "prepare-visuals-a3",
    ]
    assert {artifact.artifact_type for artifact in artifacts} == {
        "script_bundle",
        "narration_audio",
    }
