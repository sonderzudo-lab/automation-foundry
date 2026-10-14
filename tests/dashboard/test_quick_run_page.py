"""Dashboard quick run: one click draws a topic and starts a local Content Engine run."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.core.database import Base, get_session
from src.dashboard import main as dashboard_main
from src.dashboard.main import create_app
from src.platform.models import QueueClass, Run

_PERSONA = "Criador brasileiro falando de forma próxima e clara"


@pytest.fixture
async def session_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


def _client(
    session_factory: async_sessionmaker[AsyncSession],
    published: list[tuple[int, str, QueueClass]],
) -> AsyncClient:
    application = create_app(
        csrf_token="test-csrf-token",
        dispatch_publisher=lambda dispatch_id, delivery_id, queue: published.append(
            (dispatch_id, delivery_id, queue)
        ),
    )

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    return AsyncClient(transport=ASGITransport(app=application), base_url="http://127.0.0.1")


def _configure(
    monkeypatch: pytest.MonkeyPatch, topics_path: Path, persona: str | None = _PERSONA
) -> None:
    monkeypatch.setattr(
        dashboard_main,
        "settings",
        SimpleNamespace(
            content_quick_topics_path=str(topics_path), content_default_persona=persona
        ),
    )


async def test_quick_run_starts_a_run_with_a_fresh_topic_and_generated_key(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    topics_path = tmp_path / "pautas.txt"
    topics_path.write_text("# pautas\nPlanejar o dia antes de dormir\nPausas curtas\n", "utf-8")
    _configure(monkeypatch, topics_path)
    published: list[tuple[int, str, QueueClass]] = []
    async with _client(session_factory, published) as client:
        page = await client.get("/automations")
        first = await client.post(
            "/automations/content-engine/quick-run",
            data={"csrf_token": "test-csrf-token", "confirmation": "start-quick-run"},
        )
        second = await client.post(
            "/automations/content-engine/quick-run",
            data={"csrf_token": "test-csrf-token", "confirmation": "start-quick-run"},
        )
        forged = await client.post(
            "/automations/content-engine/quick-run",
            data={"csrf_token": "wrong", "confirmation": "start-quick-run"},
        )
        unconfirmed = await client.post(
            "/automations/content-engine/quick-run",
            data={"csrf_token": "test-csrf-token", "confirmation": "start-example-run"},
        )
        injected = await client.post(
            "/automations/content-engine/quick-run",
            data={
                "csrf_token": "test-csrf-token",
                "confirmation": "start-quick-run",
                "topic": "outra pauta",
            },
        )

    assert page.status_code == 200
    assert "Disparar run aleatória" in page.text
    assert "2 de 2 ainda não usadas" in page.text
    assert "Preencher manualmente" in page.text
    assert _PERSONA in page.text
    assert first.status_code == 303 and first.headers["location"].startswith("/runs/")
    assert second.status_code == 303 and second.headers["location"] != first.headers["location"]
    assert forged.status_code == 403
    assert unconfirmed.status_code == 400
    assert injected.status_code == 400
    async with session_factory() as session:
        runs = list((await session.scalars(select(Run).order_by(Run.id))).all())
    assert len(runs) == 2 and len(published) == 2
    # The second click drew the topic the first one had not used.
    assert {run.input_payload["topic"] for run in runs} == {
        "Planejar o dia antes de dormir",
        "Pausas curtas",
    }
    assert all(run.input_payload["persona"] == _PERSONA for run in runs)
    assert all(run.idempotency_key.startswith("content-") for run in runs)
    assert all(queue is QueueClass.GPU for _id, _delivery, queue in published)


async def test_quick_run_without_topics_or_persona_explains_instead_of_starting(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch, tmp_path / "nenhuma.txt", persona=None)
    published: list[tuple[int, str, QueueClass]] = []
    async with _client(session_factory, published) as client:
        page = await client.get("/automations")
        response = await client.post(
            "/automations/content-engine/quick-run",
            data={"csrf_token": "test-csrf-token", "confirmation": "start-quick-run"},
        )
        notice = await client.get(response.headers["location"])

    assert "Disparo rápido indisponível" in page.text
    assert "Disparar run aleatória" not in page.text
    assert response.status_code == 303
    assert "A run aleatória não foi disparada" in notice.text
    assert published == []
    async with session_factory() as session:
        assert (await session.scalars(select(Run))).all() == []


async def test_full_form_generates_the_key_when_left_blank(
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch, tmp_path / "nenhuma.txt")
    published: list[tuple[int, str, QueueClass]] = []
    async with _client(session_factory, published) as client:
        response = await client.post(
            "/automations/content-engine/runs",
            data={
                "csrf_token": "test-csrf-token",
                "confirmation": "start-example-run",
                "idempotency_key": "",
                "topic": "Pauta manual",
                "persona": _PERSONA,
                "format": "short",
            },
        )
    assert response.status_code == 303
    async with session_factory() as session:
        run = await session.scalar(select(Run))
    assert run is not None and run.idempotency_key.startswith("content-")
