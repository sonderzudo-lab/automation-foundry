"""Smoke and roundtrip tests for the managed Alembic migration path."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.util import CommandError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from src.dashboard.service import load_connector_summaries
from src.platform.connector_service import record_connector_observation
from src.platform.models import (
    Automation,
    ConnectorObservation,
    ConnectorStatus,
    DataQualityStatus,
)


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


def test_automation_enabled_control_migration_roundtrip(tmp_path: Path) -> None:
    database_path = tmp_path / "automation-enabled-control-roundtrip.db"
    config = Config("alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
    )

    command.upgrade(config, "head")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO automations (slug, name, owner, enabled) "
            "VALUES ('migration-control', 'Migration Control', 'local-owner', 1)"
        )
        automation_id = connection.execute(
            "SELECT id FROM automations WHERE slug = 'migration-control'"
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO control_events "
            "(automation_id, event_type, actor, reason) VALUES (?, ?, ?, ?)",
            (
                automation_id,
                "automation_disabled",
                "local-owner",
                "migration verification",
            ),
        )
        connection.execute("DELETE FROM control_events")
        connection.commit()

    command.downgrade(config, "20260810_0017")
    with sqlite3.connect(database_path) as connection:
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'control_events'"
        ).fetchone()[0]
    assert "automation_disabled" not in table_sql

    command.upgrade(config, "head")
    command.check(config)


def test_dispatch_requeued_event_migration_roundtrip(tmp_path: Path) -> None:
    database_path = tmp_path / "dispatch-requeued-roundtrip.db"
    config = Config("alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
    )

    command.upgrade(config, "head")
    with sqlite3.connect(database_path) as connection:
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'run_dispatch_events'"
        ).fetchone()[0]
    assert "requeued" in table_sql

    command.downgrade(config, "20260810_0018")
    with sqlite3.connect(database_path) as connection:
        downgraded_sql = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'run_dispatch_events'"
        ).fetchone()[0]
    assert "requeued" not in downgraded_sql

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


def test_artifact_purge_evidence_migration_roundtrip(tmp_path: Path) -> None:
    database_path = tmp_path / "artifact-purge-roundtrip.db"
    config = Config("alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
    )

    command.upgrade(config, "head")
    with sqlite3.connect(database_path) as connection:
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'artifacts'"
        ).fetchone()[0]
        index_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'index' AND tbl_name = 'artifacts'"
            )
        }
    assert "purged_at" in table_sql
    assert "purged_by_approval_id" in table_sql
    assert "ck_artifacts_purge_evidence" in table_sql
    assert "ix_artifacts_purged_at" in index_names

    command.downgrade(config, "20260811_0019")
    with sqlite3.connect(database_path) as connection:
        downgraded_sql = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'artifacts'"
        ).fetchone()[0]
    assert "purged_at" not in downgraded_sql

    command.upgrade(config, "head")
    command.check(config)


def test_connector_observation_migration_roundtrip(tmp_path: Path) -> None:
    database_path = tmp_path / "connector-observation-roundtrip.db"
    config = Config("alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
    )

    command.upgrade(config, "head")
    with sqlite3.connect(database_path) as connection:
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'connector_observations'"
        ).fetchone()[0]
        index_names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'index' AND tbl_name = 'connector_observations'"
            )
        }
    assert "ck_connector_observations_status" in table_sql
    assert "ck_connector_observations_quality_status" in table_sql
    assert "ck_connector_observations_positive_slos" in table_sql
    assert "ck_connector_observations_quality_score_range" in table_sql
    assert "ck_connector_observations_observed_not_future" in table_sql
    assert {
        "ix_connector_observations_latest",
        "ix_connector_observations_status_observed",
    }.issubset(index_names)

    command.downgrade(config, "20260811_0021")
    with sqlite3.connect(database_path) as connection:
        downgraded_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert "connector_observations" not in downgraded_tables

    command.upgrade(config, "head")
    command.check(config)


def test_migrated_sqlite_connector_score_remains_exact(tmp_path: Path) -> None:
    database_path = tmp_path / "connector-observation-exact.db"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")

    async def exercise_migrated_schema() -> tuple[
        ConnectorObservation | None,
        Decimal | None,
    ]:
        engine = create_async_engine(database_url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        observed_at = datetime(2026, 8, 11, 12, 30, tzinfo=UTC)
        async with factory() as session:
            automation = Automation(
                slug="migrated-connector",
                name="Migrated connector",
                owner="local-owner",
            )
            session.add(automation)
            await session.flush()
            await record_connector_observation(
                session,
                automation=automation,
                connector_key="precision-probe",
                idempotency_key="precision-probe:1",
                status=ConnectorStatus.HEALTHY,
                status_slo_seconds=300,
                last_success_at=observed_at,
                freshness_slo_seconds=900,
                quality_status=DataQualityStatus.PASS,
                quality_score="0.1234",
                observed_at=observed_at,
                now=observed_at,
            )
            await session.commit()

        async with factory() as session:
            observation = await session.scalar(select(ConnectorObservation))
            summary_page = await load_connector_summaries(
                session,
                now=observed_at,
            )
        await engine.dispose()
        return observation, summary_page.items[0].quality_score

    observation, summary_score = asyncio.run(exercise_migrated_schema())

    assert observation is not None
    assert observation.quality_score == Decimal("0.1234")
    assert summary_score == Decimal("0.1234")
    with sqlite3.connect(database_path) as connection:
        stored_score = connection.execute(
            "SELECT quality_score, typeof(quality_score) "
            "FROM connector_observations"
        ).fetchone()
    assert stored_score == ("0.1234", "text")


@pytest.mark.parametrize("table_name", ("connector_observations", "health_conditions"))
def test_alembic_check_tracks_observability_table(
    tmp_path: Path,
    table_name: str,
) -> None:
    database_path = tmp_path / f"managed-{table_name}.db"
    config = Config("alembic.ini")
    config.set_main_option(
        "sqlalchemy.url",
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
    )
    command.upgrade(config, "head")
    with sqlite3.connect(database_path) as connection:
        connection.execute(f"DROP TABLE {table_name}")

    with pytest.raises(CommandError, match="New upgrade operations detected"):
        command.check(config)
