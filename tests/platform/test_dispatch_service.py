"""Tests for durable dispatch publication, leases, and duplicate delivery safety."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.database import Base
from src.platform.dispatch_service import (
    DispatchClaimError,
    DispatchPublishError,
    claim_dispatch,
    execute_claimed_dispatch,
    finish_dispatch,
    publish_registered_dispatch,
)
from src.platform.models import Run, RunDispatch, RunDispatchEvent, StepRun


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
