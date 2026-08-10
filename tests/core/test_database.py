"""Tests for backend-specific async database engine configuration."""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from src.core import database
from src.core.config import Settings


def test_sqlite_engine_keeps_single_process_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured_engine = Mock(spec=AsyncEngine)
    create_engine = Mock(return_value=configured_engine)
    monkeypatch.setattr(database, "create_async_engine", create_engine)
    settings = Settings(_env_file=None, database_echo=True)

    result = database.build_engine(settings)

    assert result is configured_engine
    create_engine.assert_called_once_with(
        "sqlite+aiosqlite:///./automation_foundry.db",
        echo=True,
        connect_args={"check_same_thread": False},
    )


def test_postgresql_engine_has_bounded_pool_and_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured_engine = Mock(spec=AsyncEngine)
    create_engine = Mock(return_value=configured_engine)
    monkeypatch.setattr(database, "create_async_engine", create_engine)
    database_url = (
        "postgresql+asyncpg://foundry:private@127.0.0.1:5432/automation_foundry"
    )
    settings = Settings(
        _env_file=None,
        database_url=database_url,
        database_pool_size=7,
        database_max_overflow=3,
        database_pool_timeout_seconds=12,
        database_connect_timeout_seconds=4,
        database_command_timeout_seconds=90,
    )

    result = database.build_engine(settings)

    assert result is configured_engine
    create_engine.assert_called_once_with(
        database_url,
        echo=False,
        pool_pre_ping=True,
        pool_size=7,
        max_overflow=3,
        pool_timeout=12.0,
        connect_args={
            "timeout": 4.0,
            "command_timeout": 90.0,
            "server_settings": {"application_name": "automation-foundry"},
        },
    )
