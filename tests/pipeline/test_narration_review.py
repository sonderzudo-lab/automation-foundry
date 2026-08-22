"""Content Engine A2 human narration review gate."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.database import Base
from src.pipeline import a1_executor
from src.pipeline.a1_executor import (
    execute_content_script_run,
    finalize_content_script_approval,
)
from src.pipeline.narration_review import (
    NARRATION_REVIEW_APPROVAL_ACTION,
    NarrationReviewGateError,
    load_narration_review,
)
from src.pipeline.tts import TTS_STEP_NAME
from src.platform.approval_service import decide_approval
from src.platform.models import Approval, ApprovalStatus, Artifact, Run, RunStatus, StepRun
from tests.support.local_media_rehearsal import (
    REHEARSAL_LICENSE_ID,
    REHEARSAL_TTS_BACKEND,
    REHEARSAL_VOICE_ID,
    expected_visual_count,
    rehearsal_input_payload,
    rehearsal_script,
    rehearsal_settings,
    write_synthetic_narration_wav,
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


async def _open_narration_gate(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    idempotency_key: str,
) -> tuple[Run, Approval, Approval, Path]:
    storage_root = tmp_path / "storage"
    import_root = tmp_path / "imports" / "content-visuals"
    write_synthetic_visual_import(
        import_root,
        count=expected_visual_count(),
        width=1080,
        height=1920,
    )
    configured = rehearsal_settings(
        storage_root,
        import_root=import_root,
        narration_review_enabled=True,
    )
    configured.content_caption_backend = "disabled"
    configured.content_assembly_backend = "disabled"
    monkeypatch.setattr(a1_executor, "settings", configured)

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
        reason="roteiro integral revisado",
    )
    gated = await execute_content_script_run(
        session,
        idempotency_key=idempotency_key,
        input_payload=rehearsal_input_payload(),
        script_generator=lambda *_args: pytest.fail("A1 must replay"),
        tts_synthesizer=write_synthetic_narration_wav,
        visual_adapter=lambda *_args: pytest.fail("A3 must wait for A2 approval"),
        storage_root=storage_root,
    )
    run = await session.get(Run, gated.run_id)
    narration_approval = await session.scalar(
        select(Approval).where(
            Approval.run_id == gated.run_id,
            Approval.action == NARRATION_REVIEW_APPROVAL_ACTION,
        )
    )
    assert run is not None and narration_approval is not None
    return run, script_approval, narration_approval, storage_root


async def test_a2_review_pauses_before_visuals_with_exact_verified_requirements(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, _, approval, storage_root = await _open_narration_gate(
        session,
        tmp_path,
        monkeypatch,
        idempotency_key="content-a2-review",
    )
    review = await load_narration_review(
        session,
        approval=approval,
        storage_root=storage_root,
    )
    steps = list(
        (
            await session.scalars(
                select(StepRun).where(StepRun.run_id == run.id).order_by(StepRun.ordinal)
            )
        ).all()
    )
    visual_artifacts = list(
        (
            await session.scalars(
                select(Artifact).where(
                    Artifact.run_id == run.id,
                    Artifact.artifact_type == "visual_image",
                )
            )
        ).all()
    )

    assert run.status == RunStatus.AWAITING_APPROVAL.value
    assert approval.status == ApprovalStatus.PENDING.value
    assert [step.name for step in steps] == ["generate-script-a1", TTS_STEP_NAME]
    assert visual_artifacts == []
    assert review.voice_id == REHEARSAL_VOICE_ID
    assert review.backend == REHEARSAL_TTS_BACKEND
    assert review.license_id == REHEARSAL_LICENSE_ID
    assert review.required_visual_count == expected_visual_count()
    assert (review.visual_width, review.visual_height) == (1080, 1920)
    assert review.audio_sha256 == approval.review_payload["audio_sha256"]
    assert review.audio_path.is_file()


async def test_a2_approval_resumes_idempotently_and_completes_a3(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, _, approval, storage_root = await _open_narration_gate(
        session,
        tmp_path,
        monkeypatch,
        idempotency_key="content-a2-resume",
    )
    await decide_approval(
        session,
        approval=approval,
        decision=ApprovalStatus.APPROVED,
        actor="local-owner",
        reason="áudio completo aprovado",
    )

    assert await finalize_content_script_approval(
        session,
        approval=approval,
        storage_root=storage_root,
    ) is True

    resumed = await execute_content_script_run(
        session,
        idempotency_key="content-a2-resume",
        input_payload=rehearsal_input_payload(),
        script_generator=lambda *_args: pytest.fail("A1 must replay"),
        tts_synthesizer=lambda *_args: pytest.fail("A2 must replay"),
        storage_root=storage_root,
    )
    approvals = list(
        (
            await session.scalars(
                select(Approval).where(
                    Approval.run_id == run.id,
                    Approval.action == NARRATION_REVIEW_APPROVAL_ACTION,
                )
            )
        ).all()
    )
    visual_artifacts = list(
        (
            await session.scalars(
                select(Artifact).where(
                    Artifact.run_id == run.id,
                    Artifact.artifact_type == "visual_image",
                )
            )
        ).all()
    )

    assert resumed.run_status == RunStatus.SUCCEEDED.value
    assert run.status == RunStatus.SUCCEEDED.value
    assert len(approvals) == 1
    assert len(visual_artifacts) == expected_visual_count()


async def test_a2_review_rejects_tampered_audio_and_honors_rejection(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, _, approval, storage_root = await _open_narration_gate(
        session,
        tmp_path,
        monkeypatch,
        idempotency_key="content-a2-tamper",
    )
    review = await load_narration_review(
        session,
        approval=approval,
        storage_root=storage_root,
    )
    review.audio_path.write_bytes(review.audio_path.read_bytes() + b"changed")

    with pytest.raises(NarrationReviewGateError, match="size|checksum"):
        await load_narration_review(
            session,
            approval=approval,
            storage_root=storage_root,
        )

    await decide_approval(
        session,
        approval=approval,
        decision=ApprovalStatus.REJECTED,
        actor="local-owner",
        reason="evidência de áudio alterada",
    )
    assert run.status == RunStatus.CANCELLED.value
    assert approval.status == ApprovalStatus.REJECTED.value
