"""Content Engine A1 executor integration with the shared run contract."""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncGenerator
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.core.database import Base
from src.pipeline import a1_executor
from src.pipeline.a1_executor import (
    execute_content_script_run,
    finalize_content_script_approval,
    load_content_script_review,
    parse_content_script_form,
    prepare_content_script_run,
)
from src.pipeline.script_gen import EmptyResponseError, ScriptResult
from src.platform.approval_service import approval_payload_digest, decide_approval
from src.platform.control_service import request_run_cancellation
from src.platform.models import (
    Approval,
    ApprovalStatus,
    Artifact,
    LedgerEntry,
    MetricPoint,
    Run,
    RunStatus,
    StepRun,
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


def _input_payload() -> dict[str, object]:
    return {
        "topic": "Como o sono consolida memórias",
        "persona": "Divulgador científico brasileiro, claro e rigoroso",
        "format": "short",
        "recent_openings": ["Você já percebeu como uma memória muda após dormir?"],
    }


def _generated_script() -> ScriptResult:
    return ScriptResult(
        angle="Dormir também é editar o que o cérebro guardará.",
        outline="1. Gancho\n2. Consolidação\n3. Aplicação",
        full_script="O sono reorganiza as memórias que formamos durante o dia.",
        hook="Seu cérebro edita memórias enquanto você dorme.",
        narration="O sono reorganiza as memórias que formamos durante o dia.",
    )


def test_manual_form_is_typed_bounded_and_normalized() -> None:
    parsed = parse_content_script_form(
        {
            "topic": "  Sono e memória  ",
            "persona": "  Ciência acessível  ",
            "format": "short",
            "recent_openings": "Primeira abertura\n\nSegunda abertura",
        }
    )

    assert parsed == {
        "topic": "Sono e memória",
        "persona": "Ciência acessível",
        "format": "short",
        "recent_openings": ["Primeira abertura", "Segunda abertura"],
    }
    with pytest.raises(ValidationError):
        parse_content_script_form(
            {
                "topic": "x",
                "persona": "Ciência acessível",
                "format": "short",
                "recent_openings": "",
            }
        )
    with pytest.raises(ValueError, match="unsupported"):
        parse_content_script_form(
            {
                "topic": "Sono e memória",
                "persona": "Ciência acessível",
                "format": "short",
                "recent_openings": "",
                "publish": "true",
            }
        )


async def test_a1_success_is_observable_idempotent_and_reviewable(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    calls = 0

    def generate(
        topic: str,
        persona: str,
        fmt: str,
        recent_openings: list[str] | None,
    ) -> ScriptResult:
        nonlocal calls
        calls += 1
        assert topic == _input_payload()["topic"]
        assert persona == _input_payload()["persona"]
        assert fmt == "short"
        assert recent_openings == _input_payload()["recent_openings"]
        return _generated_script()

    first = await execute_content_script_run(
        session,
        idempotency_key="content-a1-success",
        input_payload=_input_payload(),
        script_generator=generate,  # type: ignore[arg-type]
        storage_root=tmp_path / "storage",
    )
    await session.commit()
    replay = await execute_content_script_run(
        session,
        idempotency_key="content-a1-success",
        input_payload=_input_payload(),
        script_generator=generate,  # type: ignore[arg-type]
        storage_root=tmp_path / "storage",
    )

    run = await session.get(Run, first.run_id)
    step = await session.get(StepRun, first.step_run_id)
    artifact = await session.get(Artifact, first.artifact_id)
    approval = await session.get(Approval, first.approval_id)
    metrics = list((await session.scalars(select(MetricPoint))).all())
    ledger = list((await session.scalars(select(LedgerEntry))).all())

    assert calls == 1
    assert first.created is True and first.replayed is False
    assert replay.created is False and replay.replayed is True
    assert replay.run_id == first.run_id
    assert run is not None and run.status == RunStatus.AWAITING_APPROVAL.value
    assert run.trigger == "manual"
    assert run.input_payload == _input_payload()
    assert step is not None and step.status == RunStatus.SUCCEEDED.value
    assert step.queue == "gpu"
    assert step.attempt == 1
    assert artifact is not None
    assert artifact.step_run_id == step.id
    assert artifact.artifact_type == "script_bundle"
    assert artifact.retention_days == 90
    assert approval is not None
    assert approval.status == ApprovalStatus.PENDING.value
    assert approval.action == "review_content_script_a1"
    assert approval.review_payload is not None
    assert approval.review_payload["hook"] == _generated_script().hook
    assert "persona" not in approval.review_payload
    assert approval.payload_digest == approval_payload_digest(
        {
            "schema_version": 1,
            "run_id": run.id,
            "step_run_id": step.id,
            "artifact_id": artifact.id,
            "artifact_sha256": artifact.sha256,
            "narration_sha256": hashlib.sha256(
                _generated_script().narration.encode("utf-8")
            ).hexdigest(),
        }
    )
    assert first.approval_id == approval.id
    assert replay.approval_id == approval.id
    bundle_path = tmp_path / "storage" / artifact.relative_path
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    assert bundle["schema_version"] == 1
    assert bundle["result"]["full_script"] == _generated_script().full_script
    assert bundle["result"]["narration"] == _generated_script().narration
    assert not list(bundle_path.parent.glob("*.tmp"))
    assert len(metrics) == 1
    assert metrics[0].name == "content_script_narration_characters"
    assert metrics[0].value == Decimal(str(len(_generated_script().narration)))
    assert len(ledger) == 1
    assert ledger[0].entry_type == "cost"
    assert ledger[0].amount == Decimal("0")
    assert ledger[0].currency == "BRL"


async def test_a1_failure_is_structured_retryable_and_has_no_artifact(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    def unavailable(
        _topic: str,
        _persona: str,
        _fmt: str,
        _recent_openings: list[str] | None,
    ) -> ScriptResult:
        raise EmptyResponseError("private Ollama response")

    result = await execute_content_script_run(
        session,
        idempotency_key="content-a1-failed",
        input_payload=_input_payload(),
        script_generator=unavailable,  # type: ignore[arg-type]
        storage_root=tmp_path / "storage",
    )

    run = await session.get(Run, result.run_id)
    step = await session.get(StepRun, result.step_run_id)
    artifacts = list((await session.scalars(select(Artifact))).all())
    assert run is not None and run.status == RunStatus.FAILED.value
    assert run.error == {
        "code": "CONTENT_LLM_UNAVAILABLE",
        "exception_type": "RetryableTaskError",
        "retryable": True,
        "timed_out": False,
    }
    assert step is not None and step.error == run.error
    assert artifacts == []
    assert not (tmp_path / "storage").exists()


async def test_a1_explicit_retry_creates_a_new_run_with_same_validated_input(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    def unavailable(
        _topic: str,
        _persona: str,
        _fmt: str,
        _recent_openings: list[str] | None,
    ) -> ScriptResult:
        raise EmptyResponseError("private Ollama response")

    source = await execute_content_script_run(
        session,
        idempotency_key="content-a1-retry-source",
        input_payload=_input_payload(),
        script_generator=unavailable,  # type: ignore[arg-type]
        storage_root=tmp_path / "storage",
    )
    retry = await execute_content_script_run(
        session,
        idempotency_key="content-a1-retry-successor",
        retry_of_run_id=source.run_id,
        retry_requested_by="local-owner",
        retry_reason="Ollama local recuperado",
        input_payload=_input_payload(),
        script_generator=lambda *_args: _generated_script(),
        storage_root=tmp_path / "storage",
    )

    retry_run = await session.get(Run, retry.run_id)
    assert retry.run_id != source.run_id
    assert retry_run is not None
    assert retry_run.status == RunStatus.AWAITING_APPROVAL.value
    assert retry_run.trigger == "retry"
    assert retry_run.retry_of_run_id == source.run_id
    assert retry_run.retry_requested_by == "local-owner"
    assert retry_run.retry_reason == "Ollama local recuperado"
    assert retry_run.input_payload == _input_payload()


async def test_a1_approval_reviews_exact_bundle_and_finalizes_idempotently(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    result = await execute_content_script_run(
        session,
        idempotency_key="content-a1-approved",
        input_payload=_input_payload(),
        script_generator=lambda *_args: _generated_script(),
        storage_root=tmp_path / "storage",
    )
    approval = await session.get(Approval, result.approval_id)
    assert approval is not None

    review = await load_content_script_review(
        session,
        approval=approval,
        storage_root=tmp_path / "storage",
    )
    assert review.full_script == _generated_script().full_script
    assert review.narration == _generated_script().narration
    assert not hasattr(review, "persona")

    await decide_approval(
        session,
        approval=approval,
        decision=ApprovalStatus.APPROVED,
        actor="local-owner",
        reason="roteiro integral revisado",
    )
    continuation_required = await finalize_content_script_approval(
        session,
        approval=approval,
        storage_root=tmp_path / "storage",
    )
    replayed_continuation_required = await finalize_content_script_approval(
        session,
        approval=approval,
        storage_root=tmp_path / "storage",
    )
    run = await session.get(Run, result.run_id)
    metric = await session.scalar(
        select(MetricPoint).where(MetricPoint.run_id == result.run_id)
    )
    ledger = await session.scalar(
        select(LedgerEntry).where(LedgerEntry.run_id == result.run_id)
    )

    assert continuation_required is False
    assert replayed_continuation_required is False
    assert run is not None and run.status == RunStatus.SUCCEEDED.value
    assert metric is not None and ledger is not None
    assert run.output_payload == {
        "step_run_id": result.step_run_id,
        "artifact_id": result.artifact_id,
        "approval_id": result.approval_id,
        "metric_point_id": metric.id,
        "ledger_entry_id": ledger.id,
    }


async def test_a1_approval_rolls_back_when_bundle_changed_after_request(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    result = await execute_content_script_run(
        session,
        idempotency_key="content-a1-tampered",
        input_payload=_input_payload(),
        script_generator=lambda *_args: _generated_script(),
        storage_root=tmp_path / "storage",
    )
    await session.commit()
    approval = await session.get(Approval, result.approval_id)
    artifact = await session.get(Artifact, result.artifact_id)
    assert approval is not None and artifact is not None
    bundle_path = tmp_path / "storage" / artifact.relative_path
    bundle_path.write_text('{"changed": true}\n', encoding="utf-8")

    await decide_approval(
        session,
        approval=approval,
        decision=ApprovalStatus.APPROVED,
        actor="local-owner",
        reason="attempt against changed evidence",
    )
    with pytest.raises(ValueError, match="size|checksum"):
        await finalize_content_script_approval(
            session,
            approval=approval,
            storage_root=tmp_path / "storage",
        )
    await session.rollback()
    await session.refresh(approval)
    run = await session.get(Run, result.run_id)

    assert approval.status == ApprovalStatus.PENDING.value
    assert run is not None and run.status == RunStatus.AWAITING_APPROVAL.value


async def test_a1_rejected_script_cancels_run_without_follow_up_step(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    result = await execute_content_script_run(
        session,
        idempotency_key="content-a1-rejected",
        input_payload=_input_payload(),
        script_generator=lambda *_args: _generated_script(),
        storage_root=tmp_path / "storage",
    )
    approval = await session.get(Approval, result.approval_id)
    assert approval is not None

    await decide_approval(
        session,
        approval=approval,
        decision=ApprovalStatus.REJECTED,
        actor="local-owner",
        reason="roteiro precisa de nova versão",
    )
    run = await session.get(Run, result.run_id)
    steps = list(
        (await session.scalars(select(StepRun).where(StepRun.run_id == result.run_id))).all()
    )

    assert approval.status == ApprovalStatus.REJECTED.value
    assert run is not None and run.status == RunStatus.CANCELLED.value
    assert len(steps) == 1


async def test_a1_evidence_failure_closes_run_as_retryable(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_registration(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("private filesystem detail")

    monkeypatch.setattr(a1_executor, "register_local_artifact", fail_registration)
    result = await execute_content_script_run(
        session,
        idempotency_key="content-a1-evidence-failure",
        input_payload=_input_payload(),
        script_generator=lambda *_args: _generated_script(),
        storage_root=tmp_path / "storage",
    )

    run = await session.get(Run, result.run_id)
    step = await session.get(StepRun, result.step_run_id)
    artifacts = list((await session.scalars(select(Artifact))).all())
    metrics = list((await session.scalars(select(MetricPoint))).all())
    ledger = list((await session.scalars(select(LedgerEntry))).all())
    assert run is not None and run.status == RunStatus.FAILED.value
    assert run.error == {
        "code": "CONTENT_EVIDENCE_PERSIST_FAILED",
        "exception_type": "RuntimeError",
        "retryable": True,
        "timed_out": False,
    }
    assert "private filesystem detail" not in str(run.error)
    assert step is not None and step.status == RunStatus.SUCCEEDED.value
    assert artifacts == [] and metrics == [] and ledger == []
    bundles = list((tmp_path / "storage").rglob("script.json"))
    assert len(bundles) == 1


async def test_a1_cancelled_before_execution_never_contacts_generator(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    prepared = await prepare_content_script_run(
        session,
        idempotency_key="content-a1-cancelled",
        input_payload=_input_payload(),
    )
    await request_run_cancellation(
        session,
        run=prepared.run,
        reason="operator cancelled before GPU work",
    )
    await session.commit()

    def must_not_run(*_args: object) -> ScriptResult:
        raise AssertionError("generator must not be called")

    result = await execute_content_script_run(
        session,
        idempotency_key="content-a1-cancelled",
        input_payload=_input_payload(),
        script_generator=must_not_run,  # type: ignore[arg-type]
        storage_root=tmp_path / "storage",
    )

    assert result.run_status == RunStatus.CANCELLED.value
    assert result.step_run_id is None
    assert not (tmp_path / "storage").exists()
