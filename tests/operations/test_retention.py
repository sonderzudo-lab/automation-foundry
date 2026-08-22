"""Read-only retention inventory: classification, containment, and fail-closed limits."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.config import Settings
from src.core.database import Base
from src.operations.retention import (
    RetentionClass,
    RetentionInventoryError,
    collect_retention_inventory,
)
from src.platform.approval_service import request_approval
from src.platform.artifact_service import register_local_artifact
from src.platform.models import Artifact, QueueClass, Run, RunStatus
from src.platform.run_service import (
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


def _configuration(storage_root: Path, **overrides: object) -> Settings:
    return Settings(storage_root=str(storage_root), **overrides)  # type: ignore[arg-type]


async def _run(session: AsyncSession, key: str, *, slug: str = "retention-tests") -> Run:
    automation = (
        await get_or_create_automation(
            session,
            slug=slug,
            name="Retention Tests",
            owner="local-owner",
        )
    ).automation
    run = (
        await get_or_create_run(session, automation=automation, idempotency_key=key)
    ).run
    await transition_run(session, run, RunStatus.RUNNING)
    return run


async def _register(
    session: AsyncSession,
    *,
    run: Run,
    storage_root: Path,
    relative_path: str,
    content: bytes = b"artifact bytes\n",
    retention_days: int | None = 30,
    key: str | None = None,
) -> Artifact:
    target = storage_root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_bytes(content)
    step = (
        await get_or_create_step_run(
            session,
            run=run,
            name=f"step-{relative_path}",
            queue=QueueClass.IO,
            ordinal=len(relative_path),
            idempotency_key=f"{run.idempotency_key}:{relative_path}",
        )
    ).step_run
    result = await register_local_artifact(
        session,
        run=run,
        step_run=step,
        storage_root=storage_root,
        file_path=Path(relative_path),
        idempotency_key=key or f"{run.idempotency_key}:{relative_path}",
        artifact_type="script",
        media_type="application/json",
        origin="retention-tests",
        retention_days=retention_days,
    )
    return result.artifact


async def _expire(session: AsyncSession, artifact: Artifact, *, days: int = 1) -> None:
    artifact.retention_until = datetime.now(UTC).replace(tzinfo=None) - timedelta(
        days=days
    )
    await session.flush()


async def test_inventory_classifies_retained_expired_orphan_and_missing(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    run = await _run(session, "inventory-1")
    await _register(
        session,
        run=run,
        storage_root=storage_root,
        relative_path="retention-tests/1/fresh.json",
    )
    expired = await _register(
        session,
        run=run,
        storage_root=storage_root,
        relative_path="retention-tests/1/old.json",
        content=b"older artifact bytes\n",
    )
    await _expire(session, expired)
    gone = await _register(
        session,
        run=run,
        storage_root=storage_root,
        relative_path="retention-tests/1/gone.json",
        content=b"deleted by operator\n",
    )
    (storage_root / "retention-tests/1/gone.json").unlink()
    orphan = storage_root / "retention-tests" / "1" / "unregistered.bin"
    orphan.write_bytes(b"never registered")
    await transition_run(session, run, RunStatus.SUCCEEDED)

    inventory = await collect_retention_inventory(
        session,
        configuration=_configuration(storage_root),
    )

    assert inventory.dry_run is True
    assert inventory.deleted_file_count == 0
    assert inventory.deleted_bytes == 0
    assert inventory.storage_root_present is True

    by_path = {item.relative_path: item for item in inventory.items}
    assert by_path["retention-tests/1/fresh.json"].classification is (
        RetentionClass.RETAINED
    )
    assert by_path["retention-tests/1/old.json"].classification is (
        RetentionClass.EXPIRED
    )
    assert by_path["retention-tests/1/old.json"].size_bytes == len(
        b"older artifact bytes\n"
    )
    assert by_path["retention-tests/1/gone.json"].classification is (
        RetentionClass.MISSING
    )
    assert by_path["retention-tests/1/gone.json"].artifact_id == gone.id
    assert by_path["retention-tests/1/unregistered.bin"].classification is (
        RetentionClass.ORPHAN
    )

    expired_summary = inventory.summary_for(RetentionClass.EXPIRED)
    assert expired_summary.file_count == 1
    assert expired_summary.total_bytes == len(b"older artifact bytes\n")
    orphan_summary = inventory.summary_for(RetentionClass.ORPHAN)
    assert orphan_summary.file_count == 1
    assert orphan_summary.total_bytes == len(b"never registered")

    assert inventory.scanned_file_count == 3
    assert [entry.automation_slug for entry in inventory.automations] == [
        "retention-tests"
    ]
    automation = inventory.automations[0]
    assert automation.expired_file_count == 1
    assert automation.missing_file_count == 1

    # Nothing on disk was touched by the inventory.
    assert (storage_root / "retention-tests/1/fresh.json").exists()
    assert (storage_root / "retention-tests/1/old.json").exists()
    assert orphan.exists()


async def test_open_run_and_pending_approval_never_expire(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    storage_root.mkdir()

    open_run = await _run(session, "inventory-open")
    open_artifact = await _register(
        session,
        run=open_run,
        storage_root=storage_root,
        relative_path="retention-tests/2/open.json",
    )
    await _expire(session, open_artifact)

    gated_run = await _run(session, "inventory-gated")
    gated_artifact = await _register(
        session,
        run=gated_run,
        storage_root=storage_root,
        relative_path="retention-tests/3/gated.json",
        content=b"awaiting a human decision\n",
    )
    await _expire(session, gated_artifact)
    await request_approval(
        session,
        run=gated_run,
        idempotency_key="gated:publish",
        action="publish",
        summary="Local review before anything leaves the machine.",
        input_payload={"artifact": "gated"},
        review_payload={"artifact": "gated"},
    )
    # The approval stays pending while the run itself reaches a terminal state,
    # so the hold must come from the open gate and not from the run status.
    await transition_run(session, gated_run, RunStatus.RUNNING)
    await transition_run(session, gated_run, RunStatus.SUCCEEDED)

    inventory = await collect_retention_inventory(
        session,
        configuration=_configuration(storage_root),
    )

    by_path = {item.relative_path: item for item in inventory.items}
    open_item = by_path["retention-tests/2/open.json"]
    assert open_item.classification is RetentionClass.RETENTION_HOLD
    assert open_item.reason == "open_run"
    gated_item = by_path["retention-tests/3/gated.json"]
    assert gated_item.classification is RetentionClass.RETENTION_HOLD
    assert gated_item.reason == "pending_approval"
    assert inventory.summary_for(RetentionClass.EXPIRED).file_count == 0


async def test_changed_evidence_and_shared_paths_are_held_not_expired(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    storage_root.mkdir()

    changed_run = await _run(session, "inventory-changed")
    changed = await _register(
        session,
        run=changed_run,
        storage_root=storage_root,
        relative_path="retention-tests/4/changed.json",
    )
    await _expire(session, changed)
    await transition_run(session, changed_run, RunStatus.SUCCEEDED)
    (storage_root / "retention-tests/4/changed.json").write_bytes(
        b"rewritten outside the platform\n"
    )

    first_run = await _run(session, "inventory-shared-1")
    second_run = await _run(session, "inventory-shared-2")
    shared_relative = "retention-tests/5/shared.json"
    shared_first = await _register(
        session,
        run=first_run,
        storage_root=storage_root,
        relative_path=shared_relative,
        content=b"one file, two records\n",
    )
    shared_second = await _register(
        session,
        run=second_run,
        storage_root=storage_root,
        relative_path=shared_relative,
        content=b"one file, two records\n",
    )
    await _expire(session, shared_first)
    await _expire(session, shared_second)
    await transition_run(session, first_run, RunStatus.SUCCEEDED)
    await transition_run(session, second_run, RunStatus.SUCCEEDED)

    inventory = await collect_retention_inventory(
        session,
        configuration=_configuration(storage_root),
    )

    items = {(item.artifact_id, item.relative_path): item for item in inventory.items}
    changed_item = items[(changed.id, "retention-tests/4/changed.json")]
    assert changed_item.classification is RetentionClass.RETENTION_HOLD
    assert changed_item.reason == "evidence_mismatch"
    assert changed_item.evidence_matches is False
    for artifact in (shared_first, shared_second):
        shared_item = items[(artifact.id, shared_relative)]
        assert shared_item.classification is RetentionClass.RETENTION_HOLD
        assert shared_item.reason == "shared_path"
    assert inventory.summary_for(RetentionClass.EXPIRED).file_count == 0
    # The shared file is counted once on disk and never becomes an orphan.
    assert inventory.summary_for(RetentionClass.ORPHAN).file_count == 0


async def test_registered_path_traversal_is_refused_as_unsafe(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"private file outside the storage root\n")

    run = await _run(session, "inventory-traversal")
    artifact = await _register(
        session,
        run=run,
        storage_root=storage_root,
        relative_path="retention-tests/6/inside.json",
    )
    # Simulate a legacy or tampered record that escapes the configured root.
    artifact.relative_path = "../outside.txt"
    artifact.retention_until = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1)
    await session.flush()

    inventory = await collect_retention_inventory(
        session,
        configuration=_configuration(storage_root),
    )

    unsafe = [
        item
        for item in inventory.items
        if item.classification is RetentionClass.UNSAFE and item.artifact_id is not None
    ]
    assert len(unsafe) == 1
    assert unsafe[0].reason == "registered_path_escapes_storage_root"
    assert ".." not in unsafe[0].relative_path
    assert "outside" not in unsafe[0].relative_path
    assert inventory.summary_for(RetentionClass.EXPIRED).file_count == 0
    assert outside.exists()


async def test_backups_and_runtime_are_excluded_from_orphans(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    (storage_root / "backups" / "20260101T000000Z-abcd1234").mkdir(parents=True)
    (storage_root / "backups" / "20260101T000000Z-abcd1234" / "manifest.json").write_bytes(
        b"{}\n"
    )
    (storage_root / "runtime").mkdir()
    (storage_root / "runtime" / "supervisor.log").write_bytes(b"local runtime state\n")
    (storage_root / ".gitkeep").write_bytes(b"")

    inventory = await collect_retention_inventory(
        session,
        configuration=_configuration(storage_root),
    )

    assert inventory.summary_for(RetentionClass.ORPHAN).file_count == 0
    assert inventory.scanned_file_count == 0
    assert inventory.excluded_file_count == 3
    assert inventory.excluded_bytes == len(b"{}\n") + len(b"local runtime state\n")


async def test_missing_storage_root_is_reported_without_creating_it(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"

    inventory = await collect_retention_inventory(
        session,
        configuration=_configuration(storage_root),
    )

    assert inventory.storage_root_present is False
    assert inventory.scanned_file_count == 0
    assert inventory.deleted_file_count == 0
    assert not storage_root.exists()


async def test_file_limit_fails_closed_instead_of_reporting_partial_state(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    (storage_root / "retention-tests").mkdir(parents=True)
    for index in range(3):
        (storage_root / "retention-tests" / f"file-{index}.bin").write_bytes(b"x")

    with pytest.raises(RetentionInventoryError):
        await collect_retention_inventory(
            session,
            configuration=_configuration(storage_root, retention_inventory_max_files=2),
        )


async def test_drive_root_is_never_inventoried(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    with pytest.raises(RetentionInventoryError):
        await collect_retention_inventory(
            session,
            configuration=_configuration(Path(tmp_path.anchor)),
        )


async def test_symlinked_storage_root_is_refused(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    real_root = tmp_path / "storage"
    real_root.mkdir()
    link = tmp_path / "linked-storage"
    try:
        link.symlink_to(real_root, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not permitted in this environment")

    with pytest.raises(RetentionInventoryError):
        await collect_retention_inventory(
            session,
            configuration=_configuration(link),
        )


async def test_symlinked_file_below_storage_is_never_an_orphan(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    (storage_root / "retention-tests").mkdir(parents=True)
    outside = tmp_path / "secret.bin"
    outside.write_bytes(b"private bytes outside the storage root\n")
    link = storage_root / "retention-tests" / "link.bin"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is not permitted in this environment")

    inventory = await collect_retention_inventory(
        session,
        configuration=_configuration(storage_root),
    )

    assert inventory.summary_for(RetentionClass.ORPHAN).file_count == 0
    unsafe = inventory.summary_for(RetentionClass.UNSAFE)
    assert unsafe.file_count == 1
    assert unsafe.total_bytes == 0
    assert outside.read_bytes() == b"private bytes outside the storage root\n"


async def test_artifacts_without_retention_policy_are_always_retained(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    run = await _run(session, "inventory-no-policy")
    await _register(
        session,
        run=run,
        storage_root=storage_root,
        relative_path="retention-tests/7/forever.json",
        retention_days=None,
    )
    await transition_run(session, run, RunStatus.SUCCEEDED)

    inventory = await collect_retention_inventory(
        session,
        configuration=_configuration(storage_root),
    )

    item = inventory.items[0]
    assert item.classification is RetentionClass.RETAINED
    assert item.reason == "no_retention_policy"
    assert item.retention_until is None


async def test_inventory_leaves_every_byte_untouched(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    run = await _run(session, "inventory-immutable")
    expired = await _register(
        session,
        run=run,
        storage_root=storage_root,
        relative_path="retention-tests/8/expired.json",
    )
    await _expire(session, expired)
    await transition_run(session, run, RunStatus.SUCCEEDED)
    (storage_root / "retention-tests" / "8" / "orphan.bin").write_bytes(b"orphan bytes")

    before = {
        path.relative_to(storage_root).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(storage_root.rglob("*"))
        if path.is_file()
    }

    inventory = await collect_retention_inventory(
        session,
        configuration=_configuration(storage_root),
    )

    after = {
        path.relative_to(storage_root).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(storage_root.rglob("*"))
        if path.is_file()
    }
    assert before == after
    assert inventory.summary_for(RetentionClass.EXPIRED).file_count == 1
    assert inventory.deleted_file_count == 0
