"""
Testes dos models SQLAlchemy — banco SQLite em memória.

Valida:
- Criação de todas as tabelas (create_all sem erro).
- Inserção e leitura de Channel + Video relacionados.
- Relacionamento channel.videos carregado corretamente via selectinload.
- Defaults automáticos (status, created_at, ai_disclosure).
- Cascade: deletar channel remove vídeos filhos.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from src.core.database import Base
from src.core.models import Alert, Channel, Job, Metric, Topic, Video


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
async def session() -> AsyncSession:
    """Sessão async sobre banco SQLite em memória — isolada por teste."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as s:
        yield s

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

    await engine.dispose()


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_channel(**kwargs: object) -> Channel:
    defaults = {
        "name": "Tech Brasil",
        "niche": "tecnologia",
        "platform": "youtube",
        "language": "pt-BR",
        "persona": "Apresentador técnico e direto ao ponto",
    }
    defaults.update(kwargs)
    return Channel(**defaults)  # type: ignore[arg-type]


def _make_video(channel_id: int, **kwargs: object) -> Video:
    defaults = {
        "channel_id": channel_id,
        "title": "5 dicas de produtividade com IA",
        "format": "short",
    }
    defaults.update(kwargs)
    return Video(**defaults)  # type: ignore[arg-type]


# ── Testes ────────────────────────────────────────────────────────────────────


async def test_create_all_tables(session: AsyncSession) -> None:
    """Verifica que create_all cria todas as tabelas sem erro (fixture já faz isso)."""
    # Se chegou aqui, create_all não lançou exceção — tabelas existem.
    result = await session.execute(select(Channel))
    assert result.scalars().all() == []


async def test_channel_defaults(session: AsyncSession) -> None:
    """Status e created_at devem ser preenchidos automaticamente."""
    channel = _make_channel()
    session.add(channel)
    await session.commit()

    result = await session.execute(select(Channel).where(Channel.id == channel.id))
    fetched = result.scalar_one()

    assert fetched.status == "active"
    assert fetched.created_at is not None
    assert fetched.oauth_refresh_token is None


async def test_channel_video_relationship(session: AsyncSession) -> None:
    """Channel.videos deve conter o vídeo inserido via selectinload."""
    channel = _make_channel()
    session.add(channel)
    await session.flush()  # popula channel.id sem fechar a sessão

    video = _make_video(channel.id)
    session.add(video)
    await session.commit()

    # selectinload é necessário em contexto async para evitar MissingGreenlet
    result = await session.execute(
        select(Channel)
        .where(Channel.id == channel.id)
        .options(selectinload(Channel.videos))
    )
    fetched = result.scalar_one()

    assert fetched.name == "Tech Brasil"
    assert len(fetched.videos) == 1
    assert fetched.videos[0].title == "5 dicas de produtividade com IA"
    assert fetched.videos[0].format == "short"
    assert fetched.videos[0].channel_id == channel.id


async def test_video_defaults(session: AsyncSession) -> None:
    """Video deve ter status='draft', ai_disclosure=True e created_at preenchidos."""
    channel = _make_channel()
    session.add(channel)
    await session.flush()

    video = _make_video(channel.id)
    session.add(video)
    await session.commit()

    result = await session.execute(select(Video).where(Video.id == video.id))
    fetched = result.scalar_one()

    assert fetched.status == "draft"
    assert fetched.ai_disclosure is True
    assert fetched.similarity_score is None
    assert fetched.created_at is not None


async def test_video_back_populates_channel(session: AsyncSession) -> None:
    """Video.channel deve apontar para o canal pai via selectinload."""
    channel = _make_channel()
    session.add(channel)
    await session.flush()

    video = _make_video(channel.id)
    session.add(video)
    await session.commit()

    result = await session.execute(
        select(Video)
        .where(Video.id == video.id)
        .options(selectinload(Video.channel))
    )
    fetched_video = result.scalar_one()

    assert fetched_video.channel.name == "Tech Brasil"
    assert fetched_video.channel.platform == "youtube"


async def test_job_inserted_and_linked(session: AsyncSession) -> None:
    """Job deve ser inserido e aparecer em video.jobs."""
    channel = _make_channel()
    session.add(channel)
    await session.flush()

    video = _make_video(channel.id)
    session.add(video)
    await session.flush()

    job = Job(video_id=video.id, stage="script", status="queued")
    session.add(job)
    await session.commit()

    result = await session.execute(
        select(Video)
        .where(Video.id == video.id)
        .options(selectinload(Video.jobs))
    )
    fetched_video = result.scalar_one()

    assert len(fetched_video.jobs) == 1
    assert fetched_video.jobs[0].stage == "script"
    assert fetched_video.jobs[0].status == "queued"
    assert fetched_video.jobs[0].retries == 0


async def test_topic_nullable_channel(session: AsyncSession) -> None:
    """Topic sem channel_id (pauta geral do nicho) deve ser aceita."""
    topic = Topic(source="google_trends", title="IA generativa 2026", channel_id=None)
    session.add(topic)
    await session.commit()

    result = await session.execute(select(Topic).where(Topic.id == topic.id))
    fetched = result.scalar_one()

    assert fetched.channel_id is None
    assert fetched.status == "new"
    assert fetched.source == "google_trends"


async def test_alert_acknowledged_default(session: AsyncSession) -> None:
    """Alert deve ter acknowledged=False por padrão."""
    channel = _make_channel()
    session.add(channel)
    await session.flush()

    alert = Alert(
        channel_id=channel.id,
        type="rpm_drop",
        severity="warning",
        message="RPM caiu 30% em 24h",
    )
    session.add(alert)
    await session.commit()

    result = await session.execute(select(Alert).where(Alert.id == alert.id))
    fetched = result.scalar_one()

    assert fetched.acknowledged is False
    assert fetched.severity == "warning"


async def test_cascade_delete_channel_removes_videos(session: AsyncSession) -> None:
    """Deletar um channel deve remover seus vídeos (cascade all, delete-orphan)."""
    channel = _make_channel()
    session.add(channel)
    await session.flush()

    video = _make_video(channel.id)
    session.add(video)
    await session.commit()

    video_id = video.id

    await session.delete(channel)
    await session.commit()

    result = await session.execute(select(Video).where(Video.id == video_id))
    assert result.scalar_one_or_none() is None
