"""Tests for fail-closed local automation executor registration."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.database import Base
from src.platform.executor_registry import (
    AutomationExecutorNotFoundError,
    list_automation_executors,
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
    assert [executor.slug for executor in executors] == ["platform-smoke"]
    assert executors[0].queue.value == "io"
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
