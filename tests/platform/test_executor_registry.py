"""Tests for fail-closed local automation executor registration."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.database import Base
from src.platform.executor_registry import (
    AutomationExecutorNotFoundError,
    list_automation_executors,
    parse_registered_manual_input,
    start_registered_automation,
)


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as database_session:
        yield database_session
    await engine.dispose()


async def test_registry_dispatches_only_declared_manual_executor(
    session: AsyncSession,
) -> None:
    executors = list_automation_executors()
    assert [executor.slug for executor in executors] == [
        "content-engine",
        "platform-smoke",
    ]
    content_executor, smoke_executor = executors
    assert content_executor.queue.value == "gpu"
    assert content_executor.runs_in_background is True
    assert [field.name for field in content_executor.manual_input_fields] == [
        "topic",
        "persona",
        "format",
        "recent_openings",
    ]
    assert smoke_executor.queue.value == "io"
    assert smoke_executor.runs_in_background is False
    result = await start_registered_automation(
        session,
        slug="platform-smoke",
        idempotency_key="registry-smoke-001",
    )
    assert result.run_status == "succeeded"

    with pytest.raises(AutomationExecutorNotFoundError, match="no registered"):
        await start_registered_automation(
            session,
            slug="unknown-module",
            idempotency_key="must-not-run",
        )


def test_registry_validates_executor_owned_manual_input() -> None:
    payload = parse_registered_manual_input(
        slug="content-engine",
        values={
            "topic": "Sono e memória",
            "persona": "Ciência acessível",
            "format": "short",
            "recent_openings": "",
        },
    )
    assert payload["format"] == "short"
    assert payload["recent_openings"] == []

    with pytest.raises(ValueError, match="do not match"):
        parse_registered_manual_input(
            slug="content-engine",
            values={"topic": "campos incompletos"},
        )
