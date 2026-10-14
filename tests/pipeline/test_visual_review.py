"""Content Engine A3 human review gate for the images, before captions and assembly."""

from __future__ import annotations

import sys
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
    redo_decision_payload,
)
from src.pipeline.visuals import (
    QUALITY_TEST_USAGE_SCOPE,
    VisualAssetResult,
    VisualRequest,
)
from src.platform.approval_service import decide_approval
from src.platform.models import Approval, ApprovalStatus, Artifact, Run, RunStatus, StepRun
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
# A test may swap the generator to observe which requests each round asks for.
generated_adapter_override: object = None


async def continue_run(session: AsyncSession, storage_root: Path, key: str) -> Run:
    """Deliver the durable dispatch again, as the worker does after an approval."""
    result = await execute_content_script_run(
        session,
        idempotency_key=key,
        input_payload=PAYLOAD,
        script_generator=lambda *_args: pytest.fail("A1 must replay after approval"),
        tts_synthesizer=write_synthetic_narration_wav,
        visual_adapter=generated_adapter_override or generated_adapter,
        assembly_adapter=_assembly_adapter,
        storage_root=storage_root,
    )
    run = await session.get(Run, result.run_id)
    assert run is not None
    return run


async def pending_approval(session: AsyncSession, run: Run, action: str) -> Approval:
    # The latest one: draft rounds open one A3 approval each.
    approval = await session.scalar(
        select(Approval)
        .where(Approval.run_id == run.id, Approval.action == action)
        .order_by(Approval.id.desc())
        .limit(1)
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
                select(StepRun)
                .where(StepRun.run_id == run.id)
                .order_by(StepRun.ordinal, StepRun.id)
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
    # Generated images are reviewed as a draft round before A3 publishes anything.
    assert await _step_names(session, run) == [
        "generate-script-a1",
        "synthesize-tts-a2",
        "draft-visuals-a3-r1",
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
    assert review.round_number == 1 and review.can_redo
    assert "rodada 1" in (approval.review_payload or {})["visuals"]
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
    assert names[:6] == [
        "generate-script-a1",
        "synthesize-tts-a2",
        "draft-visuals-a3-r1",
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
    manifest = next(storage_root.rglob("visual-drafts/r1/manifest.json"))
    manifest.write_text(manifest.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    # The changed manifest no longer matches its registered artifact: the run fails
    # at A3 and never reaches captions or assembly.
    continued = await continue_run(session, storage_root, key)
    assert continued.status == RunStatus.FAILED.value
    assert (continued.error or {})["code"] == (
        "CONTENT_VISUALS_DRAFT_EVIDENCE_PERSIST_FAILED"
    )
    assert len(await _step_names(session, continued)) == 3


class _RecordingAdapter:
    """The fake generator, recording which requests each round asked for."""

    def __init__(self) -> None:
        self.calls: list[tuple[int, ...]] = []

    def __call__(
        self, requests: tuple[VisualRequest, ...], destination: Path
    ) -> tuple[VisualAssetResult, ...]:
        self.calls.append(tuple(request.index for request in requests))
        results = generated_adapter(requests, destination)
        if len(self.calls) > 1:
            # A later round must produce different pixels for the images it redoes.
            for request in requests:
                path = destination / f"visual-{request.index:03d}.png"
                _write_png(path, width=request.width, height=request.height, shade=90)
        return results


async def _send_back(
    session: AsyncSession,
    run: Run,
    storage_root: Path,
    key: str,
    notes: dict[int, str],
) -> Run:
    approval = await pending_approval(session, run, VISUAL_REVIEW_APPROVAL_ACTION)
    review = await load_visual_review(session, approval=approval, storage_root=storage_root)
    await decide_approval(
        session,
        approval=approval,
        decision=ApprovalStatus.APPROVED,
        actor="local-owner",
        reason="Ajustes pedidos nas imagens " + ", ".join(map(str, sorted(notes))),
        decision_payload=redo_decision_payload(review, notes),
    )
    assert (
        await finalize_content_script_approval(
            session, approval=approval, storage_root=storage_root
        )
        is True
    )
    return await continue_run(session, storage_root, key)


async def test_sent_back_images_are_redone_in_a_new_round_and_the_rest_kept(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = "content-a3-redo"
    recorder = _RecordingAdapter()
    monkeypatch.setattr(sys.modules[__name__], "generated_adapter_override", recorder)
    run, storage_root = await run_to_visual_gate(
        session, tmp_path, monkeypatch, idempotency_key=key
    )
    first = await load_visual_review(
        session,
        approval=await pending_approval(session, run, VISUAL_REVIEW_APPROVAL_ACTION),
        storage_root=storage_root,
    )
    note = "Tire a pessoa e mostre só a mesa arrumada"

    run = await _send_back(session, run, storage_root, key, {1: note})

    approvals = list(
        (
            await session.scalars(
                select(Approval)
                .where(
                    Approval.run_id == run.id,
                    Approval.action == VISUAL_REVIEW_APPROVAL_ACTION,
                )
                .order_by(Approval.id)
            )
        ).all()
    )
    assert [approval.status for approval in approvals] == ["approved", "pending"]
    assert run.status == RunStatus.AWAITING_APPROVAL.value
    second = await load_visual_review(
        session, approval=approvals[1], storage_root=storage_root
    )
    assert second.round_number == 2
    assert recorder.calls == [tuple(image.index for image in first.images), (1,)]
    assert second.images[0].sha256 != first.images[0].sha256
    assert second.images[0].revision_note == note
    assert [image.sha256 for image in second.images[1:]] == [
        image.sha256 for image in first.images[1:]
    ]
    assert all(not image.revision_note for image in second.images[1:])
    assert await _step_names(session, run) == [
        "generate-script-a1",
        "synthesize-tts-a2",
        "draft-visuals-a3-r1",
        "draft-visuals-a3-r2",
    ]

    # Approving round 2 publishes exactly its images as the A3 bundle.
    run = await approve_visuals(session, run, storage_root, key)
    a3_images = list(
        (
            await session.scalars(
                select(Artifact)
                .where(Artifact.run_id == run.id, Artifact.artifact_type == "visual_image")
                .order_by(Artifact.id)
            )
        ).all()
    )
    assert [image.sha256 for image in a3_images] == [image.sha256 for image in second.images]
    assert recorder.calls == [tuple(image.index for image in first.images), (1,)]
    await pending_approval(session, run, FINAL_REVIEW_APPROVAL_ACTION)


async def test_redo_requests_are_validated(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, storage_root = await run_to_visual_gate(
        session, tmp_path, monkeypatch, idempotency_key="content-a3-redo-limits"
    )
    review = await load_visual_review(
        session,
        approval=await pending_approval(session, run, VISUAL_REVIEW_APPROVAL_ACTION),
        storage_root=storage_root,
    )
    assert redo_decision_payload(review, {1: "  "}) == {
        "choice": "redo",
        "note_1": "Refazer esta imagem com outra cena.",
    }
    with pytest.raises(VisualReviewGateError, match="at least one"):
        redo_decision_payload(review, {})
    with pytest.raises(VisualReviewGateError, match="unknown image"):
        redo_decision_payload(review, {99: "x"})
    with pytest.raises(VisualReviewGateError, match="unknown image or a long note"):
        redo_decision_payload(review, {1: "x" * 251})
    with pytest.raises(VisualReviewGateError, match="at most 7"):
        redo_decision_payload(review, {index: "x" for index in range(1, 9)})
    bundle_review = VisualReview(
        approval_id=1,
        run_id=1,
        manifest_artifact_id=1,
        manifest_sha256="a" * 64,
        width=1080,
        height=1920,
        usage_scope=None,
        licenses=(),
        images=review.images,
    )
    with pytest.raises(VisualReviewGateError, match="only generated drafts"):
        redo_decision_payload(bundle_review, {1: "x"})
