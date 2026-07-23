"""Alembic environment for the shared platform schema."""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from src.core.config import settings
from src.platform import models as platform_models

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = platform_models.Base.metadata
_PLATFORM_TABLES = frozenset(
    {
        "artifacts",
        "automations",
        "approval_events",
        "approvals",
        "control_events",
        "runs",
        "run_transitions",
        "step_runs",
        "step_run_transitions",
    }
)


def _include_object(
    _object: object,
    name: str | None,
    type_: str,
    _reflected: bool,
    _compare_to: object | None,
) -> bool:
    if type_ == "table":
        return name in _PLATFORM_TABLES
    return True


def _database_url() -> str:
    configured = config.get_main_option("sqlalchemy.url").strip()
    return configured or settings.database_url


def run_migrations_offline() -> None:
    """Run migrations without creating an Engine."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=_include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def _run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        include_object=_include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations with an async SQLAlchemy engine."""
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _database_url()
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
