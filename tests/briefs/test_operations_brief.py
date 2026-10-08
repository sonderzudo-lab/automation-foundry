"""Operations brief: redacted evidence, deterministic rendering and the approval gate."""

from __future__ import annotations

import errno
from collections.abc import AsyncGenerator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.briefs import executor
from src.briefs.evidence import build_window, collect_operations_evidence
from src.briefs.executor import (
    OperationsBriefRunNotRunnableError,
    execute_operations_brief_run,
    finalize_operations_brief_approval,
    load_operations_brief_review,
    parse_operations_brief_form,
    prepare_operations_brief_run,
)
from src.briefs.render import OperationsBriefRenderError, render_operations_brief
from src.core.database import Base
from src.platform.approval_service import decide_approval, request_approval
from src.platform.control_service import request_run_cancellation
from src.platform.ledger_service import record_ledger_entry
from src.platform.models import (
    Approval,
    ApprovalStatus,
    Artifact,
    Automation,
    ConnectorObservation,
    LedgerEntry,
    LedgerEntryType,
    MetricPoint,
    PlatformAlert,
    Run,
    RunStatus,
    StepRun,
)
from src.platform.run_service import get_or_create_automation, get_or_create_run, transition_run

NOW = datetime(2026, 10, 8, 12, 0, 0)
TODAY = date(2026, 10, 8)
SECRET = "private-marker-must-never-reach-the-brief"


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as database_session:
        yield database_session
    await engine.dispose()


def _payload(days: int = 7) -> dict[str, object]:
    return {"window_days": days, "window_end": TODAY.isoformat()}


async def _seed_platform_activity(session: AsyncSession) -> None:
    """Create runs, a pending approval, an alert and ledger entries inside the window."""
    automation = (
        await get_or_create_automation(
            session, slug="content-engine", name="Content Engine", owner="local-operator"
        )
    ).automation
    queued = NOW - timedelta(days=2)

    async def make_run(key: str, final: RunStatus, error: dict[str, object] | None) -> Run:
        run = (
            await get_or_create_run(
                session,
                automation=automation,
                idempotency_key=key,
                trigger="manual",
                input_payload={"topic": SECRET},
            )
        ).run
        await transition_run(session, run, RunStatus.RUNNING, note="seed")
        await transition_run(
            session,
            run,
            final,
            error=error,
            output_payload={"private": SECRET} if final is RunStatus.SUCCEEDED else None,
            note="seed",
        )
        run.queued_at = queued
        run.started_at = queued + timedelta(seconds=1)
        run.finished_at = queued + timedelta(seconds=11)
        return run

    await make_run("seed-ok", RunStatus.SUCCEEDED, None)
    failed = await make_run(
        "seed-failed",
        RunStatus.FAILED,
        {"code": "CONTENT_LLM_UNAVAILABLE", "message": SECRET, "retryable": True},
    )
    waiting = (
        await get_or_create_run(
            session,
            automation=automation,
            idempotency_key="seed-waiting",
            trigger="manual",
            input_payload={},
        )
    ).run
    await transition_run(session, waiting, RunStatus.RUNNING, note="seed")
    approval = (
        await request_approval(
            session,
            run=waiting,
            idempotency_key="seed-waiting:review",
            action="review_content_script_a1",
            summary="Revisar",
            input_payload={"n": 1},
            review_payload={"topic": SECRET},
        )
    ).approval
    approval.requested_at = NOW - timedelta(days=3)
    waiting.queued_at = queued
    session.add(
        PlatformAlert(
            automation_id=automation.id,
            run_id=failed.id,
            deduplication_key="seed-alert",
            title=SECRET,
            summary=SECRET,
            severity="error",
            status="open",
            source=SECRET,
            occurrence_count=1,
            first_seen_at=queued,
            last_seen_at=queued,
        )
    )
    await record_ledger_entry(
        session,
        automation=automation,
        run=failed,
        idempotency_key="seed-ledger-cost",
        entry_type=LedgerEntryType.COST,
        category=SECRET,
        amount=Decimal("0.2500000000"),
        currency="BRL",
        source=SECRET,
        confidence=1,
        observed_at=queued.replace(tzinfo=UTC),
    )
    await session.commit()


def test_form_is_bounded_and_ends_the_window_today() -> None:
    parsed = parse_operations_brief_form({"window_days": "14"}, today=TODAY)

    assert parsed == {"window_days": 14, "window_end": "2026-10-08"}
    for bad in ({"window_days": "8"}, {"window_days": "abc"}, {}, {"window_days": "7", "x": "1"}):
        with pytest.raises(ValueError):
            parse_operations_brief_form(bad, today=TODAY)


async def test_input_rejects_unknown_window(session: AsyncSession) -> None:
    with pytest.raises(ValidationError):
        await prepare_operations_brief_run(
            session, idempotency_key="bad-window", input_payload=_payload(days=3)
        )


async def test_empty_window_still_produces_an_honest_brief(session: AsyncSession) -> None:
    evidence = await collect_operations_evidence(
        session,
        window=build_window(window_end=TODAY, window_days=7),
        collected_at=NOW,
        exclude_run_id=None,
    )
    markdown = render_operations_brief(evidence)

    assert "Não houve atividade de runs nesta janela." in markdown
    assert "Nenhuma approval pendente." in markdown
    assert render_operations_brief(evidence) == markdown


def test_renderer_refuses_unknown_schema() -> None:
    with pytest.raises(OperationsBriefRenderError):
        render_operations_brief({"schema_version": 99})


async def test_evidence_counts_state_and_never_leaks_private_text(session: AsyncSession) -> None:
    await _seed_platform_activity(session)
    evidence = await collect_operations_evidence(
        session,
        window=build_window(window_end=TODAY, window_days=7),
        collected_at=NOW,
        exclude_run_id=None,
    )
    markdown = render_operations_brief(evidence)

    assert evidence["runs"]["total"] == 3
    assert evidence["runs"]["failed_total"] == 1
    assert evidence["runs"]["recent_failures"][0]["error_code"] == "CONTENT_LLM_UNAVAILABLE"
    assert evidence["approvals"]["pending_count"] == 1
    assert evidence["approvals"]["pending"][0]["age_seconds"] == 3 * 86_400
    assert evidence["alerts"]["active_count"] == 1
    assert evidence["ledger"][0]["cost"] == "0.2500000000"
    assert "Taxa de sucesso" in markdown and "0.5000" in markdown
    assert SECRET not in markdown
    assert SECRET not in str(evidence)


async def test_full_run_produces_verified_documents_and_waits_for_approval(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    await _seed_platform_activity(session)
    result = await execute_operations_brief_run(
        session,
        idempotency_key="brief-1",
        input_payload=_payload(),
        storage_root=tmp_path / "storage",
        clock=lambda: NOW,
    )

    assert result.run_status == RunStatus.AWAITING_APPROVAL.value
    assert result.created and not result.replayed
    artifacts = list(
        (await session.scalars(select(Artifact).where(Artifact.run_id == result.run_id))).all()
    )
    assert {item.artifact_type for item in artifacts} == {
        "operations_brief",
        "operations_brief_evidence",
    }
    assert all(item.sensitivity == "internal" for item in artifacts)
    assert all(item.retention_until is not None for item in artifacts)
    metrics = {
        point.name: point.value
        for point in (
            await session.scalars(select(MetricPoint).where(MetricPoint.run_id == result.run_id))
        ).all()
    }
    assert metrics["operations_brief_runs_observed"] == 3
    assert metrics["operations_brief_failed_runs"] == 1
    cost = await session.scalar(
        select(LedgerEntry).where(
            LedgerEntry.run_id == result.run_id,
            LedgerEntry.entry_type == LedgerEntryType.COST.value,
        )
    )
    assert cost is not None and cost.amount == 0 and cost.currency == "BRL"
    observation = await session.scalar(select(ConnectorObservation))
    assert observation is not None
    assert observation.connector_key == "platform-database"
    assert observation.status == "healthy"
    assert observation.quality_status == "pass"
    approval = await session.get(Approval, result.approval_id)
    assert approval is not None and approval.status == ApprovalStatus.PENDING.value
    assert set(approval.review_payload) == {
        "window",
        "runs_observed",
        "failed_runs",
        "pending_approvals",
        "active_alerts",
        "artifact",
        "sha256",
    }

    review = await load_operations_brief_review(
        session, approval=approval, storage_root=tmp_path / "storage"
    )
    assert review.window_days == 7
    assert "Brief operacional" in review.markdown
    assert SECRET not in review.markdown


async def test_approval_completes_the_run_once_and_replays_are_idempotent(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage = tmp_path / "storage"
    first = await execute_operations_brief_run(
        session,
        idempotency_key="brief-2",
        input_payload=_payload(),
        storage_root=storage,
        clock=lambda: NOW,
    )
    replay = await execute_operations_brief_run(
        session,
        idempotency_key="brief-2",
        input_payload=_payload(),
        storage_root=storage,
        clock=lambda: NOW,
    )
    assert replay.replayed and replay.approval_id == first.approval_id
    assert replay.brief_artifact_id == first.brief_artifact_id

    approval = await session.get(Approval, first.approval_id)
    assert approval is not None
    await decide_approval(
        session,
        approval=approval,
        decision=ApprovalStatus.APPROVED,
        actor="local-owner",
        reason="brief revisado",
    )
    assert not await finalize_operations_brief_approval(
        session, approval=approval, storage_root=storage
    )
    assert not await finalize_operations_brief_approval(
        session, approval=approval, storage_root=storage
    )

    run = await session.get(Run, first.run_id)
    assert run is not None and run.status == RunStatus.SUCCEEDED.value
    assert run.output_payload == {
        "step_run_id": first.step_run_id,
        "evidence_artifact_id": first.evidence_artifact_id,
        "brief_artifact_id": first.brief_artifact_id,
        "approval_id": first.approval_id,
    }
    steps = list((await session.scalars(select(StepRun).where(StepRun.run_id == run.id))).all())
    assert len(steps) == 1


async def test_rejection_cancels_without_deleting_evidence(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage = tmp_path / "storage"
    result = await execute_operations_brief_run(
        session,
        idempotency_key="brief-3",
        input_payload=_payload(),
        storage_root=storage,
        clock=lambda: NOW,
    )
    approval = await session.get(Approval, result.approval_id)
    assert approval is not None
    await decide_approval(
        session,
        approval=approval,
        decision=ApprovalStatus.REJECTED,
        actor="local-owner",
        reason="números não conferem",
    )

    run = await session.get(Run, result.run_id)
    assert run is not None and run.status == RunStatus.CANCELLED.value
    with pytest.raises(OperationsBriefRunNotRunnableError, match="not approved"):
        await finalize_operations_brief_approval(session, approval=approval, storage_root=storage)
    assert len(list(storage.rglob("brief.md"))) == 1


async def test_tampered_brief_blocks_review_and_finalization(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage = tmp_path / "storage"
    result = await execute_operations_brief_run(
        session,
        idempotency_key="brief-4",
        input_payload=_payload(),
        storage_root=storage,
        clock=lambda: NOW,
    )
    approval = await session.get(Approval, result.approval_id)
    assert approval is not None
    brief = next(storage.rglob("brief.md"))
    brief.write_text(brief.read_text(encoding="utf-8") + "\nlinha injetada\n", encoding="utf-8")

    with pytest.raises(Exception, match="checksum|size"):
        await load_operations_brief_review(session, approval=approval, storage_root=storage)
    run = await session.get(Run, result.run_id)
    assert run is not None and run.status == RunStatus.AWAITING_APPROVAL.value


async def test_disk_full_is_structured_and_leaves_no_artifact_or_approval(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def disk_full(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.ENOSPC, SECRET)

    monkeypatch.setattr(executor, "_write_atomic", disk_full)
    result = await execute_operations_brief_run(
        session,
        idempotency_key="brief-5",
        input_payload=_payload(),
        storage_root=tmp_path / "storage",
        clock=lambda: NOW,
    )

    run = await session.get(Run, result.run_id)
    assert run is not None and run.status == RunStatus.FAILED.value
    assert run.error is not None and run.error["code"] == "BRIEF_STORAGE_WRITE_FAILED"
    assert run.error["retryable"] is True
    assert SECRET not in str(run.error)
    assert list((await session.scalars(select(Artifact))).all()) == []
    assert list((await session.scalars(select(Approval))).all()) == []
    with pytest.raises(OperationsBriefRunNotRunnableError, match="successor retry"):
        await execute_operations_brief_run(
            session,
            idempotency_key="brief-5",
            input_payload=_payload(),
            storage_root=tmp_path / "storage",
            clock=lambda: NOW,
        )


async def test_evidence_persistence_failure_closes_the_run_as_retryable(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_registration(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError(SECRET)

    monkeypatch.setattr(executor, "register_local_artifact", fail_registration)
    result = await execute_operations_brief_run(
        session,
        idempotency_key="brief-6",
        input_payload=_payload(),
        storage_root=tmp_path / "storage",
        clock=lambda: NOW,
    )

    run = await session.get(Run, result.run_id)
    assert run is not None and run.status == RunStatus.FAILED.value
    assert run.error == {
        "code": "BRIEF_EVIDENCE_PERSIST_FAILED",
        "exception_type": "RuntimeError",
        "retryable": True,
        "timed_out": False,
    }
    assert list((await session.scalars(select(Approval))).all()) == []
    assert list((await session.scalars(select(MetricPoint))).all()) == []


async def test_cancelled_before_start_never_reads_evidence(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = await prepare_operations_brief_run(
        session, idempotency_key="brief-7", input_payload=_payload()
    )
    await request_run_cancellation(
        session, run=prepared.run, reason="operador cancelou antes da coleta"
    )
    await session.commit()

    async def must_not_run(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("evidence must not be collected")

    monkeypatch.setattr(executor, "collect_operations_evidence", must_not_run)
    result = await execute_operations_brief_run(
        session,
        idempotency_key="brief-7",
        input_payload=_payload(),
        storage_root=tmp_path / "storage",
        clock=lambda: NOW,
    )

    assert result.run_status == RunStatus.CANCELLED.value
    assert not (tmp_path / "storage").exists()


async def test_the_brief_never_counts_its_own_running_run(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    result = await execute_operations_brief_run(
        session,
        idempotency_key="brief-8",
        input_payload=_payload(),
        storage_root=tmp_path / "storage",
        clock=lambda: NOW + timedelta(seconds=1),
    )
    run = await session.get(Run, result.run_id)
    automation = await session.get(Automation, result.automation_id)

    assert run is not None and automation is not None
    metric = await session.scalar(
        select(MetricPoint).where(
            MetricPoint.run_id == result.run_id,
            MetricPoint.name == "operations_brief_runs_observed",
        )
    )
    assert metric is not None and metric.value == 0
