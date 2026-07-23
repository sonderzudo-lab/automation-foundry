"""
SQLAlchemy 2.0 models — estilo declarativo tipado (Mapped / mapped_column).

Tabelas implementadas (Seção 4 do ARQUITETURA.md):
  channels · videos · jobs · metrics · costs · topics · alerts · ab_variants

Nota sobre lazy loading async:
  Em contexto async, o lazy loading padrão do SQLAlchemy lança MissingGreenlet.
  Sempre use selectinload() / joinedload() ao consultar relacionamentos:

      result = await session.execute(
          select(Channel).options(selectinload(Channel.videos))
      )
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.database import Base

# ── Helper ────────────────────────────────────────────────────────────────────


def _utcnow() -> datetime:
    """Retorna datetime naive em UTC (sem tzinfo) para compatibilidade com SQLite."""
    return datetime.now(UTC).replace(tzinfo=None)


# ── Channel ───────────────────────────────────────────────────────────────────


class Channel(Base):
    """Canal do YouTube ou TikTok gerenciado pelo sistema."""

    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    niche: Mapped[str] = mapped_column(Text, nullable=False)
    platform: Mapped[str] = mapped_column(String(20), nullable=False)  # 'youtube' | 'tiktok'
    language: Mapped[str] = mapped_column(String(10), nullable=False)  # 'pt-BR' | 'en'
    persona: Mapped[str] = mapped_column(Text, nullable=False)
    cloud_project_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Armazenado criptografado via encryption_key (Fernet). Nunca leia raw.
    oauth_refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)

    # Relacionamentos
    videos: Mapped[list[Video]] = relationship(
        "Video", back_populates="channel", cascade="all, delete-orphan"
    )
    topics: Mapped[list[Topic]] = relationship(
        "Topic", back_populates="channel"
    )
    alerts: Mapped[list[Alert]] = relationship(
        "Alert", back_populates="channel", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Channel id={self.id} name={self.name!r} platform={self.platform!r}>"


# ── Video ─────────────────────────────────────────────────────────────────────


class Video(Base):
    """Vídeo em qualquer estágio do pipeline (draft → published)."""

    __tablename__ = "videos"
    __table_args__ = (
        Index("ix_videos_channel_status", "channel_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Ângulo editorial aprovado pelo humano
    niche_angle: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 'google_trends' | 'youtube' | 'reddit' | 'competitor' | 'manual'
    topic_source: Mapped[str | None] = mapped_column(String(50), nullable=True)
    format: Mapped[str] = mapped_column(String(10), nullable=False)  # 'short' | 'long'
    script_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    audio_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    video_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    thumbnail_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    # ID atribuído pela plataforma após upload
    platform_video_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    # True quando voz/imagem sintética exige rótulo de IA
    ai_disclosure: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Score calculado por D3; > 0.85 bloqueia publicação automática
    similarity_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # draft | queued | rendering | ready | scheduled | published | failed
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="draft")
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)

    # Relacionamentos
    channel: Mapped[Channel] = relationship("Channel", back_populates="videos")
    jobs: Mapped[list[Job]] = relationship(
        "Job", back_populates="video", cascade="all, delete-orphan"
    )
    metrics: Mapped[list[Metric]] = relationship(
        "Metric", back_populates="video", cascade="all, delete-orphan"
    )
    costs: Mapped[list[Cost]] = relationship(
        "Cost", back_populates="video", cascade="all, delete-orphan"
    )
    ab_variants: Mapped[list[ABVariant]] = relationship(
        "ABVariant", back_populates="video", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Video id={self.id} status={self.status!r} format={self.format!r}>"


# ── Job ───────────────────────────────────────────────────────────────────────


class Job(Base):
    """Rastreamento fino de cada etapa do pipeline por vídeo."""

    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_video_stage", "video_id", "stage"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id"), nullable=False)
    # script | tts | visual | caption | render | upload
    stage: Mapped[str] = mapped_column(String(20), nullable=False)
    # queued | running | done | failed
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    worker: Mapped[str | None] = mapped_column(String(100), nullable=True)
    celery_task_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)
    retries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Relacionamento
    video: Mapped[Video] = relationship("Video", back_populates="jobs")

    def __repr__(self) -> str:
        return f"<Job id={self.id} stage={self.stage!r} status={self.status!r}>"


# ── Metric ────────────────────────────────────────────────────────────────────


class Metric(Base):
    """Métricas do YouTube Analytics — uma linha por vídeo por dia."""

    __tablename__ = "metrics"
    __table_args__ = (
        Index("ix_metrics_video_date", "video_id", "date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id"), nullable=False)
    date: Mapped[datetime] = mapped_column(Date, nullable=False)
    views: Mapped[int | None] = mapped_column(Integer, nullable=True)
    watch_time_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    avg_view_duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    ctr: Mapped[float | None] = mapped_column(Float, nullable=True)
    estimated_revenue: Mapped[float | None] = mapped_column(Float, nullable=True)
    cpm: Mapped[float | None] = mapped_column(Float, nullable=True)
    monetized_playbacks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    subscribers_gained: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Dados do YouTube Analytics ficam definitivos ~48h após o dia — não trate parcial como final
    collected_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)

    # Relacionamento
    video: Mapped[Video] = relationship("Video", back_populates="metrics")

    def __repr__(self) -> str:
        return f"<Metric id={self.id} video_id={self.video_id} date={self.date}>"


# ── Cost ──────────────────────────────────────────────────────────────────────


class Cost(Base):
    """Custo de produção por vídeo (GPU + energia + APIs). Usado em E2."""

    __tablename__ = "costs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id"), nullable=False)
    gpu_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    kwh_estimated: Mapped[float | None] = mapped_column(Float, nullable=True)
    api_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    computed_cost: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)

    # Relacionamento
    video: Mapped[Video] = relationship("Video", back_populates="costs")

    def __repr__(self) -> str:
        return f"<Cost id={self.id} video_id={self.video_id} computed_cost={self.computed_cost}>"


# ── Topic ─────────────────────────────────────────────────────────────────────


class Topic(Base):
    """Pauta sugerida pelo sistema (D1/D2). Requer aprovação humana para virar vídeo."""

    __tablename__ = "topics"
    __table_args__ = (
        Index("ix_topics_channel_status", "channel_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # nullable: pauta pode ser geral do nicho, sem canal específico
    channel_id: Mapped[int | None] = mapped_column(
        ForeignKey("channels.id"), nullable=True
    )
    # 'google_trends' | 'youtube' | 'reddit' | 'competitor'
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    raw_data: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    potential_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 'new' | 'approved' | 'rejected' | 'used'
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="new")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)

    # Relacionamento (opcional — pauta pode não ter canal)
    channel: Mapped[Channel | None] = relationship("Channel", back_populates="topics")

    def __repr__(self) -> str:
        return f"<Topic id={self.id} source={self.source!r} status={self.status!r}>"


# ── Alert ─────────────────────────────────────────────────────────────────────


class Alert(Base):
    """Alertas de saúde do canal (F1): strikes, queda de RPM, jobs falhos, etc."""

    __tablename__ = "alerts"
    __table_args__ = (
        Index("ix_alerts_channel_acknowledged", "channel_id", "acknowledged"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), nullable=False)
    # 'strike' | 'rpm_drop' | 'demonetized' | 'job_failed' | 'similarity_high'
    type: Mapped[str] = mapped_column(String(30), nullable=False)
    # 'info' | 'warning' | 'critical'
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    acknowledged: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)

    # Relacionamento
    channel: Mapped[Channel] = relationship("Channel", back_populates="alerts")

    def __repr__(self) -> str:
        return (
            f"<Alert id={self.id} type={self.type!r} "
            f"severity={self.severity!r} acknowledged={self.acknowledged}>"
        )


# ── ABVariant ─────────────────────────────────────────────────────────────────


class ABVariant(Base):
    """Variante de thumbnail ou título para teste A/B (E3)."""

    __tablename__ = "ab_variants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id"), nullable=False)
    # 'thumbnail' | 'title'
    variant_type: Mapped[str] = mapped_column(String(20), nullable=False)
    # caminho do arquivo de thumbnail OU texto do título alternativo
    content: Mapped[str] = mapped_column(Text, nullable=False)
    impressions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    clicks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ctr: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_winner: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)

    # Relacionamento
    video: Mapped[Video] = relationship("Video", back_populates="ab_variants")

    def __repr__(self) -> str:
        return (
            f"<ABVariant id={self.id} type={self.variant_type!r} "
            f"is_winner={self.is_winner}>"
        )
