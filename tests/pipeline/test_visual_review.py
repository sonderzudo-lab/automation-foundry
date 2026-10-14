"""Content Engine A3 human review gate for the images, before captions and assembly."""

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
from src.pipeline import a1_executor
from src.pipeline.a1_executor import (
    ContentScriptRunNotRunnableError,
    execute_content_script_run,
    finalize_content_script_approval,
    load_content_approval_review,
)
from src.pipeline.final_review import FINAL_REVIEW_APPROVAL_ACTION
from src.pipeline.visual_generation import FLUX2_KLEIN_QUALITY_TEST_BACKEND
from src.pipeline.visual_review import (
    VISUAL_REVIEW_APPROVAL_ACTION,
    VisualReview,
    VisualReviewGateError,
    finalize_visual_approval,
    load_visual_review,
)
from src.pipeline.visuals import (
    QUALITY_TEST_USAGE_SCOPE,
    VisualAssetResult,
    VisualRequest,
)
from src.platform.approval_service import decide_approval
from src.platform.models import Approval, ApprovalStatus, Run, RunStatus, StepRun
from tests.pipeline.test_assembly import _assembly_adapter
from tests.pipeline.test_visuals import _write_png
from tests.support.local_media_rehearsal import (
    rehearsal_input_payload,
    rehearsal_script,
    rehearsal_settings,
    write_synthetic_narration_wav,
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


def generated_adapter(
    requests: tuple[VisualRequest, ...],
    destination: Path,
) -> tuple[VisualAssetResult, ...]:
    """Stands in for the generating backend: valid PNGs with no commercial-use claim."""
    results: list[VisualAssetResult] = []
    for request in requests:
        file_name = f"visual-{request.index:03d}.png"
        _write_png(destination / file_name, width=request.width, height=request.height)
        results.append(
            VisualAssetResult(
                file_name=file_name,
                source_type="generated",
                provider="fake-generator",
                model_id="fake-image-model-v1",
                source_id=f"fake:{request.index}",
                source_uri=f"local-model://fake-image-model-v1/{request.index}",
                license_id="TEST-ONLY; scope=quality-test-only",
                commercial_use=False,
                attribution="test fixture",
                generation_prompt=f"scene {request.index}, flat cartoon illustration",
                seed=1000 + request.index,
            )
        )
    return tuple(results)


PAYLOAD: dict[str, object] = {**rehearsal_input_payload(), "visual_style": "cartoon"}


async def continue_run(session: AsyncSession, storage_root: Path, key: str) -> Run:
    """Deliver the durable dispatch again, as the worker does after an approval."""
    result = await execute_content_script_run(
        session,
        idempotency_key=key,
        input_payload=PAYLOAD,
        script_generator=lambda *_args: pytest.fail("A1 must replay after approval"),
        tts_synthesizer=write_synthetic_narration_wav,
        visual_adapter=generated_adapter,
        assembly_adapter=_assembly_adapter,
        storage_root=storage_root,
    )
    run = await session.get(Run, result.run_id)
    assert run is not None
    return run


async def pending_approval(session: AsyncSession, run: Run, action: str) -> Approval:
    approval = await session.scalar(
        select(Approval).where(Approval.run_id == run.id, Approval.action == action)
    )
    assert approval is not None
    return approval


async def run_to_visual_gate(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    idempotency_key: str,
    visual_backend: str = FLUX2_KLEIN_QUALITY_TEST_BACKEND,
    visual_review_enabled: bool = True,
) -> tuple[Run, Path]:
    """Run A1→A3 with generated images and stop wherever the settings stop it."""
    storage_root = tmp_path / "storage"
    fixture_settings = rehearsal_settings(
        storage_root,
        import_root=tmp_path / "unused-imports",
        final_review_enabled=True,
        thumbnail_review_enabled=True,
    )
    fixture_settings.content_visual_backend = visual_backend
    fixture_settings.content_visual_review_enabled = visual_review_enabled
    monkeypatch.setattr(a1_executor, "settings", fixture_settings)
    first = await execute_content_script_run(
        session,
        idempotency_key=idempotency_key,
        input_payload=PAYLOAD,
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
    return await continue_run(session, storage_root, idempotency_key), storage_root


async def approve_visuals(
    session: AsyncSession, run: Run, storage_root: Path, key: str
) -> Run:
    approval = await pending_approval(session, run, VISUAL_REVIEW_APPROVAL_ACTION)
    await decide_approval(
        session,
        approval=approval,
        decision=ApprovalStatus.APPROVED,
        actor="local-owner",
        reason="imagens conferidas",
    )
    assert (
        await finalize_content_script_approval(
            session, approval=approval, storage_root=storage_root
        )
        is True
    )
    return await continue_run(session, storage_root, key)


async def _step_names(session: AsyncSession, run: Run) -> list[str]:
    return [
        step.name
        for step in (
            await session.scalars(
                select(StepRun).where(StepRun.run_id == run.id).order_by(StepRun.ordinal)
            )
        ).all()
    ]


async def test_a3_gate_pauses_the_run_before_captions_and_shows_each_image(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, storage_root = await run_to_visual_gate(
        session, tmp_path, monkeypatch, idempotency_key="content-a3-gate-open"
    )
    approval = await pending_approval(session, run, VISUAL_REVIEW_APPROVAL_ACTION)

    assert run.status == RunStatus.AWAITING_APPROVAL.value
    assert approval.status == ApprovalStatus.PENDING.value
    assert await _step_names(session, run) == [
        "generate-script-a1",
        "synthesize-tts-a2",
        "prepare-visuals-a3",
    ]
    review = await load_content_approval_review(
        session, approval=approval, storage_root=storage_root
    )
    assert isinstance(review, VisualReview)
    assert review.usage_scope == QUALITY_TEST_USAGE_SCOPE
    assert (review.width, review.height) == (1080, 1920)
    assert [image.index for image in review.images] == list(range(1, len(review.images) + 1))
    assert all(image.narration_text for image in review.images)
    assert " ".join(image.narration_text for image in review.images) == (
        " ".join(rehearsal_script().narration.split())
    )
    assert review.images[0].generation_prompt.startswith("scene 1")
    assert all(image.image_path.is_file() for image in review.images)
    projection = approval.review_payload or {}
    assert "pessoa real" in projection["checklist"]
    assert "não monetizável" in projection["usage"]
    assert str(storage_root) not in " ".join(projection.values())


async def test_a3_approval_continues_the_same_run_to_the_final_review(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "content-a3-gate-approved"
    run, storage_root = await run_to_visual_gate(
        session, tmp_path, monkeypatch, idempotency_key=key
    )
    run = await approve_visuals(session, run, storage_root, key)

    assert run.status == RunStatus.AWAITING_APPROVAL.value
    await pending_approval(session, run, FINAL_REVIEW_APPROVAL_ACTION)
    names = await _step_names(session, run)
    assert names[:5] == [
        "generate-script-a1",
        "synthesize-tts-a2",
        "prepare-visuals-a3",
        "generate-captions-a4",
        "assemble-video-a5",
    ]
    # A second delivery replays every step and opens no second image gate.
    replay = await continue_run(session, storage_root, key)
    approvals = (
        await session.scalars(
            select(Approval).where(
                Approval.run_id == replay.id,
                Approval.action == VISUAL_REVIEW_APPROVAL_ACTION,
            )
        )
    ).all()
    assert len(approvals) == 1
    assert await _step_names(session, replay) == names


async def test_a3_rejection_cancels_the_run_without_captions_or_assembly(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, storage_root = await run_to_visual_gate(
        session, tmp_path, monkeypatch, idempotency_key="content-a3-gate-rejected"
    )
    approval = await pending_approval(session, run, VISUAL_REVIEW_APPROVAL_ACTION)
    await decide_approval(
        session,
        approval=approval,
        decision=ApprovalStatus.REJECTED,
        actor="local-owner",
        reason="a imagem 2 não combina com o trecho",
    )

    assert run.status == RunStatus.CANCELLED.value
    with pytest.raises(VisualReviewGateError, match="not approved"):
        await finalize_visual_approval(
            session, approval=approval, storage_root=storage_root
        )
    assert len(await _step_names(session, run)) == 3
    assert not list(storage_root.rglob("*.mp4"))


async def test_a3_gate_refuses_images_changed_after_the_review_opened(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "content-a3-gate-tampered"
    run, storage_root = await run_to_visual_gate(
        session, tmp_path, monkeypatch, idempotency_key=key
    )
    approval = await pending_approval(session, run, VISUAL_REVIEW_APPROVAL_ACTION)
    image = sorted(storage_root.rglob("visual-*.png"))[0]
    _write_png(image, width=1080, height=1920)
    image.write_bytes(image.read_bytes() + b"tampered")

    with pytest.raises(VisualReviewGateError, match="size does not match|checksum"):
        await load_visual_review(session, approval=approval, storage_root=storage_root)
    await decide_approval(
        session,
        approval=approval,
        decision=ApprovalStatus.APPROVED,
        actor="local-owner",
        reason="aprovado sem perceber a troca",
    )
    with pytest.raises(VisualReviewGateError):
        await finalize_content_script_approval(
            session, approval=approval, storage_root=storage_root
        )


async def test_generated_visuals_cannot_run_without_the_a3_gate(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ContentScriptRunNotRunnableError, match="A3 visual review gate"):
        await run_to_visual_gate(
            session,
            tmp_path,
            monkeypatch,
            idempotency_key="content-a3-gate-required",
            visual_review_enabled=False,
        )


async def test_a3_gate_needs_the_stages_it_guards(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_root = tmp_path / "storage"
    fixture_settings = rehearsal_settings(storage_root, import_root=tmp_path / "imports")
    fixture_settings.content_visual_review_enabled = True
    fixture_settings.content_assembly_backend = "disabled"
    monkeypatch.setattr(a1_executor, "settings", fixture_settings)
    with pytest.raises(ContentScriptRunNotRunnableError, match="A3 visual review requires"):
        await execute_content_script_run(
            session,
            idempotency_key="content-a3-gate-dependencies",
            input_payload=PAYLOAD,
            script_generator=lambda *_args: rehearsal_script(),
            storage_root=storage_root,
        )


async def test_a3_gate_evidence_is_frozen_for_later_deliveries(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "content-a3-gate-frozen"
    run, storage_root = await run_to_visual_gate(
        session, tmp_path, monkeypatch, idempotency_key=key
    )
    approval = await pending_approval(session, run, VISUAL_REVIEW_APPROVAL_ACTION)
    await decide_approval(
        session,
        approval=approval,
        decision=ApprovalStatus.APPROVED,
        actor="local-owner",
        reason="imagens conferidas",
    )
    manifest = next(storage_root.rglob("visuals/manifest.json"))
    manifest.write_text(manifest.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    # The changed manifest no longer matches its registered artifact: the run fails
    # at A3 and never reaches captions or assembly.
    continued = await continue_run(session, storage_root, key)
    assert continued.status == RunStatus.FAILED.value
    assert (continued.error or {})["code"] == "CONTENT_VISUALS_EVIDENCE_PERSIST_FAILED"
    assert len(await _step_names(session, continued)) == 3
