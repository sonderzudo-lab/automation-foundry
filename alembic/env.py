"""Alembic environment for the shared platform and module schemas."""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from src.core import models as content_engine_models
from src.core.config import settings
from src.platform import models as platform_models

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = platform_models.Base.metadata
_MIGRATED_TABLES = frozenset(
    {
        "ab_variants",
        "alerts",
        "artifacts",
        "automations",
        "approval_events",
        "approvals",
        "channels",
        "connector_observations",
        "control_events",
        "costs",
        "experiment_events",
        "experiments",
        "health_conditions",
        "jobs",
        "ledger_entries",
        "metrics",
        "metric_points",
        "platform_alert_events",
        "platform_alerts",
        "runs",
        "run_transitions",
        "run_dispatches",
        "run_dispatch_events",
        "schedule_events",
        "schedule_occurrences",
        "schedules",
        "step_runs",
        "step_run_transitions",
        "topics",
        "videos",
    }
)

if content_engine_models.Base.metadata is not target_metadata:
    raise RuntimeError("Content Engine and platform models must share one metadata registry")


def _include_object(
    _object: object,
    name: str | None,
    type_: str,
    _reflected: bool,
    _compare_to: object | None,
) -> bool:
    if type_ == "table":
        return name in _MIGRATED_TABLES
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
