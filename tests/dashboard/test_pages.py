"""Separate dashboard pages: shared navigation, runs listing, approval and alert history."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.database import Base, get_session
from src.dashboard.listings import RUN_PAGE_SIZE, load_nav_summary, load_run_list
from src.dashboard.main import create_app
from src.platform.approval_service import decide_approval
from src.platform.models import (
    AlertSeverity,
    AlertStatus,
    Approval,
    ApprovalStatus,
    Automation,
    PlatformAlert,
    Run,
    RunStatus,
)
from tests.dashboard.test_dashboard import _seed_dashboard, _seed_pending_approval

_BROWSER = {"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
_DATA_PAGES = (
    "/",
    "/runs",
    "/approvals",
    "/alerts",
    "/schedules",
    "/automations",
    "/reports",
    "/data",
)


@pytest.fixture
async def session_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncGenerator[AsyncClient, None]:
    application = create_app(csrf_token="test-csrf-token")

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as http_client:
        yield http_client


async def _seed_many_runs(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    total: int,
) -> None:
    base = datetime(2026, 8, 1, 12, 0)
    statuses = (
        RunStatus.SUCCEEDED.value,
        RunStatus.FAILED.value,
        RunStatus.CANCELLED.value,
    )
    async with session_factory() as session:
        automation = Automation(slug="bulk-module", name="Bulk Module", owner="owner")
        session.add(automation)
        await session.flush()
        for index in range(total):
            queued = base + timedelta(minutes=index)
            session.add(
                Run(
                    automation_id=automation.id,
                    idempotency_key=f"bulk-run-{index}",
                    trigger="manual",
                    input_payload={"private": "bulk-input-secret"},
                    status=statuses[index % len(statuses)],
                    queued_at=queued,
                    started_at=queued + timedelta(seconds=5),
                    finished_at=queued + timedelta(seconds=65),
                )
            )
        await session.commit()


@pytest.mark.parametrize("path", _DATA_PAGES)
async def test_every_page_renders_with_the_shared_sidebar(
    session_factory: async_sessionmaker[AsyncSession],
    client: AsyncClient,
    path: str,
) -> None:
    await _seed_dashboard(session_factory)

    response = await client.get(path, headers=_BROWSER)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    for destination in (
        'href="/runs"',
        'href="/approvals"',
        'href="/alerts"',
        'href="/schedules"',
        'href="/automations"',
        'href="/reports"',
        'href="/data"',
    ):
        assert destination in response.text
    assert 'aria-current="page"' in response.text
    assert 'href="/automations#auto-content-engine"' in response.text
    assert "nav-count" in response.text


async def test_run_page_shows_the_same_sidebar_counters(
    session_factory: async_sessionmaker[AsyncSession],
    client: AsyncClient,
) -> None:
    run_id = await _seed_dashboard(session_factory)

    response = await client.get(f"/runs/{run_id}")

    assert response.status_code == 200
    assert 'href="/approvals"' in response.text
    assert "nav-count" in response.text
    assert 'href="/automations#auto-content-engine"' in response.text


async def test_nav_summary_counts_pending_approvals_and_open_alerts(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_dashboard(session_factory)

    async with session_factory() as session:
        nav = await load_nav_summary(session)

    assert nav.pending_approvals == 1
    assert nav.active_alerts == 1
    assert [automation.slug for automation in nav.automations] == ["content-engine"]


async def test_runs_page_paginates_and_filters_without_exposing_payloads(
    session_factory: async_sessionmaker[AsyncSession],
    client: AsyncClient,
) -> None:
    await _seed_many_runs(session_factory, total=RUN_PAGE_SIZE + 5)

    first = await client.get("/runs")
    second = await client.get("/runs?page=2")
    failed = await client.get("/runs?status=failed&automation=bulk-module")
    other = await client.get("/runs?automation=nao-existe")

    assert first.status_code == second.status_code == failed.status_code == 200
    assert f"{RUN_PAGE_SIZE + 5} runs" in first.text
    assert "Página 1 de 2" in first.text
    assert "Página 2 de 2" in second.text
    assert first.text.count('href="/runs/') == RUN_PAGE_SIZE
    assert second.text.count('href="/runs/') == 5
    assert "Próxima" in first.text and "Anterior" in second.text
    assert "bulk-input-secret" not in first.text
    # 30 runs alternam concluída, falha e cancelada: 10 de cada.
    assert "10 runs" in failed.text
    assert "Nenhuma run encontrada" in other.text


async def test_runs_page_rejects_an_unknown_status_with_a_readable_page(
    client: AsyncClient,
) -> None:
    browser = await client.get("/runs?status=banana", headers=_BROWSER)
    plain = await client.get("/runs?status=banana", headers={"Accept": "*/*"})
    bad_page = await client.get("/runs?page=0")

    assert browser.status_code == plain.status_code == bad_page.status_code == 400
    assert "Requisição inválida" in browser.text
    assert plain.json() == {"detail": "invalid run filter"}


async def test_load_run_list_counts_every_status_for_the_filter_chips(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_many_runs(session_factory, total=6)

    async with session_factory() as session:
        listing = await load_run_list(session)

    counts = dict(listing.status_counts)
    assert counts[RunStatus.SUCCEEDED.value] == 2
    assert counts[RunStatus.FAILED.value] == 2
    assert counts[RunStatus.CANCELLED.value] == 2
    assert counts[RunStatus.RUNNING.value] == 0
    assert listing.total == 6
    assert listing.items[0].duration_seconds == 60


async def test_approvals_page_lists_pending_and_the_decision_history(
    session_factory: async_sessionmaker[AsyncSession],
    client: AsyncClient,
) -> None:
    _run_id, approval_id = await _seed_pending_approval(session_factory)
    await _seed_dashboard(session_factory)
    async with session_factory() as session:
        pending = await session.get(Approval, approval_id)
        assert pending is not None
        await decide_approval(
            session,
            approval=pending,
            decision=ApprovalStatus.REJECTED,
            actor="private-actor-name",
            reason="private rejection reason",
        )
        await session.commit()

    response = await client.get("/approvals")

    assert response.status_code == 200
    assert "Decisões recentes" in response.text
    assert "Rejeitada" in response.text
    assert f"<strong>#{approval_id}</strong>" in response.text
    assert "Nenhuma decisão registrada" not in response.text
    # A seed aprovação ainda pendente aparece como pendente e com caminho para decidir.
    assert "Approval pendente" in response.text
    assert "private-actor-name" not in response.text
    assert "private rejection reason" not in response.text
    assert "private protected approval summary" not in response.text


async def test_approvals_page_is_empty_and_calm_without_approvals(
    client: AsyncClient,
) -> None:
    response = await client.get("/approvals")

    assert response.status_code == 200
    assert "Nenhuma approval pendente" in response.text
    assert "Nenhuma decisão registrada" in response.text


async def test_alerts_page_separates_active_from_resolved(
    session_factory: async_sessionmaker[AsyncSession],
    client: AsyncClient,
) -> None:
    await _seed_dashboard(session_factory)
    async with session_factory() as session:
        automation = await session.scalar(select(Automation))
        assert automation is not None
        when = datetime(2026, 8, 5, 10, 0)
        session.add(
            PlatformAlert(
                automation_id=automation.id,
                deduplication_key="resolved-alert-key",
                title="Disk almost full",
                summary="private resolved summary",
                severity=AlertSeverity.WARNING.value,
                status=AlertStatus.RESOLVED.value,
                source="private-resolved-source",
                occurrence_count=3,
                first_seen_at=when,
                last_seen_at=when,
                resolved_at=when + timedelta(hours=1),
                resolved_by="private-resolver",
                resolution_reason="private resolution reason",
            )
        )
        await session.commit()

    response = await client.get("/alerts")

    assert response.status_code == 200
    assert "Worker unavailable" in response.text
    assert "Resolvidos recentemente" in response.text
    assert "Disk almost full" in response.text
    for private_value in (
        "private resolved summary",
        "private-resolved-source",
        "private-resolver",
        "private resolution reason",
        "private alert summary",
    ):
        assert private_value not in response.text
