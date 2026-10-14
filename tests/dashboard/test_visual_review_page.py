"""Dashboard page and image route of the A3 visual review gate."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.core.database import Base, get_session
from src.dashboard.main import create_app
from src.pipeline.a1_executor import CONTENT_SCRIPT_APPROVAL_ACTION
from src.pipeline.visual_review import VISUAL_REVIEW_APPROVAL_ACTION
from src.platform.models import Approval, ApprovalStatus, Artifact, Run, RunStatus
from tests.dashboard.test_dashboard import _assert_review_link
from tests.pipeline.test_visual_review import pending_approval, run_to_visual_gate
from tests.support.local_media_rehearsal import rehearsal_script


@pytest.fixture
async def session_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def test_dashboard_visual_review_shows_and_serves_only_verified_images(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with session_factory() as session:
        run, storage_root = await run_to_visual_gate(
            session, tmp_path, monkeypatch, idempotency_key="dashboard-content-a3-page"
        )
        visual_approval = await pending_approval(session, run, VISUAL_REVIEW_APPROVAL_ACTION)
        script_approval = await pending_approval(session, run, CONTENT_SCRIPT_APPROVAL_ACTION)
        images = list(
            (
                await session.scalars(
                    select(Artifact)
                    .where(Artifact.run_id == run.id, Artifact.artifact_type == "visual_image")
                    .order_by(Artifact.id)
                )
            ).all()
        )
        manifest = await session.scalar(
            select(Artifact).where(
                Artifact.run_id == run.id, Artifact.artifact_type == "visual_manifest"
            )
        )
        assert manifest is not None and images
        run_id, approval_id, script_approval_id = run.id, visual_approval.id, script_approval.id
        first_image_id, first_image_path = images[0].id, storage_root / images[0].relative_path
        manifest_id = manifest.id
        await session.commit()

    published: list[int] = []
    application = create_app(
        csrf_token="test-csrf-token",
        dispatch_publisher=lambda dispatch_id, delivery_id, queue: published.append(
            dispatch_id
        ),
    )

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        home = await client.get("/")
        review = await client.get(f"/approvals/{approval_id}/review")
        image = await client.get(f"/approvals/{approval_id}/visuals/{first_image_id}")
        not_an_image = await client.get(f"/approvals/{approval_id}/visuals/{manifest_id}")
        wrong_gate = await client.get(
            f"/approvals/{script_approval_id}/visuals/{first_image_id}"
        )
        missing = await client.get(f"/approvals/999999/visuals/{first_image_id}")

        assert home.status_code == 200
        _assert_review_link(home.text, approval_id, VISUAL_REVIEW_APPROVAL_ACTION)
        assert review.status_code == 200
        assert review.headers["cache-control"] == "no-store"
        assert "Revisão humana das imagens A3" in review.text
        assert "Teste de qualidade, não monetizável" in review.text
        assert "pessoa real reconhecível" in review.text
        assert rehearsal_script().narration.split(".")[0] in review.text
        assert f"/approvals/{approval_id}/visuals/{first_image_id}" in review.text
        assert "1080x1920" in review.text
        assert str(storage_root) not in review.text
        assert "visual-001.png" not in review.text
        assert image.status_code == 200
        assert image.headers["content-type"] == "image/png"
        assert image.headers["cache-control"] == "no-store"
        assert image.headers["x-content-type-options"] == "nosniff"
        assert image.content == first_image_path.read_bytes()
        assert not_an_image.status_code == 404
        assert wrong_gate.status_code == 404
        assert missing.status_code == 404

        # A changed image is refused instead of being shown or served.
        original = first_image_path.read_bytes()
        first_image_path.write_bytes(original + b"tampered")
        tampered_review = await client.get(f"/approvals/{approval_id}/review")
        tampered_image = await client.get(
            f"/approvals/{approval_id}/visuals/{first_image_id}"
        )
        assert tampered_review.status_code == 409
        assert tampered_image.status_code == 409
        first_image_path.write_bytes(original)

        rejected = await client.post(
            f"/approvals/{approval_id}/reject",
            data={"csrf_token": "test-csrf-token", "confirmation": "reject-approval"},
        )
        after = await client.get(f"/approvals/{approval_id}/review")

    assert rejected.status_code in {302, 303}
    assert "somente leitura" in after.text
    assert published == []
    async with session_factory() as session:
        decided = await session.get(Approval, approval_id)
        finished = await session.get(Run, run_id)
        assert decided is not None and decided.status == ApprovalStatus.REJECTED.value
        assert finished is not None and finished.status == RunStatus.CANCELLED.value
