"""Content Engine A1 executor integration with the shared run contract."""

from __future__ import annotations

import errno
import hashlib
import json
from collections.abc import AsyncGenerator
from decimal import Decimal
from pathlib import Path

import pytest
from httpx import Request
from openai import APIConnectionError
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
    ContentScriptInput,
    execute_content_script_run,
    finalize_content_script_approval,
    load_content_script_review,
    parse_content_script_form,
    prepare_content_script_run,
)
from src.pipeline.script_gen import (
    OPENING_STYLE_LABELS,
    OPENING_STYLES,
    EmptyResponseError,
    ScriptLengthError,
    ScriptResult,
    SourcedFact,
    TextModelUse,
)
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
    with_facts = parse_content_script_form(
        {
            "topic": "Sono e memória",
            "persona": "Ciência acessível",
            "format": "short",
            "recent_openings": "",
            "facts": (
                "Adultos precisam de 7 horas de sono | https://example.test/sono\n\n"
                "  A | B dura 90 minutos |  Manual do sono, p. 12  "
            ),
        }
    )
    assert with_facts["facts"] == [
        {"claim": "Adultos precisam de 7 horas de sono", "source": "https://example.test/sono"},
        {"claim": "A | B dura 90 minutos", "source": "Manual do sono, p. 12"},
    ]
    with pytest.raises(ValueError, match="fato \\| fonte"):
        parse_content_script_form(
            {
                "topic": "Sono e memória",
                "persona": "Ciência acessível",
                "format": "short",
                "facts": "Número solto sem fonte",
            }
        )
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


async def test_a1_passes_sourced_facts_and_records_unsourced_claims(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    fact = {"claim": "Adultos precisam de 7 horas de sono", "source": "https://example.test/sono"}
    received: list[object] = []

    def generate(
        _topic: str,
        _persona: str,
        _fmt: str,
        _recent_openings: list[str] | None,
        *,
        facts: tuple[SourcedFact, ...],
    ) -> ScriptResult:
        received.extend(facts)
        return _generated_script().model_copy(
            update={"unsourced_claims": ["Quem dorme pouco erra o dobro."]}
        )

    result = await execute_content_script_run(
        session,
        idempotency_key="content-a1-facts",
        input_payload={**_input_payload(), "facts": [fact]},
        script_generator=generate,
        storage_root=tmp_path / "storage",
    )

    run = await session.get(Run, result.run_id)
    step = await session.get(StepRun, result.step_run_id)
    artifact = await session.get(Artifact, result.artifact_id)
    assert run is not None and step is not None and artifact is not None
    assert received == [SourcedFact(**fact)]
    assert run.input_payload == {**_input_payload(), "facts": [fact]}
    assert step.output_payload is not None
    assert step.output_payload["unsourced_claims"] == 1
    bundle = json.loads(
        (tmp_path / "storage" / artifact.relative_path).read_text(encoding="utf-8")
    )
    assert bundle["facts"] == [fact]
    assert bundle["result"]["unsourced_claims"] == ["Quem dorme pouco erra o dobro."]

    approval = await session.get(Approval, result.approval_id)
    assert approval is not None
    review = await load_content_script_review(
        session,
        approval=approval,
        storage_root=tmp_path / "storage",
    )
    assert review.facts == (SourcedFact(**fact),)
    assert review.unsourced_claims == ("Quem dorme pouco erra o dobro.",)


async def test_a1_rotates_the_automatic_opening_style_and_shows_what_was_asked(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    received: list[dict[str, object]] = []

    def generate(*_args: object, **options: object) -> ScriptResult:
        received.append(options)
        return _generated_script().model_copy(
            update={
                "opening_style": options["opening_style"],
                "opening_style_met": False,
                "structure": options["structure"],
            }
        )

    results = [
        await execute_content_script_run(
            session,
            idempotency_key=f"content-a1-variety-{index}",
            input_payload={**_input_payload(), "opening_style": "auto", "structure": "list"},
            script_generator=generate,
            storage_root=tmp_path / "storage",
        )
        for index in range(2)
    ]

    styles = [options["opening_style"] for options in received]
    assert all(style in OPENING_STYLES for style in styles)
    # Runs consecutivas recebem estilos diferentes, e cada run guarda o que recebeu.
    assert styles[0] != styles[1]
    assert all(options["structure"] == "list" for options in received)
    assert all("facts" not in options for options in received)
    run = await session.get(Run, results[0].run_id)
    step = await session.get(StepRun, results[0].step_run_id)
    approval = await session.get(Approval, results[0].approval_id)
    assert run is not None and step is not None and approval is not None
    assert run.input_payload == {
        **_input_payload(),
        "opening_style": "auto",
        "structure": "list",
    }
    assert step.output_payload is not None
    assert step.output_payload["opening_style"] == styles[0]
    assert step.output_payload["structure"] == "list"
    review = await load_content_script_review(
        session,
        approval=approval,
        storage_root=tmp_path / "storage",
    )
    assert review.opening_style == OPENING_STYLE_LABELS[styles[0]]  # type: ignore[index]
    assert review.opening_style_met is False
    assert review.structure == "Lista de itens"


async def test_a1_records_the_model_that_actually_wrote_the_script(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    uses = [
        TextModelUse(provider="codex_exec", model_id="codex-default", calls=3),
        TextModelUse(
            provider="ollama", model_id="qwen3:14b", fallback_from="codex_exec", calls=1
        ),
    ]
    generated = _generated_script().model_copy(update={"text_models": uses})

    result = await execute_content_script_run(
        session,
        idempotency_key="content-a1-attribution",
        input_payload=_input_payload(),
        script_generator=lambda *_args: generated,
        storage_root=tmp_path / "storage",
    )

    step = await session.get(StepRun, result.step_run_id)
    artifact = await session.get(Artifact, result.artifact_id)
    metrics = list((await session.scalars(select(MetricPoint))).all())
    assert step is not None and step.output_payload is not None and artifact is not None
    expected = "codex_exec:codex-default+qwen3:14b"
    assert step.output_payload["model"] == expected
    assert [metric.dimensions["model"] for metric in metrics] == [expected]
    bundle = json.loads(
        (tmp_path / "storage" / artifact.relative_path).read_text(encoding="utf-8")
    )
    assert bundle["model"] == expected
    assert bundle["result"]["text_models"] == [use.model_dump(mode="json") for use in uses]

    approval = await session.get(Approval, result.approval_id)
    assert approval is not None
    review = await load_content_script_review(
        session, approval=approval, storage_root=tmp_path / "storage"
    )
    assert review.hook == generated.hook


async def test_a1_without_attribution_keeps_the_configured_ollama_model(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.pipeline import a1_executor

    monkeypatch.setattr(a1_executor.settings, "ollama_model", "qwen3:14b")
    result = await execute_content_script_run(
        session,
        idempotency_key="content-a1-legacy-attribution",
        input_payload=_input_payload(),
        script_generator=lambda *_args: _generated_script(),
        storage_root=tmp_path / "storage",
    )

    step = await session.get(StepRun, result.step_run_id)
    metrics = list((await session.scalars(select(MetricPoint))).all())
    assert step is not None and step.output_payload is not None
    assert step.output_payload["model"] == "qwen3:14b"
    assert [metric.dimensions["model"] for metric in metrics] == ["qwen3:14b"]


def test_a1_input_without_facts_keeps_the_payload_of_earlier_runs() -> None:
    """Runs anteriores ao campo `facts` precisam continuar idempotentes."""
    assert ContentScriptInput.model_validate(_input_payload()).as_payload() == _input_payload()
    with pytest.raises(ValidationError):
        ContentScriptInput.model_validate(
            {**_input_payload(), "facts": [{"claim": "Número sem fonte", "source": ""}]}
        )


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


async def test_a1_script_outside_the_word_range_fails_retryable_without_artifact(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    def too_short(*_args: object) -> ScriptResult:
        raise ScriptLengthError("private narration detail")

    result = await execute_content_script_run(
        session,
        idempotency_key="content-a1-length",
        input_payload=_input_payload(),
        script_generator=too_short,
        storage_root=tmp_path / "storage",
    )

    run = await session.get(Run, result.run_id)
    step = await session.get(StepRun, result.step_run_id)
    artifacts = list((await session.scalars(select(Artifact))).all())
    approvals = list((await session.scalars(select(Approval))).all())
    assert run is not None and run.status == RunStatus.FAILED.value
    assert step is not None and step.status == RunStatus.FAILED.value
    assert run.error == step.error == {
        "code": "CONTENT_SCRIPT_LENGTH_OUT_OF_RANGE",
        "exception_type": "RetryableTaskError",
        "retryable": True,
        "timed_out": False,
    }
    assert artifacts == []
    assert approvals == []
    assert "private narration detail" not in str(run.error)
    assert not (tmp_path / "storage").exists()


async def test_a1_ollama_connection_failure_is_structured_and_has_no_artifact(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    def ollama_offline(*_args: object) -> ScriptResult:
        raise APIConnectionError(
            message="private connection detail",
            request=Request("POST", "http://127.0.0.1:11434/v1/chat/completions"),
        )

    result = await execute_content_script_run(
        session,
        idempotency_key="content-a1-ollama-offline",
        input_payload=_input_payload(),
        script_generator=ollama_offline,
        storage_root=tmp_path / "storage",
    )

    run = await session.get(Run, result.run_id)
    step = await session.get(StepRun, result.step_run_id)
    artifacts = list((await session.scalars(select(Artifact))).all())
    assert run is not None and run.status == RunStatus.FAILED.value
    assert step is not None and step.status == RunStatus.FAILED.value
    assert run.error == step.error == {
        "code": "CONTENT_LLM_UNAVAILABLE",
        "exception_type": "RetryableTaskError",
        "retryable": True,
        "timed_out": False,
    }
    assert artifacts == []
    assert "private connection detail" not in str(run.error)
    assert not (tmp_path / "storage").exists()


async def test_a1_disk_full_failure_is_structured_and_leaves_no_artifact(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def disk_full(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.ENOSPC, "private filesystem detail")

    monkeypatch.setattr(a1_executor, "_write_script_bundle", disk_full)
    result = await execute_content_script_run(
        session,
        idempotency_key="content-a1-disk-full",
        input_payload=_input_payload(),
        script_generator=lambda *_args: _generated_script(),
        storage_root=tmp_path / "storage",
    )

    run = await session.get(Run, result.run_id)
    step = await session.get(StepRun, result.step_run_id)
    artifacts = list((await session.scalars(select(Artifact))).all())
    approvals = list((await session.scalars(select(Approval))).all())
    assert run is not None and run.status == RunStatus.FAILED.value
    assert step is not None and step.status == RunStatus.FAILED.value
    assert run.error == step.error == {
        "code": "CONTENT_STORAGE_WRITE_FAILED",
        "exception_type": "RetryableTaskError",
        "retryable": True,
        "timed_out": False,
    }
    assert artifacts == []
    assert approvals == []
    assert "private filesystem detail" not in str(run.error)
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(a1_executor.settings, "content_tts_backend", "disabled")
    monkeypatch.setattr(
        a1_executor.settings,
        "content_narration_review_enabled",
        False,
    )
    monkeypatch.setattr(a1_executor.settings, "content_visual_backend", "disabled")
    monkeypatch.setattr(a1_executor.settings, "content_caption_backend", "disabled")
    monkeypatch.setattr(a1_executor.settings, "content_assembly_backend", "disabled")
    monkeypatch.setattr(
        a1_executor.settings,
        "content_caption_alignment_gate_enabled",
        False,
    )
    monkeypatch.setattr(a1_executor.settings, "content_visual_review_enabled", False)
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
    # Um roteiro sem a verificação não é o mesmo que um roteiro sem frases marcadas.
    assert review.unsourced_claims is None
    assert review.facts == ()
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
