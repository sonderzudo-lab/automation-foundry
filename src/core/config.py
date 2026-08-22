from __future__ import annotations

import ipaddress
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    """
    Configurações centrais do projeto, lidas do arquivo .env (ou variáveis de ambiente).

    env_ignore_empty=True: variáveis de ambiente com valor vazio ("") são tratadas
    como ausentes — campos Optional recebem None em vez de "".
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        env_ignore_empty=True,
    )

    # ── Infra ─────────────────────────────────────────────────────────────────

    redis_url: str = "redis://127.0.0.1:6379/0"
    celery_result_backend_url: str = "redis://127.0.0.1:6379/1"
    celery_soft_time_limit_seconds: int = Field(default=300, ge=1, le=86400)
    celery_hard_time_limit_seconds: int = Field(default=330, ge=2, le=86400)
    celery_visibility_timeout_seconds: int = Field(default=3600, ge=60, le=86400)
    celery_result_expires_seconds: int = Field(default=300, ge=30, le=86400)
    celery_dispatch_lease_seconds: int = Field(default=600, ge=3, le=86400)
    celery_schedule_tick_seconds: int = Field(default=30, ge=5, le=3600)
    celery_schedule_batch_size: int = Field(default=100, ge=1, le=1000)
    database_url: str = "sqlite+aiosqlite:///./automation_foundry.db"
    database_pool_size: int = Field(default=5, ge=1, le=50)
    database_max_overflow: int = Field(default=5, ge=0, le=50)
    database_pool_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    database_connect_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    database_command_timeout_seconds: float = Field(default=120.0, gt=0, le=3600)
    database_echo: bool = False
    storage_root: str = "./storage"
    # Limite fail-closed do inventário de retenção. Um storage maior que isso
    # interrompe a varredura em vez de produzir um resultado parcial.
    retention_inventory_max_files: int = Field(default=200_000, ge=1, le=5_000_000)
    retention_inventory_max_items: int = Field(default=200, ge=1, le=100_000)
    # Único caminho do projeto que apaga dados do operador. Permanece desabilitado
    # por default; mesmo habilitado exige approval humana, runtime parado e backup.
    retention_purge_enabled: bool = False
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = Field(default=8000, ge=1, le=65535)
    health_probe_timeout_seconds: float = Field(default=1.5, gt=0, le=10)
    health_warning_percent: float = Field(default=85.0, ge=1, le=100)
    health_critical_percent: float = Field(default=95.0, ge=1, le=100)
    health_alert_failure_threshold: int = Field(default=2, ge=1, le=100)
    health_alert_recovery_threshold: int = Field(default=2, ge=1, le=100)
    celery_health_tick_seconds: int = Field(default=60, ge=10, le=3600)
    runtime_startup_timeout_seconds: int = Field(default=90, ge=10, le=600)
    runtime_shutdown_timeout_seconds: int = Field(default=30, ge=5, le=300)

    # ── Ollama (LLM local) ────────────────────────────────────────────────────

    ollama_base_url: str = "http://127.0.0.1:11434/v1"
    ollama_model: str = "qwen3:14b"
    ollama_timeout: int = 120

    # ── Content Engine TTS ───────────────────────────────────────────────────

    # Kokoro permanece apenas em modo de teste local até a qualidade PT-BR e o
    # risco residual de proveniência por voz serem aceitos explicitamente.
    content_tts_backend: Literal["disabled", "kokoro_quality_test"] = "disabled"
    content_tts_voice_id: str = Field(default="pf_dora", min_length=1, max_length=100)
    content_tts_language_code: str = Field(default="p", min_length=1, max_length=20)
    # FLUX.1 permanece bloqueado. O único modo opt-in importa PNGs escolhidos
    # pelo operador e exige manifesto de direitos + SHA-256 por arquivo.
    content_visual_backend: Literal["disabled", "local_assets_quality_test"] = (
        "disabled"
    )
    content_visual_import_root: str = Field(
        default="./imports/content-visuals",
        min_length=1,
        max_length=1000,
    )
    content_visual_manifest_path: str = Field(
        default="manifest.json",
        min_length=1,
        max_length=1000,
    )
    # O modo opt-in estima timings a partir do texto aprovado; não transcreve o áudio.
    content_caption_backend: Literal[
        "disabled",
        "approved_text_timing_quality_test",
    ] = "disabled"
    # Diagnóstico local somente leitura do alinhamento A4. Ele mede a energia do
    # WAV aprovado contra os eventos do ASS publicado; não bloqueia a run, não
    # regenera legenda e não consulta nenhum serviço externo.
    content_caption_alignment_min_speech_coverage: float = Field(
        default=0.90, gt=0, le=1
    )
    content_caption_alignment_max_outside_speech: float = Field(
        default=0.25, ge=0, le=1
    )
    content_caption_alignment_tolerance_seconds: float = Field(
        default=0.50, gt=0, le=30
    )
    # A5 aceita adapters injetados em testes; o build FFmpeg local ainda não foi auditado.
    content_assembly_backend: Literal["disabled", "ffmpeg_quality_test"] = "disabled"
    content_ffmpeg_path: str = Field(default="ffmpeg", min_length=1, max_length=1000)
    content_ffprobe_path: str = Field(default="ffprobe", min_length=1, max_length=1000)
    content_ffmpeg_expected_sha256: str | None = None
    content_ffprobe_expected_sha256: str | None = None
    # A6 usa comparação lexical determinística local; o escopo inicial é a automação.
    content_similarity_threshold: float = Field(default=0.85, gt=0, le=1)
    content_similarity_window: int = Field(default=20, ge=1, le=100)
    # A7 é o gate humano do vídeo final. Ele não publica nada; aprovar apenas
    # conclui a run local e registra a decisão ligada aos hashes das evidências.
    content_final_review_enabled: bool = False
    # A8 congela os PNGs A3 e exige a seleção humana de exatamente um deles.
    # Só pode ser habilitado junto com A7 e também não publica nada.
    content_thumbnail_review_enabled: bool = False

    # ── YouTube / Google APIs ─────────────────────────────────────────────────

    google_client_id: str | None = None
    google_client_secret: str | None = None
    google_cloud_project_id: str | None = None
    youtube_api_key: str | None = None

    # ── Reddit (PRAW) ─────────────────────────────────────────────────────────

    reddit_client_id: str | None = None
    reddit_client_secret: str | None = None
    reddit_user_agent: str = "automation-foundry/0.1"

    # ── Stock de imagens/vídeo ────────────────────────────────────────────────

    pexels_api_key: str | None = None
    pixabay_api_key: str | None = None

    # ── X / Twitter (Bloco H1) ────────────────────────────────────────────────

    x_api_key: str | None = None
    x_api_secret: str | None = None
    x_access_token: str | None = None
    x_access_token_secret: str | None = None
    x_bearer_token: str | None = None

    # ── Afiliados (Bloco H2) ──────────────────────────────────────────────────

    amazon_associates_tag: str | None = None
    hotmart_client_id: str | None = None
    hotmart_client_secret: str | None = None

    # ── Notificações / Alertas (Bloco F1) ────────────────────────────────────

    alert_webhook_url: str | None = None

    # ── Segurança ─────────────────────────────────────────────────────────────

    # Fernet key para criptografar oauth_refresh_token no banco.
    # Gere com: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    encryption_key: str | None = None

    # ── Feature flags ─────────────────────────────────────────────────────────

    # Só ative após auditar a Content Posting API do TikTok.
    tiktok_api_audited: bool = False

    # ── Hardware / custos (Bloco E2) ──────────────────────────────────────────

    gpu_power_watts: int = 400
    cpu_power_watts: int = 150
    energy_tariff_brl_per_kwh: float = 0.75

    # ── Ambiente ──────────────────────────────────────────────────────────────

    environment: str = "development"
    log_level: str = "INFO"

    @field_validator("database_url")
    @classmethod
    def _database_must_use_supported_local_driver(cls, value: str) -> str:
        try:
            url = make_url(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("database_url must be a valid SQLAlchemy URL") from exc

        if url.drivername == "sqlite+aiosqlite":
            return value
        if url.drivername != "postgresql+asyncpg":
            raise ValueError(
                "database_url must use sqlite+aiosqlite or postgresql+asyncpg"
            )
        if url.host is None:
            raise ValueError("PostgreSQL database_url must include a loopback host")
        host = url.host.strip()
        if host.casefold() == "localhost":
            return value
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise ValueError("PostgreSQL database_url must use loopback") from exc
        if not address.is_loopback:
            raise ValueError("PostgreSQL database_url must use loopback")
        return value

    @field_validator("dashboard_host")
    @classmethod
    def _dashboard_must_use_loopback(cls, value: str) -> str:
        host = value.strip()
        if host.casefold() == "localhost":
            return host
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise ValueError("dashboard_host must be a loopback address") from exc
        if not address.is_loopback:
            raise ValueError("dashboard_host must be a loopback address")
        return host

    @model_validator(mode="after")
    def _health_thresholds_must_be_ordered(self) -> Settings:
        if self.health_warning_percent >= self.health_critical_percent:
            raise ValueError("health warning threshold must be below critical threshold")
        return self

    @model_validator(mode="after")
    def _tts_quality_test_must_use_reviewed_pt_br_profile(self) -> Settings:
        if self.content_tts_backend != "kokoro_quality_test":
            return self
        if self.content_tts_language_code != "p":
            raise ValueError("Kokoro quality test requires language code 'p'")
        if self.content_tts_voice_id not in {"pf_dora", "pm_alex", "pm_santa"}:
            raise ValueError("Kokoro quality test requires an audited PT-BR voice ID")
        return self

    @model_validator(mode="after")
    def _ffmpeg_quality_test_must_pin_external_binaries(self) -> Settings:
        if self.content_assembly_backend != "ffmpeg_quality_test":
            return self
        for field, value in (
            ("content_ffmpeg_expected_sha256", self.content_ffmpeg_expected_sha256),
            ("content_ffprobe_expected_sha256", self.content_ffprobe_expected_sha256),
        ):
            normalized = value.strip().casefold() if isinstance(value, str) else ""
            if len(normalized) != 64 or any(
                character not in "0123456789abcdef" for character in normalized
            ):
                raise ValueError(f"{field} must pin one SHA-256 digest")
        return self

    @model_validator(mode="after")
    def _thumbnail_review_requires_final_review(self) -> Settings:
        if self.content_thumbnail_review_enabled and not self.content_final_review_enabled:
            raise ValueError("A8 thumbnail review requires A7 final review")
        return self

    @property
    def is_development(self) -> bool:
        return self.environment == "development"


@lru_cache
def get_settings() -> Settings:
    """Retorna o singleton de Settings (cacheado por lru_cache)."""
    return Settings()


# Instância importável diretamente: `from src.core.config import settings`
settings: Settings = get_settings()
