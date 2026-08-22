"""Content Engine A7 human review gate for the finished local video."""

from __future__ import annotations

import hashlib
import json
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
from src.pipeline.a1_executor import (
    execute_content_script_run,
    finalize_content_script_approval,
)
from src.pipeline.caption_alignment_gate import CAPTION_ALIGNMENT_GATE_STEP_NAME
from src.pipeline.final_review import (
    FINAL_REVIEW_APPROVAL_ACTION,
    FinalReviewGateError,
    finalize_final_video_approval,
    load_final_video_review,
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


async def _run_gated_pipeline(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    idempotency_key: str,
    caption_alignment_gate_enabled: bool = False,
    thumbnail_review_enabled: bool = False,
) -> tuple[Run, Approval, Path]:
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
            caption_alignment_gate_enabled=caption_alignment_gate_enabled,
            final_review_enabled=True,
            thumbnail_review_enabled=thumbnail_review_enabled,
        ),
    )
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
        script_generator=lambda *_args: pytest.fail("A1 must replay after approval"),
        tts_synthesizer=write_synthetic_narration_wav,
        assembly_adapter=_assembly_adapter,
        storage_root=storage_root,
    )
    run = await session.get(Run, gated.run_id)
    final_approval = await session.scalar(
        select(Approval).where(
            Approval.run_id == gated.run_id,
            Approval.action == FINAL_REVIEW_APPROVAL_ACTION,
        )
    )
    assert run is not None and final_approval is not None
    return run, final_approval, storage_root


async def test_a7_opens_a_pending_gate_instead_of_completing_the_run(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, approval, storage_root = await _run_gated_pipeline(
        session,
        tmp_path,
        monkeypatch,
        idempotency_key="content-a7-gate",
    )
    steps = list(
        (
            await session.scalars(
                select(StepRun).where(StepRun.run_id == run.id).order_by(StepRun.ordinal)
            )
        ).all()
    )

    assert run.status == RunStatus.AWAITING_APPROVAL.value
    assert run.output_payload is None
    assert approval.status == ApprovalStatus.PENDING.value
    assert len(steps) == 6
    assert steps[-1].name == "check-originality-a6"
    assert steps[-1].status == "succeeded"

    review = approval.review_payload
    assert review is not None
    assert set(review) == {
        "video",
        "format",
        "codecs",
        "video_sha256",
        "originality",
        "report_sha256",
        "publication",
    }
    video = await session.scalar(
        select(Artifact).where(
            Artifact.run_id == run.id,
            Artifact.artifact_type == "final_video",
        )
    )
    assert video is not None
    assert review["video_sha256"] == video.sha256
    assert "1080x1920" in review["video"]
    assert "Nenhum upload" in review["publication"]
    assert not (storage_root / "uploads").exists()


async def test_alignment_gate_passes_and_freezes_its_report_in_a7(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, approval, storage_root = await _run_gated_pipeline(
        session,
        tmp_path,
        monkeypatch,
        idempotency_key="content-a4-alignment-gate-pass",
        caption_alignment_gate_enabled=True,
    )
    steps = list(
        (
            await session.scalars(
                select(StepRun).where(StepRun.run_id == run.id).order_by(StepRun.ordinal)
            )
        ).all()
    )
    review = await load_final_video_review(
        session,
        approval=approval,
        storage_root=storage_root,
    )

    assert run.status == RunStatus.AWAITING_APPROVAL.value
    assert len(steps) == 7
    assert steps[-1].name == CAPTION_ALIGNMENT_GATE_STEP_NAME
    assert steps[-1].status == RunStatus.SUCCEEDED.value
    assert review.alignment_report_artifact_id is not None
    assert review.alignment_report_sha256 is not None
    assert review.alignment_parameters_digest is not None
    assert review.alignment_speech_coverage_ratio == "1.0000000000"
    assert approval.review_payload is not None
    assert approval.review_payload["alignment"].startswith("pass")

    await decide_approval(
        session,
        approval=approval,
        decision=ApprovalStatus.APPROVED,
        actor="local-owner",
        reason="vídeo e sincronismo revisados",
    )
    await finalize_content_script_approval(
        session,
        approval=approval,
        storage_root=storage_root,
    )

    assert run.status == RunStatus.SUCCEEDED.value
    assert run.output_payload is not None
    assert (
        run.output_payload["alignment_report_artifact_id"]
        == review.alignment_report_artifact_id
    )
    assert run.output_payload["published"] is False


async def test_a7_fails_closed_when_the_gated_alignment_report_changed(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, approval, storage_root = await _run_gated_pipeline(
        session,
        tmp_path,
        monkeypatch,
        idempotency_key="content-a4-alignment-gate-tampered",
        caption_alignment_gate_enabled=True,
    )
    gate_step = await session.scalar(
        select(StepRun).where(
            StepRun.run_id == run.id,
            StepRun.name == CAPTION_ALIGNMENT_GATE_STEP_NAME,
        )
    )
    assert gate_step is not None and gate_step.output_payload is not None
    report = await session.get(
        Artifact,
        gate_step.output_payload["report_artifact_id"],
    )
    assert report is not None
    (storage_root / report.relative_path).write_text("{}\n", encoding="utf-8")
    await decide_approval(
        session,
        approval=approval,
        decision=ApprovalStatus.APPROVED,
        actor="local-owner",
        reason="vídeo revisado",
    )

    with pytest.raises(FinalReviewGateError, match="alignment gate evidence"):
        await finalize_content_script_approval(
            session,
            approval=approval,
            storage_root=storage_root,
        )

    assert run.status == RunStatus.RUNNING.value
    assert run.output_payload is None


async def test_a7_approval_concludes_the_run_locally_and_replays_safely(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, approval, storage_root = await _run_gated_pipeline(
        session,
        tmp_path,
        monkeypatch,
        idempotency_key="content-a7-approved",
    )
    await decide_approval(
        session,
        approval=approval,
        decision=ApprovalStatus.APPROVED,
        actor="local-owner",
        reason="vídeo final revisado; nada será publicado",
    )
    continuation = await finalize_content_script_approval(
        session,
        approval=approval,
        storage_root=storage_root,
    )

    assert continuation is False
    assert run.status == RunStatus.SUCCEEDED.value
    output = run.output_payload
    assert output is not None
    assert output["published"] is False
    assert output["final_review_approval_id"] == approval.id
    assert isinstance(output["final_video_sha256"], str)
    assert isinstance(output["originality_report_sha256"], str)
    assert len(output["metric_point_ids"]) > 0

    replayed = await finalize_final_video_approval(
        session,
        approval=approval,
        storage_root=storage_root,
    )
    assert replayed is False
    assert run.output_payload == output


async def test_a7_rejection_cancels_the_run_without_publication(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, approval, _ = await _run_gated_pipeline(
        session,
        tmp_path,
        monkeypatch,
        idempotency_key="content-a7-rejected",
    )
    await decide_approval(
        session,
        approval=approval,
        decision=ApprovalStatus.REJECTED,
        actor="local-owner",
        reason="qualidade abaixo do aceitável",
    )

    assert run.status == RunStatus.CANCELLED.value
    assert run.output_payload is None
    assert approval.status == ApprovalStatus.REJECTED.value


async def test_a7_refuses_to_finalize_when_the_reviewed_video_changed(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, approval, storage_root = await _run_gated_pipeline(
        session,
        tmp_path,
        monkeypatch,
        idempotency_key="content-a7-tampered",
    )
    video = await session.scalar(
        select(Artifact).where(
            Artifact.run_id == run.id,
            Artifact.artifact_type == "final_video",
        )
    )
    assert video is not None
    path = storage_root / video.relative_path
    encoded = bytearray(path.read_bytes())
    encoded[-1] ^= 0xFF
    path.write_bytes(bytes(encoded))
    await decide_approval(
        session,
        approval=approval,
        decision=ApprovalStatus.APPROVED,
        actor="local-owner",
        reason="vídeo final revisado",
    )

    with pytest.raises(FinalReviewGateError, match="checksum"):
        await finalize_final_video_approval(
            session,
            approval=approval,
            storage_root=storage_root,
        )
    assert run.status == RunStatus.RUNNING.value
    assert run.output_payload is None


async def test_a7_refuses_a_report_that_blocked_the_content(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, approval, storage_root = await _run_gated_pipeline(
        session,
        tmp_path,
        monkeypatch,
        idempotency_key="content-a7-blocked-report",
    )
    report = await session.scalar(
        select(Artifact).where(
            Artifact.run_id == run.id,
            Artifact.artifact_type == "originality_report",
        )
    )
    assert report is not None
    path = storage_root / report.relative_path
    payload = json.loads(path.read_text("utf-8"))
    payload["blocked"] = True
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    path.write_bytes(encoded)
    report.size_bytes = len(encoded)
    report.sha256 = hashlib.sha256(encoded).hexdigest()
    await session.flush()
    await decide_approval(
        session,
        approval=approval,
        decision=ApprovalStatus.APPROVED,
        actor="local-owner",
        reason="vídeo final revisado",
    )

    with pytest.raises(FinalReviewGateError, match="blocked by A6"):
        await finalize_final_video_approval(
            session,
            approval=approval,
            storage_root=storage_root,
        )
    assert run.status == RunStatus.RUNNING.value


async def test_a7_duplicate_delivery_replays_without_repeating_work(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, approval, storage_root = await _run_gated_pipeline(
        session,
        tmp_path,
        monkeypatch,
        idempotency_key="content-a7-replay",
    )
    replayed = await execute_content_script_run(
        session,
        idempotency_key="content-a7-replay",
        input_payload=rehearsal_input_payload(),
        script_generator=lambda *_args: pytest.fail("gated run must replay"),
        storage_root=storage_root,
    )
    steps = list(
        (await session.scalars(select(StepRun).where(StepRun.run_id == run.id))).all()
    )
    approvals = list(
        (await session.scalars(select(Approval).where(Approval.run_id == run.id))).all()
    )

    assert replayed.replayed is True
    assert replayed.run_status == RunStatus.AWAITING_APPROVAL.value
    assert len(steps) == 6
    assert len(approvals) == 2
    assert approval.status == ApprovalStatus.PENDING.value
