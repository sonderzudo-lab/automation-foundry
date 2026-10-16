"""A5a clip review gate: rounds, decisions, carried clips and the official import."""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.config import Settings
from src.core.database import Base
from src.pipeline import a1_executor
from src.pipeline.assembly import _scene_clip_sources
from src.pipeline.scene_animation import (
    SCENE_ANIMATION_STEP_NAME,
    SceneAnimationLimits,
    SceneAnimationResult,
    SceneRoundPlan,
    execute_scene_animation_step,
    scene_clip_seed,
    scene_clips_directory,
    scene_clips_round_directory,
)
from src.pipeline.scene_clips import SceneClipsManifest
from src.pipeline.scene_review import (
    SCENE_CLIPS_REVIEW_APPROVAL_ACTION,
    SceneClipReview,
    SceneClipReviewGateError,
    finalize_scene_clip_approval,
    import_plan,
    load_reviewed_scene_clips,
    load_scene_clip_review,
    next_round_plan,
    redo_decision_payload,
    request_scene_clip_approval,
    requested_changes,
)
from src.pipeline.text_model import TextModelRequest, TextModelResult
from src.platform.approval_service import decide_approval
from src.platform.models import (
    Approval,
    ApprovalStatus,
    Artifact,
    MetricPoint,
    Run,
    RunStatus,
    StepRun,
)
from tests.pipeline.test_scene_animation import (
    _after_a4,
    _Clock,
    _Evidence,
    _limits,
    _Renderer,
    _TextModel,
)

_REWRITTEN = "The folded shirt stays on the bed while the camera drifts slowly to the right"


class _RewriteModel(_TextModel):
    """Answers every requested scene with one fixed rewritten motion."""

    def complete(self, request: TextModelRequest) -> TextModelResult:
        self.requests.append(request)
        count = request.user.count("Scene ")
        answer = {
            "schema_version": 1,
            "scenes": [{"index": index, "motion": _REWRITTEN} for index in range(1, count + 1)],
        }
        return TextModelResult(
            text=json.dumps(answer),
            provider=self.provider,
            model_id=self.model_id,
            duration_seconds=Decimal("1"),
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


async def animate_round(
    session: AsyncSession,
    evidence: _Evidence,
    storage: Path,
    renderer: _Renderer,
    plan: SceneRoundPlan,
    *,
    model: _TextModel | None = None,
    limits: SceneAnimationLimits | None = None,
) -> SceneAnimationResult:
    async def no_sleep(_seconds: float) -> None:
        return None

    return await execute_scene_animation_step(
        session,
        run=evidence.run,
        approval=evidence.approval,
        approval_input_payload=evidence.approval_input,
        audio_artifact=evidence.audio,
        visual_manifest_artifact=evidence.manifest,
        visual_artifacts=evidence.visuals,
        caption_artifact=evidence.caption,
        idempotency_key=evidence.run.idempotency_key,
        storage_root=storage,
        renderer_factory=renderer.factory,
        motion_model=model or _TextModel(),
        limits=limits or _limits(),
        gpu_power_watts=400,
        sleep=no_sleep,
        clock=renderer.clock,
        round_plan=plan,
    )


@dataclass
class Gate:
    evidence: _Evidence
    storage: Path
    approval: Approval
    renderer: _Renderer

    @property
    def run(self) -> Run:
        return self.evidence.run


async def open_round_one(session: AsyncSession, tmp_path: Path, key: str) -> Gate:
    storage = tmp_path / "storage"
    evidence = await _after_a4(session, storage, key=key)
    renderer = _Renderer(clock=_Clock())
    result = await animate_round(
        session, evidence, storage, renderer, SceneRoundPlan(round_number=1)
    )
    assert result.manifest_artifact_id is not None
    await request_scene_clip_approval(
        session,
        run=evidence.run,
        idempotency_key=evidence.run.idempotency_key,
        storage_root=storage,
        round_number=1,
    )
    approval = await _latest_approval(session, evidence.run)
    return Gate(evidence, storage, approval, renderer)


async def _latest_approval(session: AsyncSession, run: Run) -> Approval:
    approval = await session.scalar(
        select(Approval)
        .where(Approval.run_id == run.id, Approval.action == SCENE_CLIPS_REVIEW_APPROVAL_ACTION)
        .order_by(Approval.id.desc())
        .limit(1)
    )
    assert approval is not None
    return approval


async def _step_names(session: AsyncSession, run: Run) -> list[str]:
    return [
        step.name
        for step in (
            await session.scalars(
                select(StepRun).where(StepRun.run_id == run.id).order_by(StepRun.id)
            )
        ).all()
        if "a5a" in step.name
    ]


def _same_content(left: SceneClipsManifest, right: SceneClipsManifest) -> bool:
    """Equal manifests, except for the file names the clips were copied to."""

    def strip(manifest: SceneClipsManifest) -> dict[str, object]:
        dumped = manifest.model_dump()
        for scene in dumped["scenes"]:
            scene.pop("video", None)
        return dumped

    return strip(left) == strip(right)


def _manifest(path: Path) -> SceneClipsManifest:
    return SceneClipsManifest.model_validate_json((path / "manifest.json").read_text("utf-8"))


async def decide_redo(
    session: AsyncSession,
    gate: Gate,
    notes: dict[int, str],
    keep_still: set[int] | None = None,
) -> SceneRoundPlan:
    review = await load_scene_clip_review(
        session, approval=gate.approval, storage_root=gate.storage
    )
    await decide_approval(
        session,
        approval=gate.approval,
        decision=ApprovalStatus.APPROVED,
        actor="local-owner",
        reason="ajustes pedidos",
        decision_payload=redo_decision_payload(review, notes, keep_still or set()),
    )
    reviewed = await load_reviewed_scene_clips(
        session, approval=gate.approval, storage_root=gate.storage
    )
    redo, still = requested_changes(gate.approval)
    return next_round_plan(reviewed, notes=redo, keep_still=still)


async def open_next_round(
    session: AsyncSession,
    gate: Gate,
    plan: SceneRoundPlan,
    *,
    model: _TextModel | None = None,
) -> tuple[Gate, _Renderer, SceneAnimationResult]:
    renderer = _Renderer(clock=_Clock())
    result = await animate_round(
        session, gate.evidence, gate.storage, renderer, plan, model=model
    )
    assert plan.round_number is not None
    await request_scene_clip_approval(
        session,
        run=gate.run,
        idempotency_key=gate.run.idempotency_key,
        storage_root=gate.storage,
        round_number=plan.round_number,
    )
    approval = await _latest_approval(session, gate.run)
    return Gate(gate.evidence, gate.storage, approval, renderer), renderer, result


async def test_round_one_is_a_draft_step_with_its_own_manifest_clips_and_gate(
    session: AsyncSession, tmp_path: Path
) -> None:
    gate = await open_round_one(session, tmp_path, "a5a-gate-round-one")

    assert await _step_names(session, gate.run) == ["draft-scenes-a5a-r1"]
    assert gate.run.status == RunStatus.AWAITING_APPROVAL.value
    assert gate.approval.status == ApprovalStatus.PENDING.value
    assert gate.approval.idempotency_key.endswith(":scene-clips-review-r1")
    run_id = gate.run.id
    assert run_id is not None
    # The round keeps its own files; the official A5a directory is still empty.
    round_directory = scene_clips_round_directory(gate.storage, run_id=run_id, round_number=1)
    assert (round_directory / "manifest.json").is_file()
    assert len(list((round_directory / "clips").glob("*.mp4"))) == 2
    assert not (scene_clips_directory(gate.storage, run_id=run_id) / "manifest.json").exists()

    review = await load_scene_clip_review(
        session, approval=gate.approval, storage_root=gate.storage
    )
    assert isinstance(review, SceneClipReview) and review.round_number == 1
    assert [scene.is_clip for scene in review.scenes] == [True, True, False]
    first, second, third = review.scenes
    assert first.narration_text and second.narration_text
    assert first.motion.startswith("The adult slowly lifts") and second.frames
    assert first.clip_path is not None and first.clip_path.is_file() and first.image_path.is_file()
    assert third.still_reason == "too_long" and not third.can_redo
    projection = gate.approval.review_payload or {}
    assert "rodada 1" in projection["clips"] and "pessoa nova" in projection["checklist"]
    assert str(gate.storage) not in " ".join(projection.values())


async def test_approving_a_round_publishes_exactly_it_under_the_official_step(
    session: AsyncSession, tmp_path: Path
) -> None:
    gate = await open_round_one(session, tmp_path, "a5a-gate-approve")
    await decide_approval(
        session,
        approval=gate.approval,
        decision=ApprovalStatus.APPROVED,
        actor="local-owner",
        reason="clipes conferidos",
    )
    assert await finalize_scene_clip_approval(
        session, approval=gate.approval, storage_root=gate.storage
    )
    assert requested_changes(gate.approval) == ({}, set())
    reviewed = await load_reviewed_scene_clips(
        session, approval=gate.approval, storage_root=gate.storage
    )
    fresh = _Renderer(clock=_Clock())

    result = await animate_round(
        session, gate.evidence, gate.storage, fresh, import_plan(reviewed)
    )

    assert await _step_names(session, gate.run) == ["draft-scenes-a5a-r1", SCENE_ANIMATION_STEP_NAME]
    # Nothing was generated: no runtime was even created.
    assert fresh.workspaces == [] and fresh.starts == 0 and fresh.rendered == []
    run_id = gate.run.id
    assert run_id is not None
    official = _manifest(scene_clips_directory(gate.storage, run_id=run_id))
    assert _same_content(official, reviewed.manifest)
    assert result.manifest_artifact_id is not None and len(result.clip_artifact_ids) == 2
    # A5 accepts the records exactly as it does for a gate-off run.
    manifest_artifact = await session.get(Artifact, result.manifest_artifact_id)
    clips = [await session.get(Artifact, item) for item in result.clip_artifact_ids]
    assert manifest_artifact is not None and all(clip is not None for clip in clips)
    manifest_path, clip_paths = await _scene_clip_sources(
        session,
        run=gate.run,
        manifest_artifact=manifest_artifact,
        clip_artifacts=tuple(clip for clip in clips if clip is not None),
        storage_root=gate.storage,
    )
    assert manifest_path is not None and len(clip_paths) == 2
    # Carried clips add no GPU observations of their own.
    gpu = (
        await session.scalars(
            select(MetricPoint).where(
                MetricPoint.run_id == run_id,
                MetricPoint.name == "content_scene_clip_gpu_seconds",
            )
        )
    ).all()
    assert len(gpu) == 2


async def test_redo_rewrites_only_the_noted_motion_and_copies_the_other_clips(
    session: AsyncSession, tmp_path: Path
) -> None:
    gate = await open_round_one(session, tmp_path, "a5a-gate-redo")
    run_id = gate.run.id
    assert run_id is not None
    first_round = _manifest(scene_clips_round_directory(gate.storage, run_id=run_id, round_number=1))
    plan = await decide_redo(session, gate, {2: "Faça a câmera ir para a direita"})
    assert gate.run.status == RunStatus.RUNNING.value
    model = _RewriteModel()

    second_gate, renderer, _result = await open_next_round(session, gate, plan, model=model)

    assert renderer.rendered == [2]
    assert len(model.requests) == 1
    request = model.requests[0].user
    assert "Faça a câmera ir para a direita" in request and "Steam rises gently" in request
    assert request.count("Scene ") == 1
    second = _manifest(scene_clips_round_directory(gate.storage, run_id=run_id, round_number=2))
    old_first, old_second = first_round.scenes[0], first_round.scenes[1]
    new_first, new_second = second.scenes[0], second.scenes[1]
    assert new_first.kind == old_first.kind == "clip"
    assert new_second.kind == old_second.kind == "clip"
    assert new_first.video_sha256 == old_first.video_sha256 and new_first.motion == old_first.motion
    assert new_first.seed == old_first.seed
    assert new_second.video_sha256 != old_second.video_sha256
    assert new_second.motion == _REWRITTEN and new_second.seed != old_second.seed
    assert second.scenes[2].kind == "still"
    assert await _step_names(session, gate.run) == ["draft-scenes-a5a-r1", "draft-scenes-a5a-r2"]
    # The first round stays as the evidence of what was reviewed.
    assert len(list((scene_clips_round_directory(
        gate.storage, run_id=run_id, round_number=1) / "clips").glob("*.mp4"))) == 2
    review = await load_scene_clip_review(
        session, approval=second_gate.approval, storage_root=gate.storage
    )
    assert review.round_number == 2
    assert review.scenes[1].revision_note == "Faça a câmera ir para a direita"
    assert review.scenes[0].revision_note == ""
    # The earlier decision still shows what it asked for.
    earlier = await load_scene_clip_review(
        session, approval=gate.approval, storage_root=gate.storage
    )
    assert earlier.requested_redo == ((2, "Faça a câmera ir para a direita"),)


async def test_a_scene_marked_to_keep_its_image_is_not_generated_again(
    session: AsyncSession, tmp_path: Path
) -> None:
    gate = await open_round_one(session, tmp_path, "a5a-gate-keep-still")
    run_id = gate.run.id
    assert run_id is not None
    plan = await decide_redo(session, gate, {}, keep_still={1})
    assert dict(plan.stills) == {1: "owner_choice", 3: "too_long"} and plan.redo == {}
    model = _RewriteModel()

    second_gate, renderer, _ = await open_next_round(session, gate, plan, model=model)

    # No video runtime, and no text model call: only the manifest changes.
    assert renderer.workspaces == [] and renderer.starts == 0 and model.requests == []
    second = _manifest(scene_clips_round_directory(gate.storage, run_id=run_id, round_number=2))
    assert [scene.kind for scene in second.scenes] == ["still", "clip", "still"]
    assert second.scenes[0].reason == "owner_choice"  # type: ignore[union-attr]
    review = await load_scene_clip_review(
        session, approval=second_gate.approval, storage_root=gate.storage
    )
    assert review.requested_still == ()
    assert [scene.is_clip for scene in review.scenes] == [False, True, False]
    # Approving that round publishes the still as such.
    await decide_approval(
        session,
        approval=second_gate.approval,
        decision=ApprovalStatus.APPROVED,
        actor="local-owner",
        reason="ok",
    )
    reviewed = await load_reviewed_scene_clips(
        session, approval=second_gate.approval, storage_root=gate.storage
    )
    published = await animate_round(
        session, gate.evidence, gate.storage, _Renderer(clock=_Clock()), import_plan(reviewed)
    )
    assert len(published.clip_artifact_ids) == 1
    official = _manifest(scene_clips_directory(gate.storage, run_id=run_id))
    assert [scene.kind for scene in official.scenes] == ["still", "clip", "still"]


async def test_rejecting_a_round_cancels_the_run_before_the_official_step(
    session: AsyncSession, tmp_path: Path
) -> None:
    gate = await open_round_one(session, tmp_path, "a5a-gate-reject")

    await decide_approval(
        session,
        approval=gate.approval,
        decision=ApprovalStatus.REJECTED,
        actor="local-owner",
        reason="o movimento da cena 1 está estranho",
    )

    assert gate.run.status == RunStatus.CANCELLED.value
    with pytest.raises(SceneClipReviewGateError, match="not approved"):
        await finalize_scene_clip_approval(
            session, approval=gate.approval, storage_root=gate.storage
        )
    assert await _step_names(session, gate.run) == ["draft-scenes-a5a-r1"]


async def test_a_clip_changed_after_the_round_was_made_is_refused(
    session: AsyncSession, tmp_path: Path
) -> None:
    gate = await open_round_one(session, tmp_path, "a5a-gate-tampered")
    run_id = gate.run.id
    assert run_id is not None
    clips = sorted(
        (
            scene_clips_round_directory(gate.storage, run_id=run_id, round_number=1) / "clips"
        ).glob("*.mp4")
    )
    clips[0].write_bytes(clips[0].read_bytes() + b"tampered")

    with pytest.raises(SceneClipReviewGateError, match="size does not match|checksum"):
        await load_scene_clip_review(session, approval=gate.approval, storage_root=gate.storage)
    # An approval given without noticing cannot continue the run either.
    await decide_approval(
        session,
        approval=gate.approval,
        decision=ApprovalStatus.APPROVED,
        actor="local-owner",
        reason="aprovado sem perceber a troca",
    )
    with pytest.raises(SceneClipReviewGateError, match="size does not match|checksum"):
        await finalize_scene_clip_approval(
            session, approval=gate.approval, storage_root=gate.storage
        )
    with pytest.raises(SceneClipReviewGateError, match="size does not match|checksum"):
        await request_scene_clip_approval(
            session,
            run=gate.run,
            idempotency_key=gate.run.idempotency_key,
            storage_root=gate.storage,
            round_number=1,
        )


async def test_a_clip_changed_between_review_and_next_round_fails_the_round_closed(
    session: AsyncSession, tmp_path: Path
) -> None:
    gate = await open_round_one(session, tmp_path, "a5a-gate-changed-late")
    run_id = gate.run.id
    assert run_id is not None
    plan = await decide_redo(session, gate, {2: "outro movimento"})
    carried = plan.carried[0].path
    carried.write_bytes(carried.read_bytes() + b"tampered")
    renderer = _Renderer(clock=_Clock())

    result = await animate_round(
        session, gate.evidence, gate.storage, renderer, plan, model=_RewriteModel()
    )

    assert result.manifest_artifact_id is None and gate.run.status == RunStatus.FAILED.value
    assert renderer.rendered == []
    attempts = (
        await session.scalars(select(StepRun).where(StepRun.name == "draft-scenes-a5a-r2"))
    ).all()
    assert [(attempt.error or {}).get("code") for attempt in attempts] == [
        "CONTENT_SCENE_CLIPS_ROUND_CONFLICT"
    ]
    assert not (
        scene_clips_round_directory(gate.storage, run_id=run_id, round_number=2) / "manifest.json"
    ).exists()


async def test_a_video_model_that_changed_cannot_add_clips_to_the_same_round(
    session: AsyncSession, tmp_path: Path
) -> None:
    gate = await open_round_one(session, tmp_path, "a5a-gate-model-changed")
    plan = await decide_redo(session, gate, {2: "outro movimento"})
    renderer = _Renderer(clock=_Clock())
    renderer.inventory_sha256 = "d" * 64  # type: ignore[misc]

    result = await animate_round(
        session, gate.evidence, gate.storage, renderer, plan, model=_RewriteModel()
    )

    assert result.manifest_artifact_id is None and renderer.rendered == []
    failed = await session.scalar(
        select(StepRun).where(StepRun.name == "draft-scenes-a5a-r2")
    )
    assert failed is not None
    assert (failed.error or {}).get("code") == "CONTENT_SCENE_CLIPS_RUNTIME_CHANGED"


async def test_redo_requests_are_validated(session: AsyncSession, tmp_path: Path) -> None:
    gate = await open_round_one(session, tmp_path, "a5a-gate-validation")
    review = await load_scene_clip_review(
        session, approval=gate.approval, storage_root=gate.storage
    )

    with pytest.raises(SceneClipReviewGateError, match="at least one"):
        redo_decision_payload(review, {})
    with pytest.raises(SceneClipReviewGateError, match="at most 7"):
        redo_decision_payload(review, {index: "x" for index in range(1, 9)})
    with pytest.raises(SceneClipReviewGateError, match="unknown or non-animatable"):
        redo_decision_payload(review, {3: "a cena é longa demais"})
    with pytest.raises(SceneClipReviewGateError, match="unknown or non-animatable"):
        redo_decision_payload(review, {9: "não existe"})
    with pytest.raises(SceneClipReviewGateError, match="only an animated scene"):
        redo_decision_payload(review, {}, {3})
    with pytest.raises(SceneClipReviewGateError, match="not both"):
        redo_decision_payload(review, {1: "x"}, {1})
    with pytest.raises(SceneClipReviewGateError, match="reject the clips"):
        redo_decision_payload(review, {}, {1, 2})
    with pytest.raises(SceneClipReviewGateError, match="too long"):
        redo_decision_payload(review, {1: "x" * 251})

    payload = redo_decision_payload(review, {2: "  mais   devagar "}, {1})
    assert payload == {"choice": "redo", "note_2": "mais devagar", "still_1": True}
    assert redo_decision_payload(review, {1: "  "})["note_1"].startswith("Refazer")


def test_a_redone_clip_gets_a_seed_of_its_own() -> None:
    sha = "a" * 64
    base = scene_clip_seed(1, sha, "motion")
    assert scene_clip_seed(1, sha, "motion", "") == base
    assert scene_clip_seed(1, sha, "motion", "r2") != base
    assert scene_clip_seed(1, sha, "motion", "r2") != scene_clip_seed(1, sha, "motion", "r3")


def _gate_settings(
    monkeypatch: pytest.MonkeyPatch, storage: Path, *, review: bool
) -> None:
    monkeypatch.setattr(
        a1_executor,
        "settings",
        a1_executor.settings.model_copy(
            update={
                "storage_root": str(storage),
                "celery_soft_time_limit_seconds": 900,
                "content_scene_animation_backend": "ltx23_quality_test",
                "content_scene_clip_review_enabled": review,
            }
        ),
    )


async def _animate_scenes(
    session: AsyncSession, evidence: _Evidence, storage: Path, renderer: _Renderer
) -> SceneAnimationResult | None:
    assert evidence.caption.id is not None
    return await a1_executor._animate_scenes(
        session,
        run=evidence.run,
        approval=evidence.approval,
        approval_input=evidence.approval_input,
        audio_artifact=evidence.audio,
        visual_manifest_artifact_id=evidence.manifest.id,
        visual_artifact_ids=tuple(a.id for a in evidence.visuals if a.id is not None),
        caption_artifact_id=evidence.caption.id,
        idempotency_key=evidence.run.idempotency_key,
        storage_root=storage,
        renderer_factory=renderer.factory,
    )


async def test_the_executor_loops_rounds_until_one_is_approved(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = tmp_path / "storage"
    evidence = await _after_a4(session, storage, key="a5a-gate-executor")
    _gate_settings(monkeypatch, storage, review=True)
    model = _RewriteModel()
    monkeypatch.setattr(a1_executor, "motion_text_model", lambda: model)
    run = evidence.run

    # Round 1 is generated and the run waits for a person.
    renderer_one = _Renderer(clock=_Clock())
    assert await _animate_scenes(session, evidence, storage, renderer_one) is None
    assert renderer_one.rendered == [1, 2] and run.status == RunStatus.AWAITING_APPROVAL.value
    first = await _latest_approval(session, run)
    review = await load_scene_clip_review(session, approval=first, storage_root=storage)
    await decide_approval(
        session,
        approval=first,
        decision=ApprovalStatus.APPROVED,
        actor="local-owner",
        reason="ajuste",
        decision_payload=redo_decision_payload(review, {1: "mais devagar"}),
    )

    # The next delivery replays round 1, generates only scene 1 and waits again.
    renderer_two = _Renderer(clock=_Clock())
    assert await _animate_scenes(session, evidence, storage, renderer_two) is None
    assert renderer_two.rendered == [1] and run.status == RunStatus.AWAITING_APPROVAL.value
    second = await _latest_approval(session, run)
    assert second.id != first.id and second.idempotency_key.endswith("review-r2")
    await decide_approval(
        session,
        approval=second,
        decision=ApprovalStatus.APPROVED,
        actor="local-owner",
        reason="ok",
    )

    # Approved: the official step publishes round 2 without generating anything.
    renderer_three = _Renderer(clock=_Clock())
    published = await _animate_scenes(session, evidence, storage, renderer_three)
    assert published is not None and published.manifest_artifact_id is not None
    assert renderer_three.rendered == [] and renderer_three.workspaces == []
    assert len(published.clip_artifact_ids) == 2
    assert await _step_names(session, run) == [
        "draft-scenes-a5a-r1",
        "draft-scenes-a5a-r2",
        SCENE_ANIMATION_STEP_NAME,
    ]
    run_id = run.id
    assert run_id is not None
    official = _manifest(scene_clips_directory(storage, run_id=run_id))
    second_round = _manifest(scene_clips_round_directory(storage, run_id=run_id, round_number=2))
    assert _same_content(official, second_round)
    assert official.scenes[0].motion == _REWRITTEN  # type: ignore[union-attr]

    # A later delivery replays everything and opens nothing new.
    replay = await _animate_scenes(session, evidence, storage, _Renderer(clock=_Clock()))
    assert replay is not None and replay.replayed
    assert len(await _step_names(session, run)) == 3


async def test_with_the_gate_off_a5a_runs_as_before(
    session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = tmp_path / "storage"
    evidence = await _after_a4(session, storage, key="a5a-gate-off")
    _gate_settings(monkeypatch, storage, review=False)
    monkeypatch.setattr(a1_executor, "motion_text_model", lambda: _TextModel())
    renderer = _Renderer(clock=_Clock())

    result = await _animate_scenes(session, evidence, storage, renderer)

    assert result is not None and renderer.rendered == [1, 2]
    assert await _step_names(session, evidence.run) == [SCENE_ANIMATION_STEP_NAME]
    approvals = (
        await session.scalars(
            select(Approval).where(
                Approval.run_id == evidence.run.id,
                Approval.action == SCENE_CLIPS_REVIEW_APPROVAL_ACTION,
            )
        )
    ).all()
    assert approvals == []
    assert evidence.run.status == RunStatus.RUNNING.value
    assert not a1_executor._scene_clip_review_enabled()


def test_the_clip_review_gate_is_off_by_default_and_needs_the_a5a_backend() -> None:
    assert Settings.model_fields["content_scene_clip_review_enabled"].default is False
    with pytest.raises(ValueError, match="scene animation backend"):
        Settings(content_scene_clip_review_enabled=True)  # type: ignore[call-arg]
