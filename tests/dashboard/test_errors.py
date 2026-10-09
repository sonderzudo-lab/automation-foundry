"""Tests for the human-readable error pages shown to browsers."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.database import Base, get_session
from src.dashboard.errors import describe_error, safe_back_url, wants_html
from src.dashboard.main import create_app

_BROWSER_ACCEPT = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
_BAD_CSRF_FORM = {
    "csrf_token": "wrong-token",
    "confirmation": "kill-switch-enable",
    "target": "enable",
    "actor": "local-owner",
    "reason": "must not persist",
}


@pytest.mark.parametrize(
    ("accept", "expected"),
    [
        (_BROWSER_ACCEPT, True),
        ("TEXT/HTML", True),
        ("*/*", False),
        ("application/json", False),
        ("audio/mpeg,*/*;q=0.5", False),
        ("", False),
        (None, False),
    ],
)
def test_wants_html_only_for_browser_navigations(accept: str | None, expected: bool) -> None:
    assert wants_html(accept) is expected


@pytest.mark.parametrize(
    ("referer", "expected"),
    [
        ("http://127.0.0.1:8000/", "/"),
        ("http://127.0.0.1:8000/runs/4?x=1", "/runs/4?x=1"),
        ("http://127.0.0.1:8000", "/"),
        (None, None),
        ("", None),
        ("http://evil.example/runs/4", None),
        ("http://127.0.0.1:9999/runs/4", None),
        ("javascript:alert(1)", None),
        ("http://127.0.0.1:8000//evil.example/x", None),
        ("http://127.0.0.1:8000/automations/x/runs", None),
        ("http://[bad", None),
    ],
)
def test_safe_back_url_keeps_only_same_host_paths(
    referer: str | None, expected: str | None
) -> None:
    result = safe_back_url(
        referer,
        host="127.0.0.1:8000",
        current_path="/automations/x/runs",
    )

    assert result == expected


def test_describe_error_prefers_the_specific_detail() -> None:
    page = describe_error(403, "invalid csrf token", back_url="/runs/1")

    assert page.title == "Sessão do painel expirada"
    assert page.code == "invalid csrf token"
    assert page.back_url == "/runs/1"
    assert page.status_code == 403


def test_describe_error_falls_back_by_status_and_hides_unknown_input() -> None:
    page = describe_error(404, {"input": "secret value"})

    assert page.title == "Não encontrado"
    assert page.code is None
    assert "secret" not in page.message


def test_describe_error_drops_overlong_details_and_covers_server_errors() -> None:
    long_page = describe_error(409, "x" * 500)
    server_page = describe_error(502, "upstream exploded")

    assert long_page.code is None
    assert long_page.title == "Ação indisponível no estado atual"
    assert server_page.title == "Erro no painel"
    assert server_page.status_code == 502


@pytest.fixture
async def client() -> AsyncGenerator[AsyncClient, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    application = create_app(csrf_token="test-csrf-token")

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as http_client:
        yield http_client
    await engine.dispose()


async def test_missing_run_renders_an_html_page_for_browsers(client: AsyncClient) -> None:
    response = await client.get("/runs/9999", headers={"Accept": _BROWSER_ACCEPT})

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"
    assert "Não encontrado" in response.text
    assert "<code>run not found</code>" in response.text
    assert 'href="/"' in response.text
    assert "Voltar à visão geral" in response.text


async def test_missing_run_keeps_json_for_other_clients(client: AsyncClient) -> None:
    response = await client.get("/runs/9999", headers={"Accept": "*/*"})

    assert response.status_code == 404
    assert response.json() == {"detail": "run not found"}


async def test_unknown_path_is_a_friendly_404_for_browsers(client: AsyncClient) -> None:
    response = await client.get("/nao-existe", headers={"Accept": _BROWSER_ACCEPT})

    assert response.status_code == 404
    assert "Não encontrado" in response.text
    assert "HTTP 404" in response.text


async def test_rejected_form_explains_the_cause_and_links_back(client: AsyncClient) -> None:
    response = await client.post(
        "/automations/1/kill-switch",
        data=_BAD_CSRF_FORM,
        headers={
            "Accept": _BROWSER_ACCEPT,
            "Referer": "http://127.0.0.1/#executar-content-engine",
        },
    )

    assert response.status_code == 403
    assert "Sessão do painel expirada" in response.text
    assert 'href="/"' in response.text
    assert "wrong-token" not in response.text


async def test_back_link_uses_the_referring_page_when_it_is_safe(client: AsyncClient) -> None:
    response = await client.post(
        "/automations/1/kill-switch",
        data=_BAD_CSRF_FORM,
        headers={"Accept": _BROWSER_ACCEPT, "Referer": "http://127.0.0.1/runs/7"},
    )

    assert response.status_code == 403
    assert 'href="/runs/7"' in response.text
    assert "Voltar à página anterior" in response.text


async def test_back_link_ignores_foreign_referers(client: AsyncClient) -> None:
    response = await client.post(
        "/automations/1/kill-switch",
        data=_BAD_CSRF_FORM,
        headers={"Accept": _BROWSER_ACCEPT, "Referer": "http://evil.example/runs/7"},
    )

    assert response.status_code == 403
    assert "evil.example" not in response.text
    assert "Voltar à página anterior" not in response.text


async def test_rejected_form_keeps_json_for_other_clients(client: AsyncClient) -> None:
    response = await client.post(
        "/automations/1/kill-switch",
        data=_BAD_CSRF_FORM,
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "invalid csrf token"}


async def test_validation_errors_do_not_echo_the_submitted_value(client: AsyncClient) -> None:
    response = await client.get("/runs/valor-sensivel-123", headers={"Accept": _BROWSER_ACCEPT})

    assert response.status_code == 422
    assert "Dados inválidos" in response.text
    assert "valor-sensivel-123" not in response.text
