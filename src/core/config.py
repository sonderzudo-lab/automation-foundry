from __future__ import annotations

import ipaddress
from functools import lru_cache

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

    @property
    def is_development(self) -> bool:
        return self.environment == "development"


@lru_cache
def get_settings() -> Settings:
    """Retorna o singleton de Settings (cacheado por lru_cache)."""
    return Settings()


# Instância importável diretamente: `from src.core.config import settings`
settings: Settings = get_settings()
