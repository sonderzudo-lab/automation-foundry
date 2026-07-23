"""Tests for safe, local-first configuration defaults."""

from __future__ import annotations

import pytest

from src.core.config import Settings


def test_defaults_use_automation_foundry_identity_and_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default infrastructure endpoints stay local and use the current project name."""
    for variable in ("DATABASE_URL", "OLLAMA_BASE_URL", "REDDIT_USER_AGENT", "REDIS_URL"):
        monkeypatch.delenv(variable, raising=False)

    settings = Settings(_env_file=None)

    assert settings.database_url == "sqlite+aiosqlite:///./automation_foundry.db"
    assert settings.ollama_base_url == "http://127.0.0.1:11434/v1"
    assert settings.reddit_user_agent == "automation-foundry/0.1"
    assert settings.redis_url == "redis://127.0.0.1:6379/0"
