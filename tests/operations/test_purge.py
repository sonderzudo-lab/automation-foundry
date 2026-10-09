"""Approved storage purge: gating, evidence re-verification, and deletion."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.config import Settings
from src.core.database import Base
from src.operations import purge as purge_module
from src.operations.backup import BackupResult
from src.operations.purge import (
    PURGE_APPROVAL_ACTION,
    PurgeConfigurationError,
    PurgeDisabledError,
    PurgeError,
    PurgeEvidenceError,
    execute_approved_purge,
    plan_retention_purge,
)
from src.operations.retention import RetentionClass, collect_retention_inventory
from src.platform.approval_service import decide_approval
from src.platform.artifact_service import register_local_artifact
from src.platform.models import (
    Approval,
    ApprovalStatus,
    Artifact,
    MetricPoint,
    QueueClass,
    Run,
    RunStatus,
)
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


@pytest.fixture(autouse=True)
def stopped_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(purge_module, "runtime_mutex_is_owned", lambda: False)


@pytest.fixture
def fake_backup(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    created: list[str] = []

    def create(configuration: Settings, **_kwargs: object) -> BackupResult:
        created.append(configuration.storage_root)
        return BackupResult(
            backup_id="20260811T120000Z-abcd1234",
            bundle_path=Path(configuration.storage_root) / "backups" / "stub",
            database_format="sqlite3",
            artifact_count=1,
            total_bytes=1,
        )

    monkeypatch.setattr(purge_module, "create_backup", create)
    return created


def _configuration(storage_root: Path, *, purge_enabled: bool = True) -> Settings:
    return Settings(
        _env_file=None,
        storage_root=str(storage_root),
        retention_purge_enabled=purge_enabled,
    )


async def _expired_artifact(
    session: AsyncSession,
    *,
    storage_root: Path,
    relative_path: str,
    content: bytes,
    key: str,
    slug: str = "content-engine",
) -> Artifact:
    automation = (
        await get_or_create_automation(
            session,
            slug=slug,
            name="Content Engine",
            owner="local-owner",
        )
    ).automation
    run = (
        await get_or_create_run(session, automation=automation, idempotency_key=key)
    ).run
    await transition_run(session, run, RunStatus.RUNNING)
    step = (
        await get_or_create_step_run(
            session,
            run=run,
            name="produce",
            queue=QueueClass.IO,
            ordinal=1,
            idempotency_key=f"{key}:produce",
        )
    ).step_run
    target = storage_root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    artifact = (
        await register_local_artifact(
            session,
            run=run,
            step_run=step,
            storage_root=storage_root,
            file_path=Path(relative_path),
            idempotency_key=f"{key}:artifact",
            artifact_type="script",
            media_type="application/json",
            origin="content-engine",
            retention_days=30,
        )
    ).artifact
    artifact.retention_until = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1)
    await transition_run(session, run, RunStatus.SUCCEEDED)
    await session.flush()
    return artifact


async def _approve(session: AsyncSession, approval_id: int) -> None:
    approval = await session.get(Approval, approval_id)
    assert approval is not None
    await decide_approval(
        session,
        approval=approval,
        decision=ApprovalStatus.APPROVED,
        actor="local-owner",
        reason="reviewed the expired list",
    )


async def test_purge_is_disabled_by_default(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    default_settings = Settings(_env_file=None, storage_root=str(storage_root))

    assert default_settings.retention_purge_enabled is False
    with pytest.raises(PurgeDisabledError):
        await plan_retention_purge(
            session,
            actor="local-owner",
            reason="cleanup",
            configuration=default_settings,
        )
    with pytest.raises(PurgeDisabledError):
        await execute_approved_purge(
            session,
            approval_id=1,
            confirmation="PURGE x",
            actor="local-owner",
            configuration=default_settings,
        )


async def test_plan_opens_a_hash_bound_approval_without_deleting(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    content = b'{"expired":"content"}\n'
    artifact = await _expired_artifact(
        session,
        storage_root=storage_root,
        relative_path="content-engine/1/script.json",
        content=content,
        key="purge-plan-1",
    )
    configuration = _configuration(storage_root)

    plan = await plan_retention_purge(
        session,
        actor="local-owner",
        reason="storage cleanup",
        configuration=configuration,
    )

    assert plan.entry_count == 1
    assert plan.total_bytes == len(content)
    assert plan.requires_decision is True
    assert plan.created is True
    assert plan.entries[0].artifact_id == artifact.id
    assert plan.entries[0].sha256 == hashlib.sha256(content).hexdigest()

    approval = await session.get(Approval, plan.approval_id)
    assert approval is not None
    assert approval.action == PURGE_APPROVAL_ACTION
    assert ApprovalStatus(approval.status) is ApprovalStatus.PENDING
    assert approval.review_payload is not None
    projection = " ".join(approval.review_payload.values())
    assert "content-engine/1/script.json" not in projection
    assert "irrever" in projection.casefold()

    run = await session.get(Run, plan.run_id)
    assert run is not None
    assert RunStatus(run.status) is RunStatus.AWAITING_APPROVAL

    # Nothing was deleted by planning.
    assert (storage_root / "content-engine/1/script.json").read_bytes() == content
    refreshed = await session.get(Artifact, artifact.id)
    assert refreshed is not None
    assert refreshed.purged_at is None


async def test_plan_is_idempotent_for_the_same_expired_set(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    await _expired_artifact(
        session,
        storage_root=storage_root,
        relative_path="content-engine/1/script.json",
        content=b"same plan\n",
        key="purge-plan-idem",
    )
    configuration = _configuration(storage_root)

    first = await plan_retention_purge(
        session,
        actor="local-owner",
        reason="cleanup",
        configuration=configuration,
    )
    second = await plan_retention_purge(
        session,
        actor="local-owner",
        reason="cleanup",
        configuration=configuration,
    )

    assert second.plan_digest == first.plan_digest
    assert second.approval_id == first.approval_id
    assert second.created is False
    approvals = (await session.scalars(select(Approval))).all()
    assert len(approvals) == 1


async def test_plan_without_expired_artifacts_creates_no_gate(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    storage_root.mkdir()

    plan = await plan_retention_purge(
        session,
        actor="local-owner",
        reason="cleanup",
        configuration=_configuration(storage_root),
    )

    assert plan.entry_count == 0
    assert plan.approval_id is None
    assert plan.requires_decision is False
    assert (await session.scalars(select(Approval))).all() == []


async def test_execute_deletes_only_the_approved_files(
    session: AsyncSession,
    tmp_path: Path,
    fake_backup: list[str],
) -> None:
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    expired_content = b'{"expired":"yes"}\n'
    artifact = await _expired_artifact(
        session,
        storage_root=storage_root,
        relative_path="content-engine/1/script.json",
        content=expired_content,
        key="purge-exec-1",
    )
    keeper = storage_root / "content-engine" / "1" / "keep.json"
    keeper.write_bytes(b'{"retained":"yes"}\n')
    configuration = _configuration(storage_root)

    plan = await plan_retention_purge(
        session,
        actor="local-owner",
        reason="cleanup",
        configuration=configuration,
    )
    assert plan.approval_id is not None
    await _approve(session, plan.approval_id)

    result = await execute_approved_purge(
        session,
        approval_id=plan.approval_id,
        confirmation=f"PURGE {plan.plan_digest}",
        actor="local-owner",
        configuration=configuration,
    )

    assert result.deleted_file_count == 1
    assert result.deleted_bytes == len(expired_content)
    assert result.failed_entry_count == 0
    assert result.backup_id == "20260811T120000Z-abcd1234"
    assert fake_backup == [str(storage_root)]

    assert not (storage_root / "content-engine/1/script.json").exists()
    assert keeper.read_bytes() == b'{"retained":"yes"}\n'

    refreshed = await session.get(Artifact, artifact.id)
    assert refreshed is not None
    assert refreshed.purged_at is not None
    assert refreshed.purged_by_approval_id == plan.approval_id

    run = await session.get(Run, plan.run_id)
    assert run is not None
    assert RunStatus(run.status) is RunStatus.SUCCEEDED
    assert (run.output_payload or {})["purge"]["deleted_file_count"] == 1

    metrics = {point.name: point.value for point in (await session.scalars(select(MetricPoint))).all()}
    assert int(metrics["storage.purged_files"]) == 1
    assert int(metrics["storage.purged_bytes"]) == len(expired_content)


async def test_execute_refuses_without_an_approved_decision(
    session: AsyncSession,
    tmp_path: Path,
    fake_backup: list[str],
) -> None:
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    content = b"still pending\n"
    await _expired_artifact(
        session,
        storage_root=storage_root,
        relative_path="content-engine/1/script.json",
        content=content,
        key="purge-pending",
    )
    configuration = _configuration(storage_root)
    plan = await plan_retention_purge(
        session,
        actor="local-owner",
        reason="cleanup",
        configuration=configuration,
    )
    assert plan.approval_id is not None

    with pytest.raises(PurgeError):
        await execute_approved_purge(
            session,
            approval_id=plan.approval_id,
            confirmation=f"PURGE {plan.plan_digest}",
            actor="local-owner",
            configuration=configuration,
        )

    assert (storage_root / "content-engine/1/script.json").read_bytes() == content
    assert fake_backup == []


async def test_rejecting_the_gate_cancels_the_run_and_keeps_every_file(
    session: AsyncSession,
    tmp_path: Path,
    fake_backup: list[str],
) -> None:
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    content = b"operator said no\n"
    await _expired_artifact(
        session,
        storage_root=storage_root,
        relative_path="content-engine/1/script.json",
        content=content,
        key="purge-reject",
    )
    configuration = _configuration(storage_root)
    plan = await plan_retention_purge(
        session,
        actor="local-owner",
        reason="cleanup",
        configuration=configuration,
    )
    assert plan.approval_id is not None
    approval = await session.get(Approval, plan.approval_id)
    assert approval is not None

    await decide_approval(
        session,
        approval=approval,
        decision=ApprovalStatus.REJECTED,
        actor="local-owner",
        reason="keep the evidence for now",
    )

    run = await session.get(Run, plan.run_id)
    assert run is not None
    assert RunStatus(run.status) is RunStatus.CANCELLED
    with pytest.raises(PurgeError):
        await execute_approved_purge(
            session,
            approval_id=plan.approval_id,
            confirmation=f"PURGE {plan.plan_digest}",
            actor="local-owner",
            configuration=configuration,
        )
    assert (storage_root / "content-engine/1/script.json").read_bytes() == content
    assert fake_backup == []


async def test_execute_refuses_a_wrong_confirmation(
    session: AsyncSession,
    tmp_path: Path,
    fake_backup: list[str],
) -> None:
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    content = b"needs the exact digest\n"
    await _expired_artifact(
        session,
        storage_root=storage_root,
        relative_path="content-engine/1/script.json",
        content=content,
        key="purge-confirm",
    )
    configuration = _configuration(storage_root)
    plan = await plan_retention_purge(
        session,
        actor="local-owner",
        reason="cleanup",
        configuration=configuration,
    )
    assert plan.approval_id is not None
    await _approve(session, plan.approval_id)

    with pytest.raises(PurgeError):
        await execute_approved_purge(
            session,
            approval_id=plan.approval_id,
            confirmation="PURGE wrong-digest",
            actor="local-owner",
            configuration=configuration,
        )

    assert (storage_root / "content-engine/1/script.json").read_bytes() == content
    assert fake_backup == []


async def test_execute_aborts_before_deleting_when_a_file_changed(
    session: AsyncSession,
    tmp_path: Path,
    fake_backup: list[str],
) -> None:
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    await _expired_artifact(
        session,
        storage_root=storage_root,
        relative_path="content-engine/1/first.json",
        content=b'{"first":"aaaa"}\n',
        key="purge-tamper-1",
    )
    await _expired_artifact(
        session,
        storage_root=storage_root,
        relative_path="content-engine/2/second.json",
        content=b'{"second":"bbbb"}\n',
        key="purge-tamper-2",
        slug="content-engine",
    )
    configuration = _configuration(storage_root)
    plan = await plan_retention_purge(
        session,
        actor="local-owner",
        reason="cleanup",
        configuration=configuration,
    )
    assert plan.entry_count == 2
    assert plan.approval_id is not None
    await _approve(session, plan.approval_id)

    # Same byte length, different content: only a re-hash can catch this.
    tampered = storage_root / "content-engine" / "2" / "second.json"
    tampered.write_bytes(b'{"second":"cccc"}\n')

    with pytest.raises(PurgeEvidenceError):
        await execute_approved_purge(
            session,
            approval_id=plan.approval_id,
            confirmation=f"PURGE {plan.plan_digest}",
            actor="local-owner",
            configuration=configuration,
        )

    assert (storage_root / "content-engine/1/first.json").exists()
    assert tampered.exists()
    assert fake_backup == []


async def test_execute_requires_a_stopped_runtime(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_backup: list[str],
) -> None:
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    content = b"runtime is running\n"
    await _expired_artifact(
        session,
        storage_root=storage_root,
        relative_path="content-engine/1/script.json",
        content=content,
        key="purge-runtime",
    )
    configuration = _configuration(storage_root)
    plan = await plan_retention_purge(
        session,
        actor="local-owner",
        reason="cleanup",
        configuration=configuration,
    )
    assert plan.approval_id is not None
    await _approve(session, plan.approval_id)
    monkeypatch.setattr(purge_module, "runtime_mutex_is_owned", lambda: True)

    with pytest.raises(PurgeConfigurationError):
        await execute_approved_purge(
            session,
            approval_id=plan.approval_id,
            confirmation=f"PURGE {plan.plan_digest}",
            actor="local-owner",
            configuration=configuration,
        )

    assert (storage_root / "content-engine/1/script.json").read_bytes() == content
    assert fake_backup == []


async def test_execute_refuses_a_tampered_stored_plan(
    session: AsyncSession,
    tmp_path: Path,
    fake_backup: list[str],
) -> None:
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    protected = storage_root / "content-engine" / "9" / "protected.json"
    protected.parent.mkdir(parents=True)
    protected.write_bytes(b'{"protected":"never approved"}\n')
    await _expired_artifact(
        session,
        storage_root=storage_root,
        relative_path="content-engine/1/script.json",
        content=b"approved target\n",
        key="purge-tampered-plan",
    )
    configuration = _configuration(storage_root)
    plan = await plan_retention_purge(
        session,
        actor="local-owner",
        reason="cleanup",
        configuration=configuration,
    )
    assert plan.approval_id is not None
    await _approve(session, plan.approval_id)

    run = await session.get(Run, plan.run_id)
    assert run is not None
    payload = dict(run.input_payload)
    stored = dict(payload["plan"])
    stored["entries"] = [
        *stored["entries"],
        {
            "artifact_id": 999,
            "relative_path": "content-engine/9/protected.json",
            "sha256": "0" * 64,
            "size_bytes": 30,
        },
    ]
    payload["plan"] = stored
    run.input_payload = payload
    await session.flush()

    with pytest.raises(PurgeEvidenceError):
        await execute_approved_purge(
            session,
            approval_id=plan.approval_id,
            confirmation=f"PURGE {plan.plan_digest}",
            actor="local-owner",
            configuration=configuration,
        )

    assert protected.exists()
    assert (storage_root / "content-engine/1/script.json").exists()
    assert fake_backup == []


async def test_replaying_a_completed_purge_deletes_nothing_again(
    session: AsyncSession,
    tmp_path: Path,
    fake_backup: list[str],
) -> None:
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    await _expired_artifact(
        session,
        storage_root=storage_root,
        relative_path="content-engine/1/script.json",
        content=b"delete me once\n",
        key="purge-replay",
    )
    configuration = _configuration(storage_root)
    plan = await plan_retention_purge(
        session,
        actor="local-owner",
        reason="cleanup",
        configuration=configuration,
    )
    assert plan.approval_id is not None
    await _approve(session, plan.approval_id)
    first = await execute_approved_purge(
        session,
        approval_id=plan.approval_id,
        confirmation=f"PURGE {plan.plan_digest}",
        actor="local-owner",
        configuration=configuration,
    )

    second = await execute_approved_purge(
        session,
        approval_id=plan.approval_id,
        confirmation=f"PURGE {plan.plan_digest}",
        actor="local-owner",
        configuration=configuration,
    )

    assert first.replayed is False
    assert second.replayed is True
    assert second.deleted_file_count == first.deleted_file_count
    assert len(fake_backup) == 1


async def test_purged_records_are_reported_as_purged_not_missing(
    session: AsyncSession,
    tmp_path: Path,
    fake_backup: list[str],
) -> None:
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    await _expired_artifact(
        session,
        storage_root=storage_root,
        relative_path="content-engine/1/script.json",
        content=b"purged evidence\n",
        key="purge-inventory",
    )
    configuration = _configuration(storage_root)
    plan = await plan_retention_purge(
        session,
        actor="local-owner",
        reason="cleanup",
        configuration=configuration,
    )
    assert plan.approval_id is not None
    await _approve(session, plan.approval_id)
    await execute_approved_purge(
        session,
        approval_id=plan.approval_id,
        confirmation=f"PURGE {plan.plan_digest}",
        actor="local-owner",
        configuration=configuration,
    )

    inventory = await collect_retention_inventory(session, configuration=configuration)

    assert inventory.summary_for(RetentionClass.PURGED).file_count == 1
    assert inventory.summary_for(RetentionClass.MISSING).file_count == 0
    assert inventory.summary_for(RetentionClass.EXPIRED).file_count == 0

    # A file that reappears at a purged path is never silently accepted.
    reappeared = storage_root / "content-engine" / "1" / "script.json"
    reappeared.write_bytes(b"reappeared\n")
    after = await collect_retention_inventory(session, configuration=configuration)
    unsafe = [
        item
        for item in after.items
        if item.classification is RetentionClass.UNSAFE
    ]
    assert len(unsafe) == 1
    assert unsafe[0].reason == "purged_file_reappeared"
    assert after.summary_for(RetentionClass.ORPHAN).file_count == 0


async def test_purge_evidence_columns_must_be_set_together(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    artifact = await _expired_artifact(
        session,
        storage_root=storage_root,
        relative_path="content-engine/1/script.json",
        content=b"half evidence\n",
        key="purge-constraint",
    )

    artifact.purged_at = datetime.now(UTC).replace(tzinfo=None)
    with pytest.raises(IntegrityError):
        await session.flush()
