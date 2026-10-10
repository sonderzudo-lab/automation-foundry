"""Dashboard action "Verificar preparação": explicit, read-only and accessible."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.config import Settings
from src.core.database import Base, get_session
from src.dashboard.main import create_app
from src.pipeline.readiness import (
    ReadinessCheck,
    ReadinessReport,
    ReadinessStatus,
    collect_content_readiness,
)
from src.pipeline.readiness_checks import ProbeCode
from src.platform.models import Approval, Run
from tests.dashboard.test_dashboard import _seed_dashboard

_BROWSER = {"Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
_URL = "/automations/content-engine/readiness"
_TOKEN = "test-csrf-token"
_FORM = {"Content-Type": "application/x-www-form-urlencoded"}
_PRIVATE = "C:\\Users\\private-owner\\secret-models"


@pytest.fixture
async def session_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _check(
    check_id: str,
    label: str,
    status: ReadinessStatus,
    code: ProbeCode,
    guidance: str,
    *,
    free_bytes: int | None = None,
) -> ReadinessCheck:
    return ReadinessCheck(
        check_id, label, status, code, guidance,
        datetime(2026, 10, 9, 15, 30, 7, tzinfo=UTC), free_bytes,
    )


def _report() -> ReadinessReport:
    return ReadinessReport(
        datetime(2026, 10, 9, 15, 30, 5, tzinfo=UTC),
        datetime(2026, 10, 9, 15, 30, 8, tzinfo=UTC),
        (
            _check("ollama", "Roteiro · Ollama e modelo configurado", ReadinessStatus.READY,
                   ProbeCode.OK, "O Ollama respondeu e o modelo está na lista."),
            _check("storage", "Armazenamento · STORAGE_ROOT", ReadinessStatus.READY,
                   ProbeCode.OK, "A pasta existe.", free_bytes=5 * 1024**3),
            _check("ffmpeg", "Montagem · FFmpeg", ReadinessStatus.PROBLEM,
                   ProbeCode.MISSING, "Executável não encontrado. Confira CONTENT_FFMPEG_PATH."),
            _check("tts_cache", "Narração · cache", ReadinessStatus.NOT_APPLICABLE,
                   ProbeCode.DISABLED, "A narração está desabilitada."),
            _check("visual_inputs", "Visuais · manifesto e imagens da run",
                   ReadinessStatus.NOT_CHECKED, ProbeCode.DEFERRED,
                   "Insumo posterior: não impede iniciar o roteiro."),
        ),
    )


class _Collector:
    def __init__(self, report: ReadinessReport | None = None) -> None:
        self.calls = 0
        self.report = report or _report()

    async def __call__(self) -> ReadinessReport:
        self.calls += 1
        return self.report


def _client(
    factory: async_sessionmaker[AsyncSession],
    collector: Callable[[], object],
) -> AsyncClient:
    application = create_app(csrf_token=_TOKEN, readiness_collector=collector)  # type: ignore[arg-type]

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    return AsyncClient(
        transport=ASGITransport(app=application), base_url="http://127.0.0.1",
    )


async def test_opening_the_page_does_not_run_any_probe(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    collector = _Collector()
    async with _client(session_factory, collector) as client:
        response = await client.get(_URL, headers=_BROWSER)

    assert response.status_code == 200
    assert collector.calls == 0
    assert response.headers["cache-control"] == "no-store"
    assert "Ainda não consultado" in response.text
    assert f'action="{_URL}"' in response.text
    assert 'method="post"' in response.text
    assert f'name="csrf_token" value="{_TOKEN}"' in response.text
    assert "Verificar preparação" in response.text
    assert "Nenhum problema nos itens verificados" not in response.text


async def test_post_runs_the_diagnostic_and_renders_every_state_in_portuguese(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    collector = _Collector()
    async with _client(session_factory, collector) as client:
        response = await client.post(
            _URL, content=f"csrf_token={_TOKEN}", headers={**_BROWSER, **_FORM},
        )
    html = response.text

    assert response.status_code == 200
    assert collector.calls == 1
    assert response.headers["cache-control"] == "no-store"
    for text in ("Pronto", "Problema encontrado", "Não aplicável", "Não verificado"):
        assert text in html
    # dependency, stable code, query time and practical guidance per check
    assert "Montagem · FFmpeg" in html
    assert "CONTENT_READINESS_FFMPEG_MISSING" in html
    assert "Executável não encontrado. Confira CONTENT_FFMPEG_PATH." in html
    assert "CONTENT_READINESS_VISUAL_INPUTS_DEFERRED" in html
    assert "2026-10-09 15:30 UTC" in html
    assert 'datetime="2026-10-09T15:30:07+00:00"' in html
    assert "Espaço livre" in html and "5,0 GB" in html
    assert "1 problema encontrado" in html
    assert "Verificar novamente" in html


async def test_page_is_accessible_without_javascript_or_color_alone(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with _client(session_factory, _Collector()) as client:
        response = await client.post(
            _URL, content=f"csrf_token={_TOKEN}", headers={**_BROWSER, **_FORM},
        )
    html = response.text

    assert 'lang="pt' in html
    assert 'role="note"' in html and 'role="status"' in html
    assert 'aria-labelledby="checks-title"' in html and 'id="checks-title"' in html
    assert html.count("<h3>") == 5  # one heading per dependency
    # every state is icon + text; the icon is decorative
    assert html.count('class="icon" aria-hidden="true"') >= 5
    assert "badge-danger" in html and "badge-success" in html and "badge-warning" in html
    assert "<time datetime=" in html
    assert " hx-" not in html  # plain HTML form: nothing here depends on JavaScript
    assert "aria-current=\"page\"" in html


async def test_error_pages_and_report_never_expose_private_details(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async def failing() -> ReadinessReport:
        raise RuntimeError(_PRIVATE)

    application = create_app(csrf_token=_TOKEN, readiness_collector=failing)

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application, raise_app_exceptions=False),
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.post(
            _URL, content=f"csrf_token={_TOKEN}", headers={**_BROWSER, **_FORM},
        )

    assert response.status_code == 500
    assert _PRIVATE not in response.text and "secret-models" not in response.text


@pytest.mark.parametrize(
    ("body", "headers", "status"),
    [
        ("csrf_token=wrong", _FORM, 403),
        ("", _FORM, 400),
        ("csrf_token=" + _TOKEN + "&extra=1", _FORM, 400),
        ("csrf_token=" + _TOKEN, {"Content-Type": "text/plain"}, 415),
    ],
)
async def test_post_rejects_missing_or_invalid_form_without_probing(
    session_factory: async_sessionmaker[AsyncSession],
    body: str,
    headers: dict[str, str],
    status: int,
) -> None:
    collector = _Collector()
    async with _client(session_factory, collector) as client:
        response = await client.post(_URL, content=body, headers={**_BROWSER, **headers})

    assert response.status_code == status
    assert collector.calls == 0


async def test_invalid_csrf_explains_the_refusal_in_portuguese(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with _client(session_factory, _Collector()) as client:
        response = await client.post(
            _URL, content="csrf_token=wrong", headers={**_BROWSER, **_FORM},
        )

    assert response.status_code == 403
    assert "Sessão do painel expirada" in response.text


async def test_overlapping_requests_are_refused_instead_of_stacking_probes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    release = asyncio.Event()
    started = asyncio.Event()
    calls = 0

    async def slow() -> ReadinessReport:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return _report()

    async with _client(session_factory, slow) as client:
        first = asyncio.create_task(client.post(
            _URL, content=f"csrf_token={_TOKEN}", headers={**_BROWSER, **_FORM},
        ))
        await started.wait()
        second = await client.post(
            _URL, content=f"csrf_token={_TOKEN}", headers={**_BROWSER, **_FORM},
        )
        release.set()
        first_response = await first

    assert second.status_code == 409
    assert "Verificação em andamento" in second.text
    assert "readiness in progress" not in second.text.split("<code")[0]
    assert first_response.status_code == 200
    assert calls == 1


async def test_diagnostic_does_not_touch_runs_approvals_or_dispatch(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_dashboard(session_factory)

    async def counts() -> tuple[int, int]:
        async with session_factory() as session:
            runs = await session.scalar(select(func.count()).select_from(Run))
            approvals = await session.scalar(select(func.count()).select_from(Approval))
            return int(runs or 0), int(approvals or 0)

    before = await counts()
    published: list[object] = []
    application = create_app(
        csrf_token=_TOKEN,
        readiness_collector=_Collector(),
        dispatch_publisher=lambda *args: published.append(args),
    )

    async def override_session() -> AsyncGenerator[AsyncSession, None]:
        async with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = override_session
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://127.0.0.1",
    ) as client:
        response = await client.post(
            _URL, content=f"csrf_token={_TOKEN}", headers={**_BROWSER, **_FORM},
        )

    assert response.status_code == 200
    assert await counts() == before
    assert published == []


async def test_automations_page_offers_the_action_for_content_engine(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _seed_dashboard(session_factory)
    async with _client(session_factory, _Collector()) as client:
        response = await client.get("/automations", headers=_BROWSER)
    html = response.text

    assert response.status_code == 200
    assert html.count(f'action="{_URL}"') >= 1
    assert "Verificar preparação" in html
    assert f'name="csrf_token" value="{_TOKEN}"' in html


async def test_real_collector_end_to_end_with_mocked_services(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: Path,
) -> None:
    storage = tmp_path / "storage"
    storage.mkdir()
    configuration = Settings(  # type: ignore[call-arg]
        _env_file=None,
        storage_root=str(storage),
        content_caption_backend="faster_whisper_small_quality_test",
        content_caption_model_path=str(tmp_path / "private-model-dir"),
        content_tts_backend="kokoro_quality_test",
    )
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"data": [{"id": "other:1b"}]}),
    )
    collector = partial(collect_content_readiness, configuration, transport=transport)

    async with _client(session_factory, collector) as client:
        response = await client.post(
            _URL, content=f"csrf_token={_TOKEN}", headers={**_BROWSER, **_FORM},
        )
    html = response.text

    assert response.status_code == 200
    assert "CONTENT_READINESS_OLLAMA_MISSING" in html
    assert "O Ollama respondeu, mas o modelo configurado não está na lista." in html
    assert "CONTENT_READINESS_CAPTION_MODEL_MISSING" in html
    assert "CONTENT_READINESS_STORAGE_OK" in html
    assert "Não aplicável" in html  # FFmpeg and visuals are disabled
    assert "private-model-dir" not in html
    assert str(storage) not in html
    assert "127.0.0.1:11434" not in html
