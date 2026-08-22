"""Content Engine A8 local thumbnail-selection gate."""

from __future__ import annotations

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
from src.pipeline.a1_executor import (
    execute_content_script_run,
    finalize_content_script_approval,
)
from src.pipeline.thumbnail_review import (
    THUMBNAIL_REVIEW_APPROVAL_ACTION,
    ThumbnailReviewGateError,
    finalize_thumbnail_approval,
    load_thumbnail_review,
    thumbnail_decision_payload,
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
from tests.pipeline.test_final_review import _run_gated_pipeline
from tests.support.local_media_rehearsal import rehearsal_input_payload


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as database_session:
        yield database_session
    await engine.dispose()


async def _run_to_a8(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    idempotency_key: str,
    caption_alignment_gate_enabled: bool = False,
) -> tuple[Run, Approval, Path]:
    run, final_approval, storage_root = await _run_gated_pipeline(
        session,
        tmp_path,
        monkeypatch,
        idempotency_key=idempotency_key,
        caption_alignment_gate_enabled=caption_alignment_gate_enabled,
        thumbnail_review_enabled=True,
    )
    await decide_approval(
        session,
        approval=final_approval,
        decision=ApprovalStatus.APPROVED,
        actor="local-owner",
        reason="vídeo final revisado localmente",
    )
    continuation = await finalize_content_script_approval(
        session,
        approval=final_approval,
        storage_root=storage_root,
    )
    thumbnail_approval = await session.scalar(
        select(Approval).where(
            Approval.run_id == run.id,
            Approval.action == THUMBNAIL_REVIEW_APPROVAL_ACTION,
        )
    )
    assert continuation is False
    assert thumbnail_approval is not None
    return run, thumbnail_approval, storage_root


async def test_a8_freezes_only_same_run_a3_pngs_after_a7_approval(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, approval, storage_root = await _run_to_a8(
        session,
        tmp_path,
        monkeypatch,
        idempotency_key="content-a8-gate",
    )
    review = await load_thumbnail_review(
        session,
        approval=approval,
        storage_root=storage_root,
    )
    visual_ids = tuple(
        (
            await session.scalars(
                select(Artifact.id)
                .where(
                    Artifact.run_id == run.id,
                    Artifact.artifact_type == "visual_image",
                )
                .order_by(Artifact.id)
            )
        ).all()
    )

    assert run.status == RunStatus.AWAITING_APPROVAL.value
    assert run.output_payload is None
    assert approval.status == ApprovalStatus.PENDING.value
    assert tuple(candidate.artifact_id for candidate in review.candidates) == visual_ids
    assert len(review.candidate_set_sha256) == 64
    assert all(candidate.image_path.is_file() for candidate in review.candidates)
    assert approval.review_payload is not None
    assert "published=false" in approval.review_payload["publication"]
    assert not (storage_root / "uploads").exists()

    final_approval = await session.scalar(
        select(Approval).where(
            Approval.run_id == run.id,
            Approval.action == "review_content_final_video_a7",
        )
    )
    assert final_approval is not None
    assert (
        await finalize_content_script_approval(
            session,
            approval=final_approval,
            storage_root=storage_root,
        )
        is False
    )
    a8_approvals = tuple(
        (
            await session.scalars(
                select(Approval).where(
                    Approval.run_id == run.id,
                    Approval.action == THUMBNAIL_REVIEW_APPROVAL_ACTION,
                )
            )
        ).all()
    )
    assert len(a8_approvals) == 1


async def test_a8_selection_concludes_locally_and_replays_without_duplicate_work(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, approval, storage_root = await _run_to_a8(
        session,
        tmp_path,
        monkeypatch,
        idempotency_key="content-a8-approved",
    )
    review = await load_thumbnail_review(
        session,
        approval=approval,
        storage_root=storage_root,
    )
    selected = review.candidates[0]
    decision = thumbnail_decision_payload(
        review,
        thumbnail_artifact_id=selected.artifact_id,
    )
    await decide_approval(
        session,
        approval=approval,
        decision=ApprovalStatus.APPROVED,
        actor="local-owner",
        reason="thumbnail escolhida entre os PNGs revisados",
        decision_payload=decision,
    )
    continuation = await finalize_content_script_approval(
        session,
        approval=approval,
        storage_root=storage_root,
    )
    output = dict(run.output_payload or {})

    assert continuation is False
    assert run.status == RunStatus.SUCCEEDED.value
    assert approval.decision_payload == decision
    assert output["thumbnail_artifact_id"] == selected.artifact_id
    assert output["thumbnail_sha256"] == selected.sha256
    assert output["thumbnail_review_approval_id"] == approval.id
    assert output["final_review_approval_id"] == review.final_review_approval_id
    assert output["published"] is False

    assert (
        await finalize_thumbnail_approval(
            session,
            approval=approval,
            storage_root=storage_root,
        )
        is False
    )
    replay = await execute_content_script_run(
        session,
        idempotency_key="content-a8-approved",
        input_payload=rehearsal_input_payload(),
        script_generator=lambda *_args: pytest.fail("terminal run must replay"),
        storage_root=storage_root,
    )
    steps = tuple(
        (await session.scalars(select(StepRun).where(StepRun.run_id == run.id))).all()
    )
    approvals = tuple(
        (await session.scalars(select(Approval).where(Approval.run_id == run.id))).all()
    )
    assert replay.replayed is True
    assert len(steps) == 6
    assert len(approvals) == 3
    assert run.output_payload == output


async def test_a8_preserves_the_alignment_gate_evidence_in_final_output(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, approval, storage_root = await _run_to_a8(
        session,
        tmp_path,
        monkeypatch,
        idempotency_key="content-a8-with-alignment-gate",
        caption_alignment_gate_enabled=True,
    )
    review = await load_thumbnail_review(
        session,
        approval=approval,
        storage_root=storage_root,
    )
    decision = thumbnail_decision_payload(
        review,
        thumbnail_artifact_id=review.candidates[0].artifact_id,
    )
    await decide_approval(
        session,
        approval=approval,
        decision=ApprovalStatus.APPROVED,
        actor="local-owner",
        reason="thumbnail e sincronismo revisados",
        decision_payload=decision,
    )
    await finalize_content_script_approval(
        session,
        approval=approval,
        storage_root=storage_root,
    )

    assert run.status == RunStatus.SUCCEEDED.value
    assert run.output_payload is not None
    assert isinstance(run.output_payload["alignment_gate_step_run_id"], int)
    assert isinstance(run.output_payload["alignment_report_artifact_id"], int)
    assert isinstance(run.output_payload["alignment_report_sha256"], str)
    assert isinstance(run.output_payload["alignment_parameters_digest"], str)
    assert run.output_payload["published"] is False


async def test_a8_rejection_cancels_the_same_run(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, approval, _ = await _run_to_a8(
        session,
        tmp_path,
        monkeypatch,
        idempotency_key="content-a8-rejected",
    )
    await decide_approval(
        session,
        approval=approval,
        decision=ApprovalStatus.REJECTED,
        actor="local-owner",
        reason="nenhum candidato representa bem o vídeo",
    )

    assert run.status == RunStatus.CANCELLED.value
    assert run.output_payload is None
    assert approval.status == ApprovalStatus.REJECTED.value


async def test_a8_rejects_arbitrary_or_cross_run_artifact_ids(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run, approval, storage_root = await _run_to_a8(
        session,
        tmp_path,
        monkeypatch,
        idempotency_key="content-a8-arbitrary",
    )
    review = await load_thumbnail_review(
        session,
        approval=approval,
        storage_root=storage_root,
    )
    other_run, _other_approval, _other_root = await _run_to_a8(
        session,
        tmp_path / "other",
        monkeypatch,
        idempotency_key="content-a8-other-run",
    )
    cross_run_id = await session.scalar(
        select(Artifact.id).where(
            Artifact.run_id == other_run.id,
            Artifact.artifact_type == "visual_image",
        )
    )
    assert isinstance(cross_run_id, int)

    with pytest.raises(ThumbnailReviewGateError, match="frozen candidate set"):
        thumbnail_decision_payload(
            review,
            thumbnail_artifact_id=cross_run_id,
        )
    assert approval.status == ApprovalStatus.PENDING.value


async def test_a8_tampered_png_or_digest_blocks_review_and_completion(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, approval, storage_root = await _run_to_a8(
        session,
        tmp_path,
        monkeypatch,
        idempotency_key="content-a8-tampered",
    )
    review = await load_thumbnail_review(
        session,
        approval=approval,
        storage_root=storage_root,
    )
    candidate = review.candidates[0]
    encoded = bytearray(candidate.image_path.read_bytes())
    encoded[-1] ^= 0xFF
    candidate.image_path.write_bytes(bytes(encoded))

    with pytest.raises(ThumbnailReviewGateError, match="checksum"):
        await load_thumbnail_review(
            session,
            approval=approval,
            storage_root=storage_root,
        )
    assert run.status == RunStatus.AWAITING_APPROVAL.value
    assert run.output_payload is None

    candidate.image_path.write_bytes(bytes(bytearray(encoded[:-1]) + bytes([encoded[-1] ^ 0xFF])))
    approval.payload_digest = "0" * 64
    await session.flush()
    with pytest.raises(ThumbnailReviewGateError, match="frozen thumbnail candidates"):
        await load_thumbnail_review(
            session,
            approval=approval,
            storage_root=storage_root,
        )
    assert run.status == RunStatus.AWAITING_APPROVAL.value


@pytest.mark.parametrize(
    "decision_payload, expected",
    [
        (None, "missing or invalid"),
        (
            {"thumbnail_artifact_id": 1, "thumbnail_sha256": "0" * 64},
            "frozen candidate set|diverges",
        ),
    ],
)
async def test_a8_missing_or_divergent_decision_payload_never_completes_run(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    decision_payload: dict[str, object] | None,
    expected: str,
) -> None:
    run, approval, storage_root = await _run_to_a8(
        session,
        tmp_path,
        monkeypatch,
        idempotency_key=f"content-a8-bad-decision-{decision_payload is None}",
    )
    await decide_approval(
        session,
        approval=approval,
        decision=ApprovalStatus.APPROVED,
        actor="local-owner",
        reason="decisão estruturalmente incompleta para teste",
        decision_payload=decision_payload,
    )

    with pytest.raises(ThumbnailReviewGateError, match=expected):
        await finalize_thumbnail_approval(
            session,
            approval=approval,
            storage_root=storage_root,
        )
    assert run.status == RunStatus.RUNNING.value
    assert run.output_payload is None
