"""Opt-in disposable PostgreSQL backup and restore integration test."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from src.core.config import Settings
from src.operations import backup
from src.operations.backup import create_backup, restore_backup, verify_backup


def _compose(
    root: Path,
    *arguments: str,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", *arguments],
        cwd=root,
        check=True,
        capture_output=capture_output,
        text=True,
        timeout=120,
    )


def _psql(root: Path, sql: str, *, capture_output: bool = False) -> str:
    result = _compose(
        root,
        "exec",
        "-T",
        "postgres",
        "psql",
        "--username",
        "foundry",
        "--dbname",
        "foundry",
        "--set",
        "ON_ERROR_STOP=1",
        "--tuples-only",
        "--no-align",
        "--command",
        sql,
        capture_output=capture_output,
    )
    return (result.stdout or "").strip()


@pytest.mark.postgresql
def test_disposable_postgresql_backup_and_restore_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if os.getenv("RUN_DOCKER_BACKUP_TEST") != "1":
        pytest.skip("RUN_DOCKER_BACKUP_TEST is not enabled")
    project_root = tmp_path / "backup-compose"
    project_root.mkdir()
    (project_root / "compose.yaml").write_text(
        """services:
  postgres:
    image: postgres:17-alpine
    environment:
      POSTGRES_DB: foundry
      POSTGRES_USER: foundry
      POSTGRES_PASSWORD: integration-password
    healthcheck:
      test: [\"CMD-SHELL\", \"pg_isready -U foundry -d foundry\"]
      interval: 1s
      timeout: 1s
      retries: 30
    volumes:
      - postgres_data:/var/lib/postgresql/data
volumes:
  postgres_data:
""",
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "COMPOSE_PROJECT_NAME",
        f"foundry_backup_test_{uuid4().hex[:8]}",
    )
    monkeypatch.setattr(backup, "runtime_mutex_is_owned", lambda: False)
    storage = tmp_path / "storage"
    artifact = storage / "module" / "run-1" / "evidence.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("before", encoding="utf-8")
    settings = Settings(
        _env_file=None,
        database_url=(
            "postgresql+asyncpg://foundry:integration-password@127.0.0.1/foundry"
        ),
        storage_root=str(storage),
        runtime_startup_timeout_seconds=30,
    )

    _compose(project_root, "up", "-d", "--wait", "postgres")
    try:
        _psql(
            project_root,
            "CREATE TABLE evidence (value text NOT NULL); "
            "INSERT INTO evidence VALUES ('before');",
        )
        created = create_backup(settings, repository_root=project_root)
        verify_backup(created.bundle_path)
        _psql(project_root, "UPDATE evidence SET value = 'after';")
        shutil.rmtree(storage / "module")

        restored = restore_backup(
            settings,
            bundle_path=created.bundle_path,
            confirmation=f"RESTORE {created.backup_id}",
            repository_root=project_root,
        )

        assert _psql(
            project_root,
            "SELECT value FROM evidence;",
            capture_output=True,
        ) == "before"
        assert artifact.read_text(encoding="utf-8") == "before"
        verify_backup(restored.pre_restore_bundle_path)
    finally:
        _compose(project_root, "down", "--volumes", "--remove-orphans")
