"""Tests for durable dispatch publication, leases, and duplicate delivery safety."""

from __future__ import annotations

import wave
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.database import Base
from src.pipeline import a1_executor
from src.pipeline.a1_executor import finalize_content_script_approval
from src.pipeline.script_gen import ScriptResult
from src.pipeline.tts import TTSAdapterResult
from src.platform.approval_service import decide_approval
from src.platform.dispatch_service import (
    DispatchClaimError,
    DispatchPublishError,
    claim_dispatch,
    execute_claimed_dispatch,
    finish_dispatch,
    publish_prepared_dispatch,
    publish_registered_dispatch,
    requeue_completed_run_dispatch,
)
from src.platform.models import (
    Approval,
    ApprovalStatus,
    Artifact,
    QueueClass,
    Run,
    RunDispatch,
    RunDispatchEvent,
    RunStatus,
    StepRun,
)


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as database_session:
        yield database_session
    await engine.dispose()


async def test_publish_failure_is_observable_and_reuses_stable_delivery(
    session: AsyncSession,
) -> None:
    deliveries: list[tuple[int, str, str]] = []

    def fail_publish(dispatch_id: int, delivery_id: str, queue: object) -> None:
        deliveries.append((dispatch_id, delivery_id, str(queue)))
        raise ConnectionError("secret broker detail")

    with pytest.raises(DispatchPublishError, match="remains retryable"):
        await publish_registered_dispatch(
            session,
            slug="platform-smoke",
            idempotency_key="publish-retry-001",
            publish=fail_publish,
        )

    dispatch = await session.scalar(select(RunDispatch))
    assert dispatch is not None
    assert dispatch.status == "pending"
    assert dispatch.publish_attempts == 1
    assert dispatch.last_error_code == "BROKER_PUBLISH_FAILED"

    def succeed_publish(dispatch_id: int, delivery_id: str, queue: object) -> None:
        deliveries.append((dispatch_id, delivery_id, str(queue)))

    result = await publish_registered_dispatch(
        session,
        slug="platform-smoke",
        idempotency_key="publish-retry-001",
        publish=succeed_publish,
    )
    await session.refresh(dispatch)

    assert result.created is False
    assert dispatch.status == "published"
    assert dispatch.publish_attempts == 2
    assert deliveries[0][:2] == deliveries[1][:2]
    assert "secret broker detail" not in repr(dispatch.last_error_code)
    event_types = list(
        (
            await session.scalars(
                select(RunDispatchEvent.event_type).order_by(RunDispatchEvent.id)
            )
        ).all()
    )
    assert event_types == ["prepared", "publish_failed", "publish_succeeded"]


async def test_claim_lease_reclaims_only_after_expiry_and_checks_owner(
    session: AsyncSession,
) -> None:
    prepared = await publish_registered_dispatch(
        session,
        slug="platform-smoke",
        idempotency_key="lease-001",
        publish=lambda *_args: None,
    )
    first = await claim_dispatch(
        session,
        dispatch_id=prepared.dispatch_id,
        delivery_id=prepared.delivery_id,
        worker_id="worker-a",
        lease_seconds=60,
    )
    duplicate = await claim_dispatch(
        session,
        dispatch_id=prepared.dispatch_id,
        delivery_id=prepared.delivery_id,
        worker_id="worker-b",
        lease_seconds=60,
    )
    assert first.acquired is True
    assert duplicate.acquired is False
    assert duplicate.retry_after_seconds is not None

    with pytest.raises(DispatchClaimError, match="token"):
        await finish_dispatch(
            session,
            dispatch_id=prepared.dispatch_id,
            claim_token="wrong-owner",
            succeeded=True,
        )

    dispatch = await session.get(RunDispatch, prepared.dispatch_id)
    assert dispatch is not None
    dispatch.lease_expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
    await session.commit()
    reclaimed = await claim_dispatch(
        session,
        dispatch_id=prepared.dispatch_id,
        delivery_id=prepared.delivery_id,
        worker_id="worker-b",
        lease_seconds=60,
    )
    assert reclaimed.acquired is True
    assert reclaimed.claim_token != first.claim_token

    await finish_dispatch(
        session,
        dispatch_id=prepared.dispatch_id,
        claim_token=reclaimed.claim_token or "",
        succeeded=True,
    )
    terminal = await claim_dispatch(
        session,
        dispatch_id=prepared.dispatch_id,
        delivery_id=prepared.delivery_id,
        worker_id="worker-c",
        lease_seconds=60,
    )
    assert terminal.terminal is True
    assert terminal.acquired is False


async def test_duplicate_delivery_executes_run_only_once(session: AsyncSession) -> None:
    prepared = await publish_registered_dispatch(
        session,
        slug="platform-smoke",
        idempotency_key="execute-once-001",
        publish=lambda *_args: None,
    )
    first = await execute_claimed_dispatch(
        session,
        dispatch_id=prepared.dispatch_id,
        delivery_id=prepared.delivery_id,
        worker_id="io-worker",
        lease_seconds=600,
    )
    second = await execute_claimed_dispatch(
        session,
        dispatch_id=prepared.dispatch_id,
        delivery_id=prepared.delivery_id,
        worker_id="io-worker",
        lease_seconds=600,
    )

    run = await session.get(Run, prepared.run_id)
    dispatch = await session.get(RunDispatch, prepared.dispatch_id)
    steps = list((await session.scalars(select(StepRun))).all())
    assert first.acquired is True
    assert second.terminal is True
    assert run is not None and run.status == "succeeded"
    assert dispatch is not None and dispatch.status == "completed"
    assert len(steps) == 1


async def test_claimed_content_a1_dispatch_produces_gpu_evidence_without_external_effects(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def generate(*_args: object) -> ScriptResult:
        nonlocal calls
        calls += 1
        return ScriptResult(
            angle="Ângulo seguro",
            outline="Outline seguro",
            full_script="Roteiro revisável gerado localmente.",
            hook="Hook seguro",
            narration="Roteiro revisável gerado localmente.",
        )

    monkeypatch.setattr(a1_executor, "generate_script", generate)
    monkeypatch.setattr(
        a1_executor,
        "settings",
        SimpleNamespace(
            storage_root=str(tmp_path / "storage"),
            ollama_model="fake-local-model",
            celery_soft_time_limit_seconds=300,
        ),
    )
    prepared = await publish_registered_dispatch(
        session,
        slug="content-engine",
        idempotency_key="dispatch-content-a1",
        input_payload={
            "topic": "Sono e memória",
            "persona": "Ciência acessível",
            "format": "short",
            "recent_openings": [],
        },
        publish=lambda *_args: None,
    )
    assert prepared.queue is QueueClass.GPU

    claimed = await execute_claimed_dispatch(
        session,
        dispatch_id=prepared.dispatch_id,
        delivery_id=prepared.delivery_id,
        worker_id="gpu-worker",
        lease_seconds=600,
    )
    duplicate = await execute_claimed_dispatch(
        session,
        dispatch_id=prepared.dispatch_id,
        delivery_id=prepared.delivery_id,
        worker_id="gpu-worker",
        lease_seconds=600,
    )

    run = await session.get(Run, prepared.run_id)
    dispatch = await session.get(RunDispatch, prepared.dispatch_id)
    steps = list((await session.scalars(select(StepRun))).all())
    artifacts = list((await session.scalars(select(Artifact))).all())
    assert calls == 1
    assert claimed.acquired is True
    assert duplicate.terminal is True
    assert run is not None and run.status == RunStatus.AWAITING_APPROVAL.value
    assert dispatch is not None and dispatch.status == "completed"
    assert len(steps) == 1 and steps[0].queue == QueueClass.GPU.value
    assert len(artifacts) == 1
    assert (tmp_path / "storage" / artifacts[0].relative_path).is_file()


async def test_approved_a1_requeues_same_dispatch_and_completes_tts_once(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script_calls = 0
    tts_calls = 0

    def generate(*_args: object) -> ScriptResult:
        nonlocal script_calls
        script_calls += 1
        return ScriptResult(
            angle="Ângulo seguro",
            outline="Outline seguro",
            full_script="Roteiro revisável gerado localmente.",
            hook="Hook seguro",
            narration="Narração aprovada para o adapter TTS.",
        )

    def synthesize(
        _text: str,
        destination: Path,
        voice_id: str,
        language_code: str,
    ) -> TTSAdapterResult:
        nonlocal tts_calls
        tts_calls += 1
        with wave.open(str(destination), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(24_000)
            audio.writeframes(b"\x00\x00" * 12_000)
        return TTSAdapterResult(
            backend="fake-local",
            model_id="fake-model-v1",
            voice_id=voice_id,
            language_code=language_code,
            license_id="TEST-ONLY",
        )

    storage_root = tmp_path / "storage"
    monkeypatch.setattr(a1_executor, "generate_script", generate)
    monkeypatch.setattr(
        a1_executor,
        "get_configured_tts_synthesizer",
        lambda _backend: synthesize,
    )
    monkeypatch.setattr(
        a1_executor,
        "settings",
        SimpleNamespace(
            storage_root=str(storage_root),
            ollama_model="fake-local-model",
            celery_soft_time_limit_seconds=300,
            content_tts_backend="test",
            content_tts_voice_id="pf_test",
            content_tts_language_code="p",
            gpu_power_watts=400,
        ),
    )
    published: list[tuple[int, str, QueueClass]] = []
    prepared = await publish_registered_dispatch(
        session,
        slug="content-engine",
        idempotency_key="dispatch-content-a2",
        input_payload={
            "topic": "Sono e memória",
            "persona": "Ciência acessível",
            "format": "short",
            "recent_openings": [],
        },
        publish=lambda dispatch_id, delivery_id, queue: published.append(
            (dispatch_id, delivery_id, queue)
        ),
    )
    await execute_claimed_dispatch(
        session,
        dispatch_id=prepared.dispatch_id,
        delivery_id=prepared.delivery_id,
        worker_id="gpu-worker-a1",
        lease_seconds=600,
    )
    approval = await session.scalar(
        select(Approval).where(Approval.run_id == prepared.run_id)
    )
    assert approval is not None
    await decide_approval(
        session,
        approval=approval,
        decision=ApprovalStatus.APPROVED,
        actor="local-owner",
        reason="roteiro integral revisado",
    )
    assert await finalize_content_script_approval(session, approval=approval) is True
    requeued = await requeue_completed_run_dispatch(
        session,
        run_id=prepared.run_id,
        actor="local-owner",
    )
    await session.commit()
    await publish_prepared_dispatch(
        session,
        dispatch_id=requeued.dispatch_id,
        publish=lambda dispatch_id, delivery_id, queue: published.append(
            (dispatch_id, delivery_id, queue)
        ),
    )
    continuation = await execute_claimed_dispatch(
        session,
        dispatch_id=prepared.dispatch_id,
        delivery_id=prepared.delivery_id,
        worker_id="gpu-worker-a2",
        lease_seconds=600,
    )
    duplicate = await execute_claimed_dispatch(
        session,
        dispatch_id=prepared.dispatch_id,
        delivery_id=prepared.delivery_id,
        worker_id="gpu-worker-a2",
        lease_seconds=600,
    )

    run = await session.get(Run, prepared.run_id)
    dispatch = await session.get(RunDispatch, prepared.dispatch_id)
    steps = list(
        (
            await session.scalars(
                select(StepRun)
                .where(StepRun.run_id == prepared.run_id)
                .order_by(StepRun.ordinal)
            )
        ).all()
    )
    artifacts = list(
        (await session.scalars(select(Artifact).where(Artifact.run_id == prepared.run_id))).all()
    )
    events = list(
        (
            await session.scalars(
                select(RunDispatchEvent).where(
                    RunDispatchEvent.dispatch_id == prepared.dispatch_id
                )
            )
        ).all()
    )
    assert script_calls == 1 and tts_calls == 1
    assert len(published) == 2
    assert requeued.changed is True and requeued.should_publish is True
    assert continuation.acquired is True and duplicate.terminal is True
    assert run is not None and run.status == RunStatus.SUCCEEDED.value
    assert dispatch is not None and dispatch.status == "completed"
    assert [step.name for step in steps] == ["generate-script-a1", "synthesize-tts-a2"]
    assert steps[1].approval_id == approval.id
    assert len(artifacts) == 2
    assert any(event.event_type == "requeued" for event in events)
