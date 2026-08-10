"""Smoke and roundtrip tests for the managed Alembic migration path."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic.config import Config

from alembic import command


def test_upgrade_head_creates_platform_and_content_engine_tables(tmp_path: Path) -> None:
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
        "ab_variants",
        "alembic_version",
        "alerts",
        "artifacts",
        "approval_events",
        "approvals",
        "automations",
        "channels",
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
    }.issubset(table_names)


def test_health_condition_migration_downgrade_and_reupgrade(tmp_path: Path) -> None:
    database_path = tmp_path / "health-condition-roundtrip.db"
    config = Config("alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
    )

    command.upgrade(config, "head")
    command.downgrade(config, "20260810_0016")
    with sqlite3.connect(database_path) as connection:
        downgraded_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "health_conditions" not in downgraded_tables

    command.upgrade(config, "head")
    command.check(config)


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


def test_schedule_migration_downgrade_and_reupgrade(tmp_path: Path) -> None:
    database_path = tmp_path / "schedule-roundtrip.db"
    config = Config("alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
    )

    command.upgrade(config, "head")
    command.downgrade(config, "20260723_0005")
    with sqlite3.connect(database_path) as connection:
        downgraded_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "schedules" not in downgraded_tables
    assert "schedule_events" not in downgraded_tables
    assert "artifacts" in downgraded_tables

    command.upgrade(config, "head")
    command.check(config)


def test_metric_migration_downgrade_and_reupgrade(tmp_path: Path) -> None:
    database_path = tmp_path / "metric-roundtrip.db"
    config = Config("alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
    )

    command.upgrade(config, "head")
    command.downgrade(config, "20260723_0006")
    with sqlite3.connect(database_path) as connection:
        downgraded_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "metric_points" not in downgraded_tables
    assert "schedules" in downgraded_tables

    command.upgrade(config, "head")
    command.check(config)


def test_alert_migration_downgrade_and_reupgrade(tmp_path: Path) -> None:
    database_path = tmp_path / "alert-roundtrip.db"
    config = Config("alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
    )

    command.upgrade(config, "head")
    command.downgrade(config, "20260723_0007")
    with sqlite3.connect(database_path) as connection:
        downgraded_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "platform_alerts" not in downgraded_tables
    assert "platform_alert_events" not in downgraded_tables
    assert "metric_points" in downgraded_tables

    command.upgrade(config, "head")
    command.check(config)


def test_ledger_migration_downgrade_and_reupgrade(tmp_path: Path) -> None:
    database_path = tmp_path / "ledger-roundtrip.db"
    config = Config("alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
    )

    command.upgrade(config, "head")
    command.downgrade(config, "20260723_0008")
    with sqlite3.connect(database_path) as connection:
        downgraded_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "ledger_entries" not in downgraded_tables
    assert "platform_alerts" in downgraded_tables

    command.upgrade(config, "head")
    command.check(config)


def test_schedule_occurrence_migration_downgrade_and_reupgrade(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "schedule-occurrence-roundtrip.db"
    config = Config("alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
    )

    command.upgrade(config, "head")
    command.downgrade(config, "20260810_0015")
    with sqlite3.connect(database_path) as connection:
        downgraded_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "schedule_occurrences" not in downgraded_tables
    assert "run_dispatches" in downgraded_tables

    command.upgrade(config, "head")
    command.check(config)


def test_retry_lineage_migration_downgrade_and_reupgrade(tmp_path: Path) -> None:
    database_path = tmp_path / "retry-lineage-roundtrip.db"
    config = Config("alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
    )

    command.upgrade(config, "head")
    with sqlite3.connect(database_path) as connection:
        upgraded_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(runs)")
        }
    assert {"retry_of_run_id", "retry_requested_by", "retry_reason"}.issubset(
        upgraded_columns
    )

    command.downgrade(config, "20260810_0011")
    with sqlite3.connect(database_path) as connection:
        downgraded_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(runs)")
        }
    assert "retry_of_run_id" not in downgraded_columns
    assert "retry_requested_by" not in downgraded_columns
    assert "retry_reason" not in downgraded_columns

    command.upgrade(config, "head")
    command.check(config)


def test_experiment_migration_downgrade_and_reupgrade(tmp_path: Path) -> None:
    database_path = tmp_path / "experiment-roundtrip.db"
    config = Config("alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
    )

    command.upgrade(config, "head")
    with sqlite3.connect(database_path) as connection:
        run_columns = {row[1] for row in connection.execute("PRAGMA table_info(runs)")}
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "experiment_id" in run_columns
    assert {"experiments", "experiment_events"}.issubset(tables)

    command.downgrade(config, "20260810_0012")
    with sqlite3.connect(database_path) as connection:
        downgraded_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(runs)")
        }
        downgraded_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "experiment_id" not in downgraded_columns
    assert "experiments" not in downgraded_tables
    assert "experiment_events" not in downgraded_tables

    command.upgrade(config, "head")
    command.check(config)


def test_content_engine_baseline_downgrade_and_reupgrade(tmp_path: Path) -> None:
    database_path = tmp_path / "content-engine-baseline-roundtrip.db"
    config = Config("alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
    )

    command.upgrade(config, "head")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO channels (
                name, niche, platform, language, persona, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "Canal de teste",
                "tecnologia",
                "youtube",
                "pt-BR",
                "didática",
                "active",
                "2026-08-10 00:00:00",
            ),
        )
        connection.commit()
        assert connection.execute("SELECT COUNT(*) FROM channels").fetchone() == (1,)

    command.downgrade(config, "20260810_0013")
    with sqlite3.connect(database_path) as connection:
        downgraded_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {
        "ab_variants",
        "alerts",
        "channels",
        "costs",
        "jobs",
        "metrics",
        "topics",
        "videos",
    }.isdisjoint(downgraded_tables)
    assert "experiments" in downgraded_tables

    command.upgrade(config, "head")
    command.check(config)
