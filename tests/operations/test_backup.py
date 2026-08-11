"""Safety, integrity, and ownership tests for local backup bundles."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import zipfile
from contextlib import closing
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

from src import cli
from src.core.config import Settings
from src.operations import backup
from src.operations.backup import (
    BackupCommandError,
    BackupConfigurationError,
    BackupIntegrityError,
    create_backup,
    restore_backup,
    verify_backup,
)


def _sqlite_settings(tmp_path: Path, **overrides: object) -> Settings:
    database_path = tmp_path / "source.sqlite3"
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute("CREATE TABLE evidence (value TEXT NOT NULL)")
        connection.execute("INSERT INTO evidence VALUES ('durable')")
        connection.commit()
    values: dict[str, object] = {
        "database_url": f"sqlite+aiosqlite:///{database_path.as_posix()}",
        "storage_root": str(tmp_path / "storage"),
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _rewrite_manifest_payload(bundle: Path, section: str, payload_path: Path) -> None:
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    content = payload_path.read_bytes()
    manifest[section]["sha256"] = hashlib.sha256(content).hexdigest()
    manifest[section]["size_bytes"] = len(content)
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def test_create_and_verify_sqlite_backup_is_atomic_and_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _sqlite_settings(
        tmp_path,
        google_client_secret="must-not-be-copied",
        youtube_api_key="must-not-be-copied",
        x_access_token="must-not-be-copied",
    )
    storage = Path(settings.storage_root)
    artifact = storage / "platform-smoke" / "run-1" / "result.txt"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("verified artifact", encoding="utf-8")
    runtime_log = storage / "runtime" / "logs" / "worker.log"
    runtime_log.parent.mkdir(parents=True)
    runtime_log.write_text("private runtime log", encoding="utf-8")
    old_backup = storage / "backups" / "old" / "payload.bin"
    old_backup.parent.mkdir(parents=True)
    old_backup.write_bytes(b"old backup")
    monkeypatch.setattr(backup, "runtime_mutex_is_owned", lambda: False)

    result = create_backup(settings, repository_root=tmp_path)
    verification = verify_backup(result.bundle_path)

    assert result.bundle_path.parent == storage / "backups"
    assert result.bundle_path.name == result.backup_id
    assert not list(result.bundle_path.parent.glob("*.partial"))
    assert verification.backup_id == result.backup_id
    assert verification.database_format == "sqlite3"
    assert verification.artifact_count == 1
    configuration = (result.bundle_path / "configuration.json").read_text(
        encoding="utf-8"
    )
    assert "must-not-be-copied" not in configuration
    assert "client_secret" not in configuration
    assert "api_key" not in configuration
    with zipfile.ZipFile(result.bundle_path / "artifacts.zip") as archive:
        assert archive.namelist() == ["platform-smoke/run-1/result.txt"]
    with sqlite3.connect(result.bundle_path / "database.sqlite3") as connection:
        assert connection.execute("SELECT value FROM evidence").fetchone() == (
            "durable",
        )


def test_backup_and_verify_commands_complete_an_isolated_sqlite_smoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = _sqlite_settings(tmp_path)
    destination = tmp_path / "external-backups"
    monkeypatch.setattr(backup, "runtime_mutex_is_owned", lambda: False)
    monkeypatch.setattr(cli, "get_settings", lambda: settings)

    create_exit = cli.main(
        ["backup", "--destination", str(destination), "--json"]
    )
    created = json.loads(capsys.readouterr().out)
    verify_exit = cli.main(["verify-backup", created["bundle_path"], "--json"])
    verified = json.loads(capsys.readouterr().out)

    assert create_exit == 0
    assert verify_exit == 0
    assert verified["backup_id"] == created["backup_id"]
    assert verified["database_format"] == "sqlite3"


def test_restore_sqlite_bundle_preserves_pre_restore_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    source_settings = _sqlite_settings(source_root)
    source_artifact = (
        Path(source_settings.storage_root) / "platform-smoke" / "run-1" / "result.txt"
    )
    source_artifact.parent.mkdir(parents=True)
    source_artifact.write_text("restored artifact", encoding="utf-8")
    monkeypatch.setattr(backup, "runtime_mutex_is_owned", lambda: False)
    portable = create_backup(
        source_settings,
        destination_root=tmp_path / "portable",
        repository_root=source_root,
    )

    target_root = tmp_path / "target"
    target_root.mkdir()
    target_settings = _sqlite_settings(target_root)
    target_database = target_root / "source.sqlite3"
    with closing(sqlite3.connect(target_database)) as connection:
        connection.execute("UPDATE evidence SET value = 'before-restore'")
        connection.commit()

    result = restore_backup(
        target_settings,
        bundle_path=portable.bundle_path,
        confirmation=f"RESTORE {portable.backup_id}",
        repository_root=target_root,
    )

    with closing(sqlite3.connect(target_database)) as connection:
        assert connection.execute("SELECT value FROM evidence").fetchone() == (
            "durable",
        )
    restored_artifact = (
        Path(target_settings.storage_root)
        / "platform-smoke"
        / "run-1"
        / "result.txt"
    )
    assert restored_artifact.read_text(encoding="utf-8") == "restored artifact"
    assert verify_backup(result.pre_restore_bundle_path).backup_id == (
        result.pre_restore_backup_id
    )
    with closing(
        sqlite3.connect(result.pre_restore_bundle_path / "database.sqlite3")
    ) as connection:
        assert connection.execute("SELECT value FROM evidence").fetchone() == (
            "before-restore",
        )


def test_restore_requires_exact_confirmation_before_target_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    source_settings = _sqlite_settings(source_root)
    monkeypatch.setattr(backup, "runtime_mutex_is_owned", lambda: False)
    portable = create_backup(
        source_settings,
        destination_root=tmp_path / "portable",
        repository_root=source_root,
    )
    target_root = tmp_path / "target"
    target_root.mkdir()
    target_settings = _sqlite_settings(target_root)

    with pytest.raises(BackupConfigurationError, match="confirmation"):
        restore_backup(
            target_settings,
            bundle_path=portable.bundle_path,
            confirmation="RESTORE wrong-id",
            repository_root=target_root,
        )

    assert not (Path(target_settings.storage_root) / "backups").exists()


def test_restore_refuses_nonempty_artifact_target_before_safety_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    source_settings = _sqlite_settings(source_root)
    monkeypatch.setattr(backup, "runtime_mutex_is_owned", lambda: False)
    portable = create_backup(
        source_settings,
        destination_root=tmp_path / "portable",
        repository_root=source_root,
    )
    target_root = tmp_path / "target"
    target_root.mkdir()
    target_settings = _sqlite_settings(target_root)
    existing = Path(target_settings.storage_root) / "existing.txt"
    existing.parent.mkdir(parents=True)
    existing.write_text("keep", encoding="utf-8")

    with pytest.raises(BackupConfigurationError, match="must be empty"):
        restore_backup(
            target_settings,
            bundle_path=portable.bundle_path,
            confirmation=f"RESTORE {portable.backup_id}",
            repository_root=target_root,
        )

    assert existing.read_text(encoding="utf-8") == "keep"
    assert not (Path(target_settings.storage_root) / "backups").exists()


def test_restore_database_failure_rolls_back_new_artifacts_and_keeps_safety_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    source_settings = _sqlite_settings(source_root)
    source_artifact = Path(source_settings.storage_root) / "restored.txt"
    source_artifact.parent.mkdir(parents=True)
    source_artifact.write_text("restore me", encoding="utf-8")
    monkeypatch.setattr(backup, "runtime_mutex_is_owned", lambda: False)
    portable = create_backup(
        source_settings,
        destination_root=tmp_path / "portable",
        repository_root=source_root,
    )
    target_root = tmp_path / "target"
    target_root.mkdir()
    target_settings = _sqlite_settings(target_root)
    monkeypatch.setattr(
        backup,
        "_restore_database",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            BackupCommandError("injected database failure")
        ),
    )

    with pytest.raises(BackupCommandError, match="injected database failure"):
        restore_backup(
            target_settings,
            bundle_path=portable.bundle_path,
            confirmation=f"RESTORE {portable.backup_id}",
            repository_root=target_root,
        )

    assert not (Path(target_settings.storage_root) / "restored.txt").exists()
    safety_backups = [
        path
        for path in (Path(target_settings.storage_root) / "backups").iterdir()
        if path.is_dir() and not path.name.startswith(".")
    ]
    assert len(safety_backups) == 1
    verify_backup(safety_backups[0])


def test_backup_refuses_active_runtime_before_creating_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _sqlite_settings(tmp_path)
    monkeypatch.setattr(backup, "runtime_mutex_is_owned", lambda: True)

    with pytest.raises(BackupConfigurationError, match="runtime must be stopped"):
        create_backup(settings, repository_root=tmp_path)

    assert not (Path(settings.storage_root) / "backups").exists()


def test_backup_failure_removes_only_its_partial_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _sqlite_settings(tmp_path)
    destination = tmp_path / "backups"
    existing = destination / "existing"
    existing.mkdir(parents=True)
    (existing / "keep.txt").write_text("keep", encoding="utf-8")
    monkeypatch.setattr(backup, "runtime_mutex_is_owned", lambda: False)
    monkeypatch.setattr(
        backup,
        "_backup_artifacts",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            BackupCommandError("injected failure")
        ),
    )

    with pytest.raises(BackupCommandError, match="injected failure"):
        create_backup(
            settings,
            destination_root=destination,
            repository_root=tmp_path,
        )

    assert (existing / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert [item.name for item in destination.iterdir()] == ["existing"]


def test_verify_detects_payload_corruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _sqlite_settings(tmp_path)
    monkeypatch.setattr(backup, "runtime_mutex_is_owned", lambda: False)
    result = create_backup(settings, repository_root=tmp_path)

    with (result.bundle_path / "artifacts.zip").open("ab") as archive:
        archive.write(b"corrupt")

    with pytest.raises(BackupIntegrityError, match="integrity"):
        verify_backup(result.bundle_path)


def test_verify_rejects_self_consistent_configuration_with_secret_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _sqlite_settings(tmp_path)
    monkeypatch.setattr(backup, "runtime_mutex_is_owned", lambda: False)
    result = create_backup(settings, repository_root=tmp_path)
    configuration_path = result.bundle_path / "configuration.json"
    configuration = json.loads(configuration_path.read_text(encoding="utf-8"))
    configuration["api_token"] = "private"
    configuration_path.write_text(
        json.dumps(configuration, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _rewrite_manifest_payload(result.bundle_path, "configuration", configuration_path)

    with pytest.raises(BackupIntegrityError, match="redacted"):
        verify_backup(result.bundle_path)


def test_backup_rejects_destination_inside_artifact_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _sqlite_settings(tmp_path)
    monkeypatch.setattr(backup, "runtime_mutex_is_owned", lambda: False)

    with pytest.raises(BackupConfigurationError, match="backups directory"):
        create_backup(
            settings,
            destination_root=Path(settings.storage_root) / "artifact-output",
            repository_root=tmp_path,
        )


def test_verify_rejects_unsafe_archive_path_even_with_updated_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _sqlite_settings(tmp_path)
    monkeypatch.setattr(backup, "runtime_mutex_is_owned", lambda: False)
    result = create_backup(settings, repository_root=tmp_path)
    archive_path = result.bundle_path / "artifacts.zip"
    archive_path.unlink()
    with zipfile.ZipFile(archive_path, mode="w") as archive:
        archive.writestr("../escape.txt", "x")
    _rewrite_manifest_payload(result.bundle_path, "artifacts", archive_path)
    manifest_path = result.bundle_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifacts"]["file_count"] = 1
    manifest["artifacts"]["uncompressed_bytes"] = 1
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(BackupIntegrityError, match="unsafe path"):
        verify_backup(result.bundle_path)


def test_verify_rejects_unexpected_bundle_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _sqlite_settings(tmp_path)
    monkeypatch.setattr(backup, "runtime_mutex_is_owned", lambda: False)
    result = create_backup(settings, repository_root=tmp_path)
    (result.bundle_path / "unexpected.txt").write_text("unexpected", encoding="utf-8")

    with pytest.raises(BackupIntegrityError, match="unexpected entries"):
        verify_backup(result.bundle_path)


@pytest.mark.parametrize("already_running", [False, True])
def test_postgresql_backup_uses_fixed_compose_commands_and_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    already_running: bool,
) -> None:
    commands: list[list[str]] = []

    def run_text(
        command: list[str],
        *,
        cwd: Path,
        timeout_seconds: int,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, timeout_seconds
        commands.append(command)
        stdout = "postgres\n" if command[2:5] == ["ps", "--status", "running"] and already_running else ""
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    def dump(
        command: list[str],
        *,
        output_path: Path,
        cwd: Path,
        timeout_seconds: int,
    ) -> None:
        del cwd, timeout_seconds
        commands.append(command)
        output_path.write_bytes(b"PGDMP-test")

    monkeypatch.setattr(backup, "_run_text", run_text)
    monkeypatch.setattr(backup, "_run_binary_to_file", dump)
    database = make_url(
        "postgresql+asyncpg://foundry:private@127.0.0.1/automation_foundry"
    )

    backup._backup_postgresql(
        database,
        tmp_path / "database.dump",
        repository_root=tmp_path,
        timeout_seconds=30,
    )

    assert all(isinstance(command, list) for command in commands)
    assert any("pg_dump" in command for command in commands)
    assert all("private" not in command for command in commands)
    starts = [command for command in commands if command[2:4] == ["up", "-d"]]
    stops = [command for command in commands if command[2:4] == ["stop", "--timeout"]]
    assert bool(starts) is not already_running
    assert bool(stops) is not already_running


@pytest.mark.parametrize("already_running", [False, True])
def test_postgresql_restore_is_single_transaction_and_ownership_aware(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    already_running: bool,
) -> None:
    commands: list[list[str]] = []
    dump_path = tmp_path / "database.dump"
    dump_path.write_bytes(b"PGDMP-test")

    def run_text(
        command: list[str],
        *,
        cwd: Path,
        timeout_seconds: int,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, timeout_seconds
        commands.append(command)
        stdout = (
            "postgres\n"
            if command[2:5] == ["ps", "--status", "running"] and already_running
            else ""
        )
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    def restore(
        command: list[str],
        *,
        input_path: Path,
        cwd: Path,
        timeout_seconds: int,
    ) -> None:
        del cwd, timeout_seconds
        assert input_path == dump_path
        commands.append(command)

    monkeypatch.setattr(backup, "_run_text", run_text)
    monkeypatch.setattr(backup, "_run_binary_from_file", restore)
    database = make_url(
        "postgresql+asyncpg://foundry:private@127.0.0.1/automation_foundry"
    )

    backup._restore_postgresql(
        database,
        dump_path,
        repository_root=tmp_path,
        timeout_seconds=30,
    )

    restore_command = next(command for command in commands if "pg_restore" in command)
    assert "--single-transaction" in restore_command
    assert "--exit-on-error" in restore_command
    assert "--clean" in restore_command
    assert all("private" not in command for command in commands)
    starts = [command for command in commands if command[2:4] == ["up", "-d"]]
    stops = [command for command in commands if command[2:4] == ["stop", "--timeout"]]
    assert bool(starts) is not already_running
    assert bool(stops) is not already_running
