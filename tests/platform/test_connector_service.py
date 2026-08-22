"""Durable connector status, freshness, and quality observations."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.database import Base
from src.platform.connector_service import (
    ConnectorObservationValidationError,
    record_connector_observation,
)
from src.platform.models import Automation, ConnectorStatus, DataQualityStatus
from src.platform.run_service import IdempotencyConflictError, get_or_create_automation


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as database_session:
        yield database_session
    await engine.dispose()


async def _automation(session: AsyncSession) -> Automation:
    return (
        await get_or_create_automation(
            session,
            slug="connector-tests",
            name="Connector tests",
            owner="local-owner",
        )
    ).automation


async def test_connector_observation_is_exact_redacted_and_idempotent(
    session: AsyncSession,
) -> None:
    automation = await _automation(session)
    observed_at = datetime(
        2026,
        8,
        11,
        9,
        30,
        tzinfo=timezone(timedelta(hours=-3)),
    )
    last_success_at = observed_at - timedelta(minutes=5)
    first = await record_connector_observation(
        session,
        automation=automation,
        connector_key="youtube-analytics",
        idempotency_key="youtube-analytics:20260811T1230Z",
        status=ConnectorStatus.HEALTHY,
        status_slo_seconds=300,
        last_success_at=last_success_at,
        freshness_slo_seconds=900,
        quality_status=DataQualityStatus.PASS,
        quality_score="0.875",
        observed_at=observed_at,
        now=datetime(2026, 8, 11, 12, 31, tzinfo=UTC),
    )
    replay = await record_connector_observation(
        session,
        automation=automation,
        connector_key="youtube-analytics",
        idempotency_key="youtube-analytics:20260811T1230Z",
        status=ConnectorStatus.HEALTHY,
        status_slo_seconds=300,
        last_success_at=last_success_at,
        freshness_slo_seconds=900,
        quality_status=DataQualityStatus.PASS,
        quality_score=Decimal("0.8750"),
        observed_at=observed_at,
        now=datetime(2026, 8, 11, 13, 0, tzinfo=UTC),
    )

    assert first.created is True
    assert replay.created is False
    assert replay.observation.id == first.observation.id
    assert first.observation.quality_score == Decimal("0.8750")
    assert first.observation.observed_at == datetime(2026, 8, 11, 12, 30)
    assert first.observation.last_success_at == datetime(2026, 8, 11, 12, 25)
    assert first.observation.recorded_at == datetime(2026, 8, 11, 12, 31)
    assert not hasattr(first.observation, "endpoint")
    assert not hasattr(first.observation, "error_message")


async def test_connector_observation_rejects_divergent_replay(
    session: AsyncSession,
) -> None:
    automation = await _automation(session)
    observed_at = datetime(2026, 8, 11, 12, 30, tzinfo=UTC)
    common = {
        "automation": automation,
        "connector_key": "youtube-analytics",
        "idempotency_key": "youtube-analytics:20260811T1230Z",
        "status_slo_seconds": 300,
        "last_success_at": observed_at,
        "freshness_slo_seconds": 900,
        "quality_status": DataQualityStatus.PASS,
        "quality_score": "1",
        "observed_at": observed_at,
    }
    await record_connector_observation(
        session,
        status=ConnectorStatus.HEALTHY,
        **common,
    )

    with pytest.raises(IdempotencyConflictError, match="different inputs"):
        await record_connector_observation(
            session,
            status=ConnectorStatus.DEGRADED,
            **common,
        )


async def test_connector_observation_validation_is_fail_closed(
    session: AsyncSession,
) -> None:
    automation = await _automation(session)
    observed_at = datetime(2026, 8, 11, 12, 30, tzinfo=UTC)
    base = {
        "automation": automation,
        "connector_key": "youtube-analytics",
        "idempotency_key": "youtube-analytics:validation",
        "status": ConnectorStatus.HEALTHY,
        "status_slo_seconds": 300,
        "last_success_at": observed_at,
        "freshness_slo_seconds": 900,
        "quality_status": DataQualityStatus.PASS,
        "quality_score": "1",
        "observed_at": observed_at,
    }

    with pytest.raises(ConnectorObservationValidationError, match="lowercase ASCII"):
        await record_connector_observation(
            session,
            **{**base, "connector_key": "https://private-host/token"},
        )
    with pytest.raises(ConnectorObservationValidationError, match="timezone-aware"):
        await record_connector_observation(
            session,
            **{**base, "observed_at": observed_at.replace(tzinfo=None)},
        )
    with pytest.raises(ConnectorObservationValidationError, match="server clock"):
        await record_connector_observation(
            session,
            **base,
            now=observed_at - timedelta(seconds=1),
        )
    with pytest.raises(ConnectorObservationValidationError, match="must not be after"):
        await record_connector_observation(
            session,
            **{**base, "last_success_at": observed_at + timedelta(seconds=1)},
        )
    with pytest.raises(ConnectorObservationValidationError, match="between 1"):
        await record_connector_observation(
            session,
            **{**base, "freshness_slo_seconds": 0},
        )
    with pytest.raises(ConnectorObservationValidationError, match="cannot have"):
        await record_connector_observation(
            session,
            **{
                **base,
                "quality_status": DataQualityStatus.UNKNOWN,
                "quality_score": "0.5",
            },
        )
    with pytest.raises(ConnectorObservationValidationError, match="between 0 and 1"):
        await record_connector_observation(
            session,
            **{**base, "quality_score": "1.1"},
        )


async def test_connector_observation_requires_persisted_automation(
    session: AsyncSession,
) -> None:
    with pytest.raises(ConnectorObservationValidationError, match="must be persisted"):
        await record_connector_observation(
            session,
            automation=Automation(
                slug="not-persisted",
                name="Not persisted",
                owner="local-owner",
            ),
            connector_key="local-feed",
            idempotency_key="local-feed:1",
            status=ConnectorStatus.DISABLED,
            status_slo_seconds=300,
            last_success_at=None,
            freshness_slo_seconds=900,
            quality_status=DataQualityStatus.UNKNOWN,
            quality_score=None,
            observed_at=datetime(2026, 8, 11, 12, 30, tzinfo=UTC),
        )
