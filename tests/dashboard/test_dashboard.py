"""Read-only dashboard projections and HTTP smoke tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime
from decimal import Decimal

import pytest
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.core.database import Base, get_session
from src.dashboard.main import create_app
from src.dashboard.service import load_dashboard_snapshot
from src.platform.models import (
    AlertSeverity,
    AlertStatus,
    Approval,
    ApprovalStatus,
    Automation,
    LedgerEntry,
    LedgerEntryType,
    PlatformAlert,
    Run,
    RunStatus,
)


@pytest.fixture
async def session_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _seed_dashboard(factory: async_sessionmaker[AsyncSession]) -> None:
    observed_at = datetime(2026, 7, 23, 12, 0)
    async with factory() as session:
        automation = Automation(
            slug="content-engine",
            name="Content Engine",
            owner="private-owner",
        )
        session.add(automation)
        await session.flush()
        run = Run(
            automation_id=automation.id,
            idempotency_key="private-run-key",
            trigger="manual",
            input_payload={"private": "input-secret"},
            output_payload={"private": "output-secret"},
            error={"private": "error-secret"},
            status=RunStatus.FAILED.value,
            queued_at=observed_at,
            finished_at=observed_at,
        )
        session.add(run)
        await session.flush()
        session.add(
            Approval(
                run_id=run.id,
                idempotency_key="private-approval-key",
                action="publish",
                summary="private approval summary",
                payload_digest="a" * 64,
                status=ApprovalStatus.PENDING.value,
                requested_at=observed_at,
            )
        )
        session.add(
            PlatformAlert(
                automation_id=automation.id,
                deduplication_key="private-alert-key",
                title="Worker unavailable",
                summary="private alert summary",
                severity=AlertSeverity.ERROR.value,
                status=AlertStatus.OPEN.value,
                source="private-alert-source",
                occurrence_count=2,
                first_seen_at=observed_at,
                last_seen_at=observed_at,
            )
        )
        for index, (entry_type, amount) in enumerate(
            (
                (LedgerEntryType.COST, "0.1"),
                (LedgerEntryType.COST, "0.2"),
                (LedgerEntryType.REVENUE, "1.0"),
                (LedgerEntryType.ATTRIBUTED_VALUE, "2.0"),
            ),
            start=1,
        ):
            session.add(
                LedgerEntry(
                    automation_id=automation.id,
                    idempotency_key=f"private-ledger-key-{index}",
                    entry_type=entry_type.value,
                    category="private-ledger-category",
                    amount=Decimal(amount),
                    currency="BRL",
                    source="private-ledger-source",
                    confidence=Decimal("0.5000"),
                    observed_at=observed_at,
                )
            )
        await session.commit()


async def test_snapshot_is_bounded_redacted_and_decimal_exact(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_dashboard(session_factory)
    async with session_factory() as session:
        snapshot = await load_dashboard_snapshot(session)

    assert snapshot.automations[0].slug == "content-engine"
    assert snapshot.recent_runs[0].status == RunStatus.FAILED.value
    assert snapshot.pending_approvals[0].action == "publish"
    assert snapshot.active_alerts[0].title == "Worker unavailable"
    assert snapshot.ledger_totals[0].cost == Decimal("0.3000000000")
    assert snapshot.ledger_totals[0].revenue == Decimal("1.0000000000")
    assert snapshot.ledger_totals[0].net_revenue == Decimal("0.7000000000")
    assert snapshot.ledger_totals[0].attributed_value == Decimal("2.0000000000")
    rendered = repr(snapshot)
    for private_value in (
        "private-owner",
        "private-run-key",
        "input-secret",
        "output-secret",
        "error-secret",
        "private approval summary",
        "private alert summary",
        "private-alert-source",
        "private-ledger-category",
        "private-ledger-source",
    ):
        assert private_value not in rendered


async def test_dashboard_home_renders_read_only_redacted_state(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_dashboard(session_factory)
    application = create_app()

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.get("/")

    assert response.status_code == 200
    assert "Automation Foundry" in response.text
    assert "Content Engine" in response.text
    assert "Worker unavailable" in response.text
    assert ">0.3<" in response.text
    assert ">0.7<" in response.text
    assert "<form" not in response.text
    for private_value in (
        "private-owner",
        "private-run-key",
        "input-secret",
        "output-secret",
        "error-secret",
        "private approval summary",
        "private alert summary",
        "private-alert-source",
        "private-ledger-category",
        "private-ledger-source",
    ):
        assert private_value not in response.text

    application_routes = [
        route for route in application.routes if isinstance(route, APIRoute)
    ]
    assert len(application_routes) == 1
    assert application_routes[0].methods == {"GET"}


async def test_dashboard_home_handles_empty_database(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    application = create_app()

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://test",
    ) as client:
        response = await client.get("/")

    assert response.status_code == 200
    assert "Nenhuma automação registrada" in response.text
    assert "Nenhuma run registrada" in response.text
