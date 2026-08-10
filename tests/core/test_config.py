"""Tests for safe, local-first configuration defaults."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.core.config import Settings


def test_defaults_use_automation_foundry_identity_and_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default infrastructure endpoints stay local and use the current project name."""
    for variable in (
        "DASHBOARD_HOST",
        "DASHBOARD_PORT",
        "DATABASE_URL",
        "OLLAMA_BASE_URL",
        "REDDIT_USER_AGENT",
        "REDIS_URL",
    ):
        monkeypatch.delenv(variable, raising=False)

    settings = Settings(_env_file=None)

    assert settings.database_url == "sqlite+aiosqlite:///./automation_foundry.db"
    assert settings.dashboard_host == "127.0.0.1"
    assert settings.dashboard_port == 8000
    assert settings.ollama_base_url == "http://127.0.0.1:11434/v1"
    assert settings.reddit_user_agent == "automation-foundry/0.1"
    assert settings.redis_url == "redis://127.0.0.1:6379/0"


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.20", "example.com"])
def test_dashboard_rejects_non_loopback_hosts(host: str) -> None:
    with pytest.raises(ValidationError, match="loopback"):
        Settings(_env_file=None, dashboard_host=host)


@pytest.mark.parametrize("host", ["localhost", "127.0.0.2", "::1"])
def test_dashboard_accepts_loopback_hosts(host: str) -> None:
    settings = Settings(_env_file=None, dashboard_host=host)

    assert settings.dashboard_host == host


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1"])
def test_postgresql_accepts_only_asyncpg_on_loopback(host: str) -> None:
    rendered_host = f"[{host}]" if ":" in host else host
    database_url = (
        f"postgresql+asyncpg://foundry:secret@{rendered_host}:5432/automation_foundry"
    )

    settings = Settings(_env_file=None, database_url=database_url)

    assert settings.database_url == database_url


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql+psycopg://foundry:secret@127.0.0.1/automation_foundry",
        "postgresql+asyncpg://foundry:secret@192.168.1.20/automation_foundry",
        "mysql+aiomysql://foundry:secret@127.0.0.1/automation_foundry",
    ],
)
def test_database_rejects_unsupported_or_non_loopback_urls(database_url: str) -> None:
    with pytest.raises(ValidationError, match="database_url|loopback"):
        Settings(_env_file=None, database_url=database_url)


def test_database_pool_limits_are_validated() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_pool_size=0)


def test_health_capacity_thresholds_must_be_ordered() -> None:
    with pytest.raises(ValidationError, match="warning threshold"):
        Settings(
            _env_file=None,
            health_warning_percent=95,
            health_critical_percent=90,
        )


def test_health_alert_thresholds_are_bounded() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, health_alert_failure_threshold=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, health_alert_recovery_threshold=101)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, celery_health_tick_seconds=9)


def test_runtime_timeouts_are_bounded() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, runtime_startup_timeout_seconds=9)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, runtime_shutdown_timeout_seconds=4)

    settings = Settings(
        _env_file=None,
        runtime_startup_timeout_seconds=10,
        runtime_shutdown_timeout_seconds=5,
    )

    assert settings.runtime_startup_timeout_seconds == 10
    assert settings.runtime_shutdown_timeout_seconds == 5
