"""The quality-test usage scope of generated visuals, from A3 to the local export."""

from __future__ import annotations

import json
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
from src.pipeline import a1_executor, assembly, visuals
from src.pipeline.a1_executor import (
    ContentScriptInput,
    finalize_content_script_approval,
    parse_content_script_form,
)
from src.pipeline.final_review import (
    FINAL_REVIEW_APPROVAL_ACTION,
    load_final_video_review,
)
from src.pipeline.local_export import load_local_export_package
from src.pipeline.thumbnail_review import (
    THUMBNAIL_REVIEW_APPROVAL_ACTION,
    load_thumbnail_review,
    thumbnail_decision_payload,
)
from src.pipeline.visual_generation import FLUX2_KLEIN_QUALITY_TEST_BACKEND
from src.pipeline.visuals import (
    QUALITY_TEST_USAGE_SCOPE,
    VisualPermanentAdapterError,
    VisualRequest,
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
from tests.pipeline.test_visual_review import (
    approve_visuals,
    pending_approval,
    run_to_visual_gate,
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


async def _run_to_final_review(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    idempotency_key: str,
    visual_backend: str,
) -> tuple[Run, Path]:
    """Run the chain with generated images; the A3 gate exists only for that backend."""
    gated = visual_backend == FLUX2_KLEIN_QUALITY_TEST_BACKEND
    run, storage_root = await run_to_visual_gate(
        session,
        tmp_path,
        monkeypatch,
        idempotency_key=idempotency_key,
        visual_backend=visual_backend,
        visual_review_enabled=gated,
    )
    if gated:
        run = await approve_visuals(session, run, storage_root, idempotency_key)
    return run, storage_root


async def _approval(session: AsyncSession, run: Run, action: str) -> Approval:
    return await pending_approval(session, run, action)


async def test_generated_visuals_keep_the_quality_test_scope_until_the_local_export(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, storage_root = await _run_to_final_review(
        session,
        tmp_path,
        monkeypatch,
        idempotency_key="content-quality-test-scope",
        visual_backend=FLUX2_KLEIN_QUALITY_TEST_BACKEND,
    )
    steps = {
        step.name: step
        for step in (
            await session.scalars(select(StepRun).where(StepRun.run_id == run.id))
        ).all()
    }
    manifest_artifact = await session.scalar(
        select(Artifact).where(
            Artifact.run_id == run.id, Artifact.artifact_type == "visual_manifest"
        )
    )
    assert manifest_artifact is not None
    manifest = json.loads(
        (storage_root / manifest_artifact.relative_path).read_text(encoding="utf-8")
    )

    # A3: the bundle is labelled and every asset records what produced it.
    assert manifest["usage_scope"] == QUALITY_TEST_USAGE_SCOPE
    assert all(asset["commercial_use"] is False for asset in manifest["assets"])
    assert manifest["assets"][0]["generation_prompt"].startswith("scene 1")
    assert manifest["assets"][0]["seed"] == 1001
    visual_step = steps["prepare-visuals-a3"]
    assert visual_step.queue == "gpu"
    assert (visual_step.input_payload or {})["usage_scope"] == QUALITY_TEST_USAGE_SCOPE
    # A5: the video inherits the label.
    assert (steps["assemble-video-a5"].output_payload or {})["visual_usage_scope"] == (
        QUALITY_TEST_USAGE_SCOPE
    )

    # A7: the reviewer is told, and the decision is bound to the scope.
    final_approval = await _approval(session, run, FINAL_REVIEW_APPROVAL_ACTION)
    final_review = await load_final_video_review(
        session, approval=final_approval, storage_root=storage_root
    )
    assert final_review.visual_usage_scope == QUALITY_TEST_USAGE_SCOPE
    assert "não monetizável" in (final_approval.review_payload or {})["publication"]
    await decide_approval(
        session,
        approval=final_approval,
        decision=ApprovalStatus.APPROVED,
        actor="local-owner",
        reason="vídeo de teste revisado",
    )
    await finalize_content_script_approval(
        session, approval=final_approval, storage_root=storage_root
    )

    # A8 and the export: the finished run and its manifest still say so.
    thumbnail_approval = await _approval(session, run, THUMBNAIL_REVIEW_APPROVAL_ACTION)
    thumbnail_review = await load_thumbnail_review(
        session, approval=thumbnail_approval, storage_root=storage_root
    )
    await decide_approval(
        session,
        approval=thumbnail_approval,
        decision=ApprovalStatus.APPROVED,
        actor="local-owner",
        reason="thumbnail escolhida",
        decision_payload=thumbnail_decision_payload(
            thumbnail_review,
            thumbnail_artifact_id=thumbnail_review.candidates[0].artifact_id,
        ),
    )
    await finalize_content_script_approval(
        session, approval=thumbnail_approval, storage_root=storage_root
    )
    assert run.status == RunStatus.SUCCEEDED.value
    output = run.output_payload or {}
    assert output["visual_usage_scope"] == QUALITY_TEST_USAGE_SCOPE
    assert output["published"] is False
    package = await load_local_export_package(session, run=run, storage_root=storage_root)
    assert package.visual_usage_scope == QUALITY_TEST_USAGE_SCOPE
    assert package.manifest["usage_scope"] == QUALITY_TEST_USAGE_SCOPE
    assert package.manifest["monetizable"] is False
    assert package.manifest["published"] is False


async def test_assets_without_commercial_use_stay_blocked_for_any_other_backend(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, storage_root = await _run_to_final_review(
        session,
        tmp_path,
        monkeypatch,
        idempotency_key="content-quality-test-scope-blocked",
        visual_backend="test",
    )
    assert run.status == RunStatus.FAILED.value
    assert (run.error or {})["code"] == "CONTENT_VISUALS_COMMERCIAL_USE_BLOCKED"
    assert not list(storage_root.rglob("visual-*.png"))


async def test_a_published_quality_test_bundle_is_not_replayed_as_commercial(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, storage_root = await _run_to_final_review(
        session,
        tmp_path,
        monkeypatch,
        idempotency_key="content-quality-test-scope-replay",
        visual_backend=FLUX2_KLEIN_QUALITY_TEST_BACKEND,
    )
    manifest_path = next(storage_root.rglob("visuals/manifest.json"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    requests = tuple(
        VisualRequest(asset["index"], asset["prompt"], asset["width"], asset["height"])
        for asset in manifest["assets"]
    )
    context = SimpleNamespace(
        script_artifact_id=manifest["script_artifact_id"],
        script_artifact_sha256=manifest["script_artifact_sha256"],
        audio_artifact_id=manifest["audio_artifact_id"],
        audio_artifact_sha256=manifest["audio_artifact_sha256"],
    )

    with pytest.raises(
        VisualPermanentAdapterError, match="CONTENT_VISUALS_COMMERCIAL_USE_BLOCKED"
    ):
        visuals._load_existing_bundle(manifest_path.parent, requests, context, storage_root)
    reloaded = visuals._load_existing_bundle(
        manifest_path.parent,
        requests,
        context,
        storage_root,
        allow_quality_test_assets=True,
    )
    assert run.status == RunStatus.AWAITING_APPROVAL.value
    assert reloaded["usage_scope"] == QUALITY_TEST_USAGE_SCOPE


def _manifest_file(tmp_path: Path, *, commercial_use: object, usage_scope: object) -> Path:
    payload: dict[str, object] = {
        "schema_version": 1,
        "script_artifact_id": 1,
        "script_artifact_sha256": "a" * 64,
        "audio_artifact_id": 2,
        "audio_artifact_sha256": "b" * 64,
        "format": "short",
        "visual_count": 1,
        "assets": [
            {
                "file_name": "visual-001.png",
                "sha256": "c" * 64,
                "size_bytes": 10,
                "width": 1080,
                "height": 1920,
                "commercial_use": commercial_use,
            }
        ],
    }
    if usage_scope is not None:
        payload["usage_scope"] = usage_scope
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("commercial_use", "usage_scope", "expected"),
    [
        (True, None, None),
        (False, QUALITY_TEST_USAGE_SCOPE, QUALITY_TEST_USAGE_SCOPE),
        (False, None, ValueError),
        (True, QUALITY_TEST_USAGE_SCOPE, ValueError),
        (False, "monetizable", ValueError),
        ("yes", None, ValueError),
    ],
)
def test_assembly_accepts_non_commercial_visuals_only_in_a_labelled_bundle(
    tmp_path: Path, commercial_use: object, usage_scope: object, expected: object
) -> None:
    def validate() -> str | None:
        return assembly._validate_visual_manifest(
            _manifest_file(tmp_path, commercial_use=commercial_use, usage_scope=usage_scope),
            manifest_artifact=SimpleNamespace(size_bytes=500),  # type: ignore[arg-type]
            visual_artifacts=(
                SimpleNamespace(  # type: ignore[arg-type]
                    relative_path="content-engine/run-1/visuals/visual-001.png",
                    sha256="c" * 64,
                    size_bytes=10,
                ),
            ),
            script_artifact_id=1,
            script_artifact_sha256="a" * 64,
            audio_artifact=SimpleNamespace(id=2, sha256="b" * 64),  # type: ignore[arg-type]
            video_format="short",
            width=1080,
            height=1920,
        )

    if expected is ValueError:
        with pytest.raises(ValueError, match="assembly visual manifest"):
            validate()
    else:
        assert validate() == expected


def test_visual_style_is_an_optional_validated_run_input() -> None:
    base = {"topic": "Sono e memória", "persona": "Ciência acessível", "format": "short"}
    assert "visual_style" not in ContentScriptInput.model_validate(base).as_payload()
    styled = ContentScriptInput.model_validate({**base, "visual_style": "dark"})
    assert styled.as_payload()["visual_style"] == "dark"
    assert parse_content_script_form({**base, "visual_style": "cartoon"})[
        "visual_style"
    ] == "cartoon"
    assert "visual_style" not in parse_content_script_form({**base, "visual_style": ""})
    with pytest.raises(ValueError):
        ContentScriptInput.model_validate({**base, "visual_style": "anime de estúdio"})


def test_generating_backend_needs_its_local_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        a1_executor,
        "settings",
        SimpleNamespace(content_visual_backend=FLUX2_KLEIN_QUALITY_TEST_BACKEND),
    )
    assert a1_executor._visuals_queue().value == "gpu"
    with pytest.raises(a1_executor.ContentScriptRunNotRunnableError, match="local paths"):
        a1_executor._configured_visual_adapter(
            ContentScriptInput.model_validate(
                {"topic": "Sono e memória", "persona": "Ciência acessível", "format": "short"}
            )
        )
