"""Smoke test for the platform-only Alembic migration path."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic.config import Config

from alembic import command


def test_upgrade_head_creates_only_platform_kernel_tables(tmp_path: Path) -> None:
    database_path = tmp_path / "migration-smoke.db"
    config = Config("alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
    )

    command.upgrade(config, "head")
    command.check(config)

    with sqlite3.connect(database_path) as connection:
        table_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

    assert {
        "alembic_version",
        "artifacts",
        "approval_events",
        "approvals",
        "automations",
        "control_events",
        "runs",
        "run_transitions",
        "step_runs",
        "step_run_transitions",
    }.issubset(table_names)
    assert "channels" not in table_names


def test_approval_migration_downgrade_and_reupgrade(tmp_path: Path) -> None:
    database_path = tmp_path / "approval-roundtrip.db"
    config = Config("alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
    )

    command.upgrade(config, "head")
    command.downgrade(config, "20260723_0003")
    with sqlite3.connect(database_path) as connection:
        downgraded_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "approvals" not in downgraded_tables
    assert "approval_events" not in downgraded_tables

    command.upgrade(config, "head")
    command.check(config)


def test_artifact_migration_downgrade_and_reupgrade(tmp_path: Path) -> None:
    database_path = tmp_path / "artifact-roundtrip.db"
    config = Config("alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
    )

    command.upgrade(config, "head")
    command.downgrade(config, "20260723_0004")
    with sqlite3.connect(database_path) as connection:
        downgraded_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "artifacts" not in downgraded_tables
    assert "approvals" in downgraded_tables

    command.upgrade(config, "head")
    command.check(config)
