"""Dashboard page, clip and image routes and redo form of the A5a clip review gate."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.database import Base, get_session
from src.dashboard import main as dashboard_main
from src.dashboard.main import create_app
from src.pipeline import a1_executor
from src.pipeline.scene_animation import SceneRoundPlan
from src.pipeline.scene_review import (
    SCENE_CLIPS_REVIEW_APPROVAL_ACTION,
    request_scene_clip_approval,
)
from src.platform.models import Approval, ApprovalStatus, Run, RunStatus
from tests.dashboard.test_dashboard import _assert_review_link
from tests.pipeline.test_scene_animation import _after_a4, _Clock, _Renderer
from tests.pipeline.test_scene_review import animate_round


@pytest.fixture
async def session_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _open_gate(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    key: str,
) -> tuple[int, int, int, Path]:
    """Run to a pending round-1 clip gate; return run, approval, script approval, storage."""
    storage = tmp_path / "storage"
    async with session_factory() as session:
        evidence = await _after_a4(session, storage, key=key)
        # Only the review routes read these settings; the steps above ran with defaults.
        monkeypatch.setattr(
            a1_executor,
            "settings",
            a1_executor.settings.model_copy(
                update={
                    "storage_root": str(storage),
                    "content_scene_animation_backend": "ltx23_quality_test",
                    "content_scene_clip_review_enabled": True,
                }
            ),
        )
        await animate_round(
            session,
            evidence,
            storage,
            _Renderer(clock=_Clock()),
            SceneRoundPlan(round_number=1),
        )
        await request_scene_clip_approval(
            session,
            run=evidence.run,
            idempotency_key=evidence.run.idempotency_key,
            storage_root=storage,
            round_number=1,
        )
        gate = await session.scalar(
            select(Approval).where(
                Approval.run_id == evidence.run.id,
                Approval.action == SCENE_CLIPS_REVIEW_APPROVAL_ACTION,
            )
        )
        assert gate is not None and evidence.run.id is not None
        ids = (evidence.run.id, gate.id, evidence.approval.id)
        await session.commit()
    assert ids[1] is not None and ids[2] is not None
    return ids[0], ids[1], ids[2], storage


def _client(
    session_factory: async_sessionmaker[AsyncSession], published: list[int] | None = None
) -> AsyncClient:
    application = create_app(
        csrf_token="test-csrf-token",
        dispatch_publisher=lambda dispatch_id, delivery_id, queue: (
            published.append(dispatch_id) if published is not None else None
        ),
    )

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    return AsyncClient(transport=ASGITransport(app=application), base_url="http://127.0.0.1")


def _stub_requeue(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    requeued: list[int] = []

    async def fake_requeue(_session: AsyncSession, *, run_id: int, actor: str) -> object:
        requeued.append(run_id)
        return SimpleNamespace(should_publish=False, dispatch_id=None)

    # These runs were executed directly, without a durable dispatch to requeue.
    monkeypatch.setattr(dashboard_main, "requeue_completed_run_dispatch", fake_requeue)
    return requeued


async def test_page_shows_each_clip_with_its_excerpt_and_serves_only_verified_files(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, approval_id, script_approval_id, storage = await _open_gate(
        session_factory, tmp_path, monkeypatch, "dashboard-a5a-page"
    )
    async with session_factory() as session:
        review = await a1_executor.load_content_approval_review(
            session,
            approval=(await session.get(Approval, approval_id)),  # type: ignore[arg-type]
            storage_root=storage,
        )
        scenes = review.scenes  # type: ignore[union-attr]
        clip = scenes[0]
        still = scenes[2]
        clip_artifact, image_artifact = clip.clip_artifact_id, clip.image_artifact_id
        clip_bytes = clip.clip_path.read_bytes()  # type: ignore[union-attr]
        clip_file = clip.clip_path
        motion = clip.motion
        narration = clip.narration_text
        assert still.clip_artifact_id is None

    async with _client(session_factory) as client:
        home = await client.get("/")
        page = await client.get(f"/approvals/{approval_id}/review")
        video = await client.get(f"/approvals/{approval_id}/scene-clips/{clip_artifact}")
        ranged = await client.get(
            f"/approvals/{approval_id}/scene-clips/{clip_artifact}",
            headers={"Range": "bytes=2-5"},
        )
        image = await client.get(f"/approvals/{approval_id}/scene-clip-images/{image_artifact}")
        not_a_clip = await client.get(f"/approvals/{approval_id}/scene-clips/{image_artifact}")
        wrong_gate = await client.get(
            f"/approvals/{script_approval_id}/scene-clips/{clip_artifact}"
        )
        missing = await client.get(f"/approvals/999999/scene-clips/{clip_artifact}")

        assert home.status_code == 200
        _assert_review_link(home.text, approval_id, SCENE_CLIPS_REVIEW_APPROVAL_ACTION)
        assert page.status_code == 200
        assert page.headers["cache-control"] == "no-store"
        assert "Revisão humana dos clipes animados" in page.text
        assert f"/approvals/{approval_id}/scene-clips/{clip_artifact}" in page.text
        assert f"/approvals/{approval_id}/scene-clip-images/{image_artifact}" in page.text
        assert motion in page.text and narration.split(".")[0] in page.text
        assert "Cena longa demais" in page.text
        assert "scene-clip-redo" in page.text and 'name="still"' in page.text
        assert str(storage) not in page.text and ".mp4" not in page.text
        assert video.status_code == 200
        assert video.headers["content-type"] == "video/mp4"
        assert video.headers["cache-control"] == "no-store"
        assert video.headers["x-content-type-options"] == "nosniff"
        assert video.headers["accept-ranges"] == "bytes"
        assert video.content == clip_bytes
        assert ranged.status_code == 206
        assert ranged.content == clip_bytes[2:6]
        assert image.status_code == 200 and image.headers["content-type"] == "image/png"
        assert image.headers["x-content-type-options"] == "nosniff"
        assert not_a_clip.status_code == 404
        assert wrong_gate.status_code == 404
        assert missing.status_code == 404

        # A changed clip is refused instead of being shown or served.
        assert clip_file is not None
        clip_file.write_bytes(clip_bytes + b"tampered")
        tampered_page = await client.get(f"/approvals/{approval_id}/review")
        tampered_video = await client.get(
            f"/approvals/{approval_id}/scene-clips/{clip_artifact}"
        )
        assert tampered_page.status_code == 409
        assert tampered_video.status_code == 409
        clip_file.write_bytes(clip_bytes)


async def test_operator_approves_and_the_run_continues(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, approval_id, _script, _storage = await _open_gate(
        session_factory, tmp_path, monkeypatch, "dashboard-a5a-approve"
    )
    requeued = _stub_requeue(monkeypatch)

    async with _client(session_factory) as client:
        forged = await client.post(
            f"/approvals/{approval_id}/approve",
            data={"csrf_token": "x", "confirmation": "approve-approval"},
        )
        approved = await client.post(
            f"/approvals/{approval_id}/approve",
            data={"csrf_token": "test-csrf-token", "confirmation": "approve-approval"},
        )
        after = await client.get(f"/approvals/{approval_id}/review")

    assert forged.status_code == 403
    assert approved.status_code == 303 and requeued == [run_id]
    assert "somente leitura" in after.text
    async with session_factory() as session:
        decided = await session.get(Approval, approval_id)
        assert decided is not None and decided.status == ApprovalStatus.APPROVED.value
        assert decided.decision_payload is None


async def test_operator_rejects_and_the_run_is_cancelled(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, approval_id, _script, _storage = await _open_gate(
        session_factory, tmp_path, monkeypatch, "dashboard-a5a-reject"
    )
    published: list[int] = []

    async with _client(session_factory, published) as client:
        rejected = await client.post(
            f"/approvals/{approval_id}/reject",
            data={"csrf_token": "test-csrf-token", "confirmation": "reject-approval"},
        )

    assert rejected.status_code in {302, 303} and published == []
    async with session_factory() as session:
        finished = await session.get(Run, run_id)
        assert finished is not None and finished.status == RunStatus.CANCELLED.value


async def test_operator_sends_clips_back_with_notes_and_keeps_a_scene_still(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, approval_id, _script, _storage = await _open_gate(
        session_factory, tmp_path, monkeypatch, "dashboard-a5a-redo"
    )
    requeued = _stub_requeue(monkeypatch)
    form = {
        "csrf_token": "test-csrf-token",
        "confirmation": "redo-scene-clips",
        "redo": "2",
        "note_2": "A câmera deve ficar parada",
        "note_1": "não marcado, ignorado",
        "still": "1",
    }

    async with _client(session_factory) as client:
        page = await client.get(f"/approvals/{approval_id}/review")
        forged = await client.post(
            f"/approvals/{approval_id}/scene-clip-redo", data={**form, "csrf_token": "x"}
        )
        unconfirmed = await client.post(
            f"/approvals/{approval_id}/scene-clip-redo", data={**form, "confirmation": "approve"}
        )
        nothing = await client.post(
            f"/approvals/{approval_id}/scene-clip-redo",
            data={key: value for key, value in form.items() if key not in {"redo", "still"}},
        )
        unknown_field = await client.post(
            f"/approvals/{approval_id}/scene-clip-redo", data={**form, "topic": "x"}
        )
        everything_still = await client.post(
            f"/approvals/{approval_id}/scene-clip-redo",
            data={
                "csrf_token": "test-csrf-token",
                "confirmation": "redo-scene-clips",
                "still": ["1", "2"],
            },
        )
        too_long_scene = await client.post(
            f"/approvals/{approval_id}/scene-clip-redo",
            data={**form, "redo": "3", "still": "1"},
        )
        sent = await client.post(f"/approvals/{approval_id}/scene-clip-redo", data=form)
        again = await client.post(f"/approvals/{approval_id}/scene-clip-redo", data=form)
        after = await client.get(f"/approvals/{approval_id}/review")

    assert "Aplicar marcações" in page.text
    assert 'name="note_2"' in page.text and 'form="form-redo"' in page.text
    assert forged.status_code == 403
    assert unconfirmed.status_code == 400
    assert nothing.status_code == 400 and unknown_field.status_code == 400
    assert everything_still.status_code == 400 and too_long_scene.status_code == 400
    assert sent.status_code == 303 and sent.headers["location"] == f"/runs/{run_id}"
    assert again.status_code == 409
    assert requeued == [run_id]
    assert "Ajustes pedidos nesta rodada" in after.text
    assert "Cena 2: A câmera deve ficar parada" in after.text
    assert "Cena 1: ficar com a imagem e zoom" in after.text
    assert "Aplicar marcações" not in after.text
    async with session_factory() as session:
        decided = await session.get(Approval, approval_id)
        assert decided is not None and decided.status == ApprovalStatus.APPROVED.value
        assert decided.decision_payload == {
            "choice": "redo",
            "note_2": "A câmera deve ficar parada",
            "still_1": True,
        }
