from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


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
    database_url: str = "sqlite+aiosqlite:///./automation_foundry.db"

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

    @property
    def is_development(self) -> bool:
        return self.environment == "development"


@lru_cache
def get_settings() -> Settings:
    """Retorna o singleton de Settings (cacheado por lru_cache)."""
    return Settings()


# Instância importável diretamente: `from src.core.config import settings`
settings: Settings = get_settings()
