"""Content Engine A6 originality gate tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.core.database import Base
from src.intelligence import similarity
from src.intelligence.similarity import (
    calculate_script_similarity,
    execute_similarity_gate_step,
)
from src.pipeline.assembly import execute_approved_assembly_step
from src.platform.models import (
    Alert,
    Approval,
    Artifact,
    LedgerEntry,
    MetricPoint,
    Run,
    RunStatus,
    StepRun,
)
from tests.pipeline.test_assembly import (
    _assembly_adapter,
    _running_after_a4,
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


def test_lexical_similarity_is_high_for_duplicates_and_low_for_unrelated_text() -> None:
    duplicate = calculate_script_similarity(
        "O sono consolida memórias durante a noite.",
        "O sono consolida memórias durante a noite.",
    )
    unrelated = calculate_script_similarity(
        "O sono consolida memórias durante a noite.",
        "Vulcões submarinos criam novas ilhas no oceano Pacífico.",
    )

    assert duplicate.score == Decimal("1.0000000000")
    assert unrelated.score < Decimal("0.25")


async def test_first_a6_run_passes_with_verified_report_and_observations(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    result = await _run_through_a6(
        session,
        storage_root,
        idempotency_key="content-a6-first",
    )
    run, step, report = result
    metrics = list(
        (
            await session.scalars(
                select(MetricPoint).where(MetricPoint.source == "content-engine:a6")
            )
        ).all()
    )
    ledger = list(
        (
            await session.scalars(
                select(LedgerEntry).where(LedgerEntry.source == "content-engine:a6")
            )
        ).all()
    )
    alerts = list(
        (
            await session.scalars(
                select(Alert).where(Alert.source == "content-engine:a6")
            )
        ).all()
    )

    assert run.status == RunStatus.SUCCEEDED.value
    assert step.queue == "cpu" and step.ordinal == 6
    assert report.artifact_type == "originality_report"
    assert report.media_type == "application/json"
    assert {metric.name for metric in metrics} == {
        "content_originality_max_similarity",
        "content_originality_mean_similarity",
        "content_originality_reference_count",
        "content_originality_evaluation_duration_seconds",
        "content_originality_energy_estimate_kwh",
    }
    assert len(ledger) == 1 and str(ledger[0].amount) == "0E-10"
    assert alerts == []
    assert run.output_payload is not None
    assert run.output_payload["originality_blocked"] is False
    assert run.output_payload["originality_report_artifact_id"] == report.id


async def test_duplicate_script_is_blocked_and_opens_warning(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    first_run, _, _ = await _run_through_a6(
        session,
        storage_root,
        idempotency_key="content-a6-reference",
    )
    assert first_run.status == RunStatus.SUCCEEDED.value

    second_run, second_step, report = await _run_through_a6(
        session,
        storage_root,
        idempotency_key="content-a6-duplicate",
    )
    alert = await session.scalar(
        select(Alert).where(
            Alert.run_id == second_run.id,
            Alert.source == "content-engine:a6",
        )
    )

    assert second_run.status == RunStatus.FAILED.value
    assert second_run.error is not None
    assert second_run.error["code"] == "CONTENT_ORIGINALITY_BLOCKED"
    assert second_run.error["max_similarity"] == "1.0000000000"
    assert second_step.status == RunStatus.SUCCEEDED.value
    assert report.artifact_type == "originality_report"
    assert alert is not None and alert.severity == "warning" and alert.status == "open"
    assert second_run.output_payload is None
    assert alert.metric_point_id is not None


async def test_tampered_historical_script_fails_a6_observably(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    reference_run, _, _ = await _run_through_a6(
        session,
        storage_root,
        idempotency_key="content-a6-tamper-reference",
    )
    reference_script = await session.scalar(
        select(Artifact).where(
            Artifact.run_id == reference_run.id,
            Artifact.artifact_type == "script_bundle",
        )
    )
    assert reference_script is not None
    (storage_root / reference_script.relative_path).write_text(
        "adulterado",
        encoding="utf-8",
    )

    run, approval, script, final_video, approval_input = await _running_after_a5(
        session,
        storage_root,
        idempotency_key="content-a6-tamper-current",
    )
    result = await execute_similarity_gate_step(
        session,
        run=run,
        script_approval=approval,
        approval_input_payload=approval_input,
        script_artifact=script,
        final_video_artifact=final_video,
        idempotency_key=run.idempotency_key,
        storage_root=storage_root,
        threshold=Decimal("0.85"),
        reference_limit=20,
        cpu_power_watts=150,
        timeout_seconds=5,
    )
    step = await session.get(StepRun, result.step_run_id)

    assert result.report_artifact_id is None
    assert step is not None and step.status == RunStatus.FAILED.value
    assert step.error is not None
    assert step.error["code"] == "CONTENT_ORIGINALITY_REFERENCE_INVALID"
    assert run.status == RunStatus.FAILED.value


async def test_a6_evidence_failure_fails_run_without_partial_database_evidence(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_root = tmp_path / "storage"
    run, approval, script, final_video, approval_input = await _running_after_a5(
        session,
        storage_root,
        idempotency_key="content-a6-evidence-failure",
    )

    async def fail_registration(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(similarity, "register_local_artifact", fail_registration)
    result = await execute_similarity_gate_step(
        session,
        run=run,
        script_approval=approval,
        approval_input_payload=approval_input,
        script_artifact=script,
        final_video_artifact=final_video,
        idempotency_key=run.idempotency_key,
        storage_root=storage_root,
        threshold=Decimal("0.85"),
        reference_limit=20,
        cpu_power_watts=150,
        timeout_seconds=5,
    )
    reports = list(
        (
            await session.scalars(
                select(Artifact).where(
                    Artifact.run_id == run.id,
                    Artifact.artifact_type == "originality_report",
                )
            )
        ).all()
    )
    metrics = list(
        (
            await session.scalars(
                select(MetricPoint).where(
                    MetricPoint.run_id == run.id,
                    MetricPoint.source == "content-engine:a6",
                )
            )
        ).all()
    )

    assert result.report_artifact_id is None
    assert run.status == RunStatus.FAILED.value
    assert run.error is not None
    assert run.error["code"] == "CONTENT_ORIGINALITY_EVIDENCE_PERSIST_FAILED"
    assert reports == [] and metrics == []


async def _running_after_a5(
    session: AsyncSession,
    storage_root: Path,
    *,
    idempotency_key: str,
) -> tuple[Run, Approval, Artifact, Artifact, dict[str, object]]:
    run, approval, audio, manifest, visuals, caption, approval_input = (
        await _running_after_a4(
            session,
            storage_root,
            idempotency_key=idempotency_key,
        )
    )
    script = await session.scalar(
        select(Artifact).where(
            Artifact.run_id == run.id,
            Artifact.artifact_type == "script_bundle",
        )
    )
    assert script is not None
    assembled = await execute_approved_assembly_step(
        session,
        run=run,
        approval=approval,
        approval_input_payload=approval_input,
        audio_artifact=audio,
        visual_manifest_artifact=manifest,
        visual_artifacts=visuals,
        caption_artifact=caption,
        idempotency_key=idempotency_key,
        storage_root=storage_root,
        adapter=_assembly_adapter,
        cpu_power_watts=150,
        timeout_seconds=5,
        finalize_run=False,
    )
    final_video = await session.get(Artifact, assembled.artifact_id)
    assert final_video is not None and run.status == RunStatus.RUNNING.value
    return run, approval, script, final_video, approval_input


async def _run_through_a6(
    session: AsyncSession,
    storage_root: Path,
    *,
    idempotency_key: str,
) -> tuple[Run, StepRun, Artifact]:
    run, approval, script, final_video, approval_input = await _running_after_a5(
        session,
        storage_root,
        idempotency_key=idempotency_key,
    )
    result = await execute_similarity_gate_step(
        session,
        run=run,
        script_approval=approval,
        approval_input_payload=approval_input,
        script_artifact=script,
        final_video_artifact=final_video,
        idempotency_key=idempotency_key,
        storage_root=storage_root,
        threshold=Decimal("0.85"),
        reference_limit=20,
        cpu_power_watts=150,
        timeout_seconds=5,
    )
    step = await session.get(StepRun, result.step_run_id)
    report = await session.get(Artifact, result.report_artifact_id)
    assert step is not None and report is not None
    return run, step, report
