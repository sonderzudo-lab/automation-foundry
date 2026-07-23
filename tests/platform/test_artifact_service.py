"""Persistence, integrity, and path-safety tests for local artifacts."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from src.core.database import Base
from src.platform.artifact_service import (
    ArtifactStorageError,
    register_local_artifact,
)
from src.platform.models import (
    Artifact,
    ArtifactSensitivity,
    QueueClass,
    Run,
    RunStatus,
)
from src.platform.run_service import (
    IdempotencyConflictError,
    get_or_create_automation,
    get_or_create_run,
    transition_run,
)
from src.platform.step_service import get_or_create_step_run


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as database_session:
        yield database_session
    await engine.dispose()


async def _running_run(session: AsyncSession, key: str) -> Run:
    automation = (
        await get_or_create_automation(
            session,
            slug="artifact-tests",
            name="Artifact Tests",
            owner="local-owner",
        )
    ).automation
    run = (
        await get_or_create_run(
            session,
            automation=automation,
            idempotency_key=key,
        )
    ).run
    await transition_run(session, run, RunStatus.RUNNING)
    return run


async def test_registration_persists_verified_metadata_and_is_idempotent(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    artifact_path = storage_root / "artifact-tests" / "run-1" / "script.json"
    artifact_path.parent.mkdir(parents=True)
    content = b'{"title":"local only"}\n'
    artifact_path.write_bytes(content)
    run = await _running_run(session, "artifact-run-1")
    step = (
        await get_or_create_step_run(
            session,
            run=run,
            name="script",
            queue=QueueClass.IO,
            ordinal=1,
            idempotency_key="artifact-run-1:script",
        )
    ).step_run

    first = await register_local_artifact(
        session,
        run=run,
        step_run=step,
        storage_root=storage_root,
        file_path=artifact_path,
        idempotency_key="artifact-run-1:script-json",
        artifact_type="script",
        media_type="application/json",
        origin="content-engine",
        sensitivity=ArtifactSensitivity.CONFIDENTIAL,
        retention_days=30,
        expected_sha256=hashlib.sha256(content).hexdigest().upper(),
    )
    replay = await register_local_artifact(
        session,
        run=run,
        step_run=step,
        storage_root=storage_root,
        file_path=Path("artifact-tests/run-1/script.json"),
        idempotency_key="artifact-run-1:script-json",
        artifact_type="script",
        media_type="application/json",
        origin="content-engine",
        sensitivity=ArtifactSensitivity.CONFIDENTIAL,
        retention_days=30,
    )
    await session.commit()

    fetched = await session.scalar(
        select(Artifact)
        .where(Artifact.id == first.artifact.id)
        .options(selectinload(Artifact.run), selectinload(Artifact.step_run))
    )
    assert fetched is not None
    assert first.created is True
    assert replay.created is False
    assert replay.artifact.id == first.artifact.id
    assert fetched.relative_path == "artifact-tests/run-1/script.json"
    assert fetched.sha256 == hashlib.sha256(content).hexdigest()
    assert fetched.size_bytes == len(content)
    assert fetched.sensitivity == ArtifactSensitivity.CONFIDENTIAL.value
    assert fetched.retention_days == 30
    assert fetched.retention_until is not None
    assert fetched.retention_until > fetched.created_at
    assert fetched.run.id == run.id
    assert fetched.step_run is not None
    assert fetched.step_run.id == step.id


async def test_registration_rejects_changed_content_and_checksum_mismatch(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    artifact_path = storage_root / "result.txt"
    artifact_path.write_text("first", encoding="utf-8")
    run = await _running_run(session, "artifact-run-2")
    arguments = {
        "run": run,
        "storage_root": storage_root,
        "file_path": artifact_path,
        "idempotency_key": "artifact-run-2:result",
        "artifact_type": "result",
        "media_type": "text/plain",
        "origin": "test-worker",
    }
    await register_local_artifact(session, **arguments)

    artifact_path.write_text("changed", encoding="utf-8")
    with pytest.raises(IdempotencyConflictError, match="different metadata"):
        await register_local_artifact(session, **arguments)
    with pytest.raises(ArtifactStorageError, match="checksum"):
        await register_local_artifact(
            session,
            **{**arguments, "idempotency_key": "artifact-run-2:expected"},
            expected_sha256="0" * 64,
        )


async def test_registration_rejects_paths_outside_storage(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    outside_file = tmp_path / "private.txt"
    outside_file.write_text("must not be registered", encoding="utf-8")
    run = await _running_run(session, "artifact-run-3")
    arguments = {
        "run": run,
        "storage_root": storage_root,
        "idempotency_key": "artifact-run-3:private",
        "artifact_type": "private",
        "media_type": "text/plain",
        "origin": "test-worker",
    }

    with pytest.raises(ArtifactStorageError, match="below the configured"):
        await register_local_artifact(
            session,
            file_path=outside_file,
            **arguments,
        )
    with pytest.raises(ArtifactStorageError, match="unavailable"):
        await register_local_artifact(
            session,
            file_path=Path("missing.txt"),
            **arguments,
        )
    with pytest.raises(ArtifactStorageError, match="regular file"):
        await register_local_artifact(
            session,
            file_path=storage_root,
            **arguments,
        )


async def test_step_run_must_belong_to_artifact_run(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    (storage_root / "output.txt").write_text("output", encoding="utf-8")
    first_run = await _running_run(session, "artifact-run-4a")
    other_run = await _running_run(session, "artifact-run-4b")
    other_step = (
        await get_or_create_step_run(
            session,
            run=other_run,
            name="other",
            queue=QueueClass.CPU,
            ordinal=1,
            idempotency_key="artifact-run-4b:other",
        )
    ).step_run

    with pytest.raises(ValueError, match="belong"):
        await register_local_artifact(
            session,
            run=first_run,
            step_run=other_step,
            storage_root=storage_root,
            file_path=Path("output.txt"),
            idempotency_key="artifact-run-4a:output",
            artifact_type="output",
            media_type="text/plain",
            origin="test-worker",
        )
