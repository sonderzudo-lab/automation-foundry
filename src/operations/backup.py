"""Atomic local backup bundles for database state, artifacts, and safe settings."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import uuid
import zipfile
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy.engine import URL, make_url

from src.core.config import Settings
from src.runtime.windows import runtime_mutex_is_owned

_BACKUP_SCHEMA_VERSION = 1
_CONFIGURATION_SCHEMA_VERSION = 1
_MANIFEST_FILENAME = "manifest.json"
_CONFIGURATION_FILENAME = "configuration.json"
_ARTIFACTS_FILENAME = "artifacts.zip"
_SQLITE_FILENAME = "database.sqlite3"
_POSTGRES_FILENAME = "database.dump"
_MAX_MANIFEST_BYTES = 64 * 1024
_HASH_CHUNK_BYTES = 1024 * 1024
_EXCLUDED_STORAGE_NAMES = frozenset({"backups", "runtime"})
_SENSITIVE_KEY_PARTS = (
    "secret",
    "password",
    "token",
    "credential",
    "api_key",
    "encryption_key",
)


class BackupConfigurationError(ValueError):
    """Raised before backup mutation when local configuration is unsafe."""


class BackupCommandError(RuntimeError):
    """Raised when a local snapshot command fails without exposing its output."""


class BackupIntegrityError(ValueError):
    """Raised when a backup bundle fails offline verification."""


@dataclass(frozen=True, slots=True)
class BackupResult:
    """Redacted operational summary of one completed backup."""

    backup_id: str
    bundle_path: Path
    database_format: str
    artifact_count: int
    total_bytes: int


@dataclass(frozen=True, slots=True)
class BackupVerificationResult:
    """Redacted summary of one verified backup bundle."""

    backup_id: str
    database_format: str
    artifact_count: int
    total_bytes: int


@dataclass(frozen=True, slots=True)
class RestoreResult:
    """Redacted result of one explicitly confirmed local restore."""

    backup_id: str
    pre_restore_backup_id: str
    pre_restore_bundle_path: Path
    database_format: str
    artifact_count: int


@dataclass(frozen=True, slots=True)
class _Payload:
    filename: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class _ArtifactPayload:
    filename: str
    sha256: str
    size_bytes: int
    file_count: int
    uncompressed_bytes: int


def create_backup(
    configuration: Settings,
    *,
    destination_root: Path | None = None,
    repository_root: Path | None = None,
) -> BackupResult:
    """Create and verify one atomic backup bundle without changing source data."""
    if runtime_mutex_is_owned():
        raise BackupConfigurationError("runtime must be stopped before backup")

    root = (repository_root or Path(__file__).resolve().parents[2]).resolve()
    storage_root = _prepare_storage_root(Path(configuration.storage_root))
    destination = _prepare_destination(
        destination_root or storage_root / "backups",
        storage_root=storage_root,
    )
    backup_id = _backup_id()
    final_path = destination / backup_id
    partial_path = destination / f".{backup_id}.partial"
    partial_path.mkdir()

    try:
        database_format, database_filename = _backup_database(
            configuration,
            output_root=partial_path,
            repository_root=root,
        )
        configuration_path = partial_path / _CONFIGURATION_FILENAME
        configuration_path.write_text(
            json.dumps(
                _safe_configuration(configuration),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        artifact_payload = _backup_artifacts(
            storage_root,
            partial_path / _ARTIFACTS_FILENAME,
        )
        database_payload = _payload(partial_path / database_filename)
        configuration_payload = _payload(configuration_path)
        manifest = {
            "schema_version": _BACKUP_SCHEMA_VERSION,
            "backup_id": backup_id,
            "created_at": datetime.now(UTC).isoformat(),
            "database": {
                "format": database_format,
                **_payload_dict(database_payload),
            },
            "configuration": _payload_dict(configuration_payload),
            "artifacts": {
                **_payload_dict(artifact_payload),
                "file_count": artifact_payload.file_count,
                "uncompressed_bytes": artifact_payload.uncompressed_bytes,
            },
        }
        (partial_path / _MANIFEST_FILENAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        verification = verify_backup(partial_path)
        partial_path.replace(final_path)
    except Exception:
        _remove_partial_bundle(partial_path, destination=destination)
        raise

    return BackupResult(
        backup_id=verification.backup_id,
        bundle_path=final_path,
        database_format=verification.database_format,
        artifact_count=verification.artifact_count,
        total_bytes=verification.total_bytes,
    )


def verify_backup(bundle_path: Path) -> BackupVerificationResult:
    """Verify hashes, formats, redaction, and archive paths without extracting data."""
    bundle = _existing_bundle(bundle_path)
    manifest = _load_manifest(bundle / _MANIFEST_FILENAME)
    try:
        backup_id = _required_text(manifest, "backup_id", max_length=100)
        database = _required_mapping(manifest, "database")
        configuration = _required_mapping(manifest, "configuration")
        artifacts = _required_mapping(manifest, "artifacts")
        database_format = _required_text(database, "format", max_length=40)
        expected_database_filename = (
            _SQLITE_FILENAME if database_format == "sqlite3" else _POSTGRES_FILENAME
        )
        if database_format not in {"sqlite3", "postgres-custom"}:
            raise BackupIntegrityError("backup database format is unsupported")
        database_payload = _verified_payload(
            bundle,
            database,
            expected_filename=expected_database_filename,
        )
        configuration_payload = _verified_payload(
            bundle,
            configuration,
            expected_filename=_CONFIGURATION_FILENAME,
        )
        artifact_payload = _verified_payload(
            bundle,
            artifacts,
            expected_filename=_ARTIFACTS_FILENAME,
        )
        artifact_count = _required_nonnegative_integer(artifacts, "file_count")
        uncompressed_bytes = _required_nonnegative_integer(
            artifacts,
            "uncompressed_bytes",
        )
        expected_files = {
            _MANIFEST_FILENAME,
            database_payload.filename,
            configuration_payload.filename,
            artifact_payload.filename,
        }
        bundle_entries = list(bundle.iterdir())
        actual_files = {item.name for item in bundle_entries}
        if actual_files != expected_files or any(
            item.is_symlink() or not item.is_file() for item in bundle_entries
        ):
            raise BackupIntegrityError("backup bundle contains unexpected entries")
        _verify_database(bundle / database_payload.filename, database_format)
        _verify_configuration(bundle / configuration_payload.filename)
        _verify_artifacts(
            bundle / artifact_payload.filename,
            expected_count=artifact_count,
            expected_uncompressed_bytes=uncompressed_bytes,
        )
    except (KeyError, TypeError, ValueError, OSError, zipfile.BadZipFile) as exc:
        if isinstance(exc, BackupIntegrityError):
            raise
        raise BackupIntegrityError("backup bundle is invalid") from exc

    total_bytes = (
        database_payload.size_bytes
        + configuration_payload.size_bytes
        + artifact_payload.size_bytes
    )
    return BackupVerificationResult(
        backup_id=backup_id,
        database_format=database_format,
        artifact_count=artifact_count,
        total_bytes=total_bytes,
    )


def restore_backup(
    configuration: Settings,
    *,
    bundle_path: Path,
    confirmation: str,
    repository_root: Path | None = None,
) -> RestoreResult:
    """Restore one verified bundle after exact confirmation and a safety backup."""
    bundle = _existing_bundle(bundle_path)
    verification = verify_backup(bundle)
    if confirmation != f"RESTORE {verification.backup_id}":
        raise BackupConfigurationError("restore confirmation does not match backup id")
    if runtime_mutex_is_owned():
        raise BackupConfigurationError("runtime must be stopped before restore")

    root = (repository_root or Path(__file__).resolve().parents[2]).resolve()
    storage_root = _prepare_storage_root(Path(configuration.storage_root))
    if _artifact_files(storage_root):
        raise BackupConfigurationError("artifact storage must be empty before restore")
    manifest = _load_manifest(bundle / _MANIFEST_FILENAME)
    _assert_restore_compatible(configuration, bundle=bundle, manifest=manifest)

    pre_restore = create_backup(configuration, repository_root=root)
    staging = storage_root.parent / (
        f".{storage_root.name}.restore-{uuid.uuid4().hex}.partial"
    )
    staging.mkdir()
    installed: list[Path] = []
    try:
        _extract_artifacts(bundle / _ARTIFACTS_FILENAME, staging)
        post_staging_verification = verify_backup(bundle)
        if post_staging_verification.backup_id != verification.backup_id:
            raise BackupIntegrityError("backup changed during restore staging")
        installed = _install_staged_artifacts(staging, storage_root=storage_root)
        _remove_restore_staging(staging, parent=storage_root.parent)
        _restore_database(
            configuration,
            bundle=bundle,
            database_format=verification.database_format,
            repository_root=root,
        )
    except Exception:
        _remove_installed_artifacts(installed, storage_root=storage_root)
        _remove_restore_staging(staging, parent=storage_root.parent)
        raise

    return RestoreResult(
        backup_id=verification.backup_id,
        pre_restore_backup_id=pre_restore.backup_id,
        pre_restore_bundle_path=pre_restore.bundle_path,
        database_format=verification.database_format,
        artifact_count=verification.artifact_count,
    )


def _prepare_storage_root(path: Path) -> Path:
    if path.is_symlink():
        raise BackupConfigurationError("storage root must not be a symlink")
    root = path.resolve()
    if root == Path(root.anchor):
        raise BackupConfigurationError("storage root must not be a drive root")
    root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise BackupConfigurationError("storage root must be a real directory")
    return root


def _prepare_destination(path: Path, *, storage_root: Path) -> Path:
    if path.exists() and path.is_symlink():
        raise BackupConfigurationError("backup destination must not be a symlink")
    destination = path.resolve()
    if destination == Path(destination.anchor) or destination == storage_root:
        raise BackupConfigurationError("backup destination is too broad")
    try:
        relative = destination.relative_to(storage_root)
    except ValueError:
        relative = None
    if relative is not None and (
        not relative.parts or relative.parts[0] != "backups"
    ):
        raise BackupConfigurationError(
            "backup destination below storage must use the backups directory"
        )
    destination.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink() or not destination.is_dir():
        raise BackupConfigurationError("backup destination must be a real directory")
    return destination


def _backup_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}-{uuid.uuid4().hex[:8]}"


def _backup_database(
    configuration: Settings,
    *,
    output_root: Path,
    repository_root: Path,
) -> tuple[str, str]:
    database = make_url(configuration.database_url)
    if database.drivername == "sqlite+aiosqlite":
        _backup_sqlite(database, output_root / _SQLITE_FILENAME, repository_root)
        return "sqlite3", _SQLITE_FILENAME
    if database.drivername == "postgresql+asyncpg":
        _backup_postgresql(
            database,
            output_root / _POSTGRES_FILENAME,
            repository_root=repository_root,
            timeout_seconds=configuration.runtime_startup_timeout_seconds,
        )
        return "postgres-custom", _POSTGRES_FILENAME
    raise BackupConfigurationError("backup database driver is unsupported")


def _backup_sqlite(database: URL, output_path: Path, repository_root: Path) -> None:
    database_name = database.database
    if not database_name or database_name == ":memory:":
        raise BackupConfigurationError("SQLite backup requires a file database")
    source_path = Path(database_name)
    if not source_path.is_absolute():
        source_path = repository_root / source_path
    if source_path.is_symlink():
        raise BackupConfigurationError("SQLite database file must not be a symlink")
    source_path = source_path.resolve()
    if not source_path.is_file():
        raise BackupConfigurationError("SQLite database file is unavailable")
    try:
        with (
            closing(sqlite3.connect(str(source_path))) as source,
            closing(sqlite3.connect(str(output_path))) as target,
        ):
            source.backup(target)
            result = target.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.Error as exc:
        raise BackupCommandError("SQLite backup failed") from exc
    if result is None or result[0] != "ok":
        raise BackupCommandError("SQLite backup integrity check failed")


def _backup_postgresql(
    database: URL,
    output_path: Path,
    *,
    repository_root: Path,
    timeout_seconds: int,
) -> None:
    username = database.username
    database_name = database.database
    if (
        not username
        or not database_name
        or not database.password
        or database.password == "SET_IN_LOCAL_ENV"
    ):
        raise BackupConfigurationError("PostgreSQL backup configuration is incomplete")
    _run_text(
        ["docker", "compose", "config", "--quiet"],
        cwd=repository_root,
        timeout_seconds=timeout_seconds,
    )
    running = _postgres_is_running(repository_root, timeout_seconds=timeout_seconds)
    owned = not running
    try:
        if owned:
            _run_text(
                [
                    "docker",
                    "compose",
                    "up",
                    "-d",
                    "--wait",
                    "--wait-timeout",
                    str(timeout_seconds),
                    "postgres",
                ],
                cwd=repository_root,
                timeout_seconds=timeout_seconds + 30,
            )
        _run_binary_to_file(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "postgres",
                "pg_dump",
                "--format=custom",
                "--no-owner",
                "--no-privileges",
                "--username",
                username,
                "--dbname",
                database_name,
            ],
            output_path=output_path,
            cwd=repository_root,
            timeout_seconds=max(timeout_seconds, 300),
        )
    finally:
        if owned:
            _run_text(
                [
                    "docker",
                    "compose",
                    "stop",
                    "--timeout",
                    str(timeout_seconds),
                    "postgres",
                ],
                cwd=repository_root,
                timeout_seconds=timeout_seconds + 30,
            )


def _assert_restore_compatible(
    configuration: Settings,
    *,
    bundle: Path,
    manifest: dict[str, Any],
) -> None:
    configuration_metadata = _required_mapping(manifest, "configuration")
    configuration_filename = _required_text(
        configuration_metadata,
        "filename",
        max_length=100,
    )
    try:
        safe_configuration: Any = json.loads(
            (bundle / configuration_filename).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupIntegrityError("backup configuration is invalid") from exc
    if not isinstance(safe_configuration, dict):
        raise BackupIntegrityError("backup configuration is invalid")
    backed_database = _required_mapping(safe_configuration, "database")
    current_database = make_url(configuration.database_url)
    backed_driver = _required_text(backed_database, "driver", max_length=100)
    backed_name = backed_database.get("name")
    if (
        backed_driver != current_database.drivername
        or not isinstance(backed_name, str)
        or backed_name != _safe_database_name(current_database)
    ):
        raise BackupConfigurationError(
            "backup database identity does not match current configuration"
        )


def _restore_database(
    configuration: Settings,
    *,
    bundle: Path,
    database_format: str,
    repository_root: Path,
) -> None:
    database = make_url(configuration.database_url)
    if database_format == "sqlite3" and database.drivername == "sqlite+aiosqlite":
        _restore_sqlite(
            database,
            bundle / _SQLITE_FILENAME,
            repository_root=repository_root,
        )
        return
    if (
        database_format == "postgres-custom"
        and database.drivername == "postgresql+asyncpg"
    ):
        _restore_postgresql(
            database,
            bundle / _POSTGRES_FILENAME,
            repository_root=repository_root,
            timeout_seconds=configuration.runtime_startup_timeout_seconds,
        )
        return
    raise BackupConfigurationError("backup database format does not match target")


def _restore_sqlite(
    database: URL,
    backup_path: Path,
    *,
    repository_root: Path,
) -> None:
    database_name = database.database
    if not database_name or database_name == ":memory:":
        raise BackupConfigurationError("SQLite restore requires a file database")
    target_path = Path(database_name)
    if not target_path.is_absolute():
        target_path = repository_root / target_path
    if target_path.is_symlink() or not target_path.is_file():
        raise BackupConfigurationError("SQLite restore target is unavailable")
    target_path = target_path.resolve()
    temporary = target_path.with_name(
        f".{target_path.name}.restore-{uuid.uuid4().hex}.partial"
    )
    try:
        shutil.copyfile(backup_path, temporary)
        _verify_database(temporary, "sqlite3")
        os.replace(temporary, target_path)
    except OSError as exc:
        raise BackupCommandError("SQLite restore failed") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _restore_postgresql(
    database: URL,
    backup_path: Path,
    *,
    repository_root: Path,
    timeout_seconds: int,
) -> None:
    username = database.username
    database_name = database.database
    if (
        not username
        or not database_name
        or not database.password
        or database.password == "SET_IN_LOCAL_ENV"
    ):
        raise BackupConfigurationError("PostgreSQL restore configuration is incomplete")
    _run_text(
        ["docker", "compose", "config", "--quiet"],
        cwd=repository_root,
        timeout_seconds=timeout_seconds,
    )
    running = _postgres_is_running(repository_root, timeout_seconds=timeout_seconds)
    owned = not running
    try:
        if owned:
            _run_text(
                [
                    "docker",
                    "compose",
                    "up",
                    "-d",
                    "--wait",
                    "--wait-timeout",
                    str(timeout_seconds),
                    "postgres",
                ],
                cwd=repository_root,
                timeout_seconds=timeout_seconds + 30,
            )
        _run_binary_from_file(
            [
                "docker",
                "compose",
                "exec",
                "-T",
                "postgres",
                "pg_restore",
                "--clean",
                "--if-exists",
                "--exit-on-error",
                "--single-transaction",
                "--no-owner",
                "--no-privileges",
                "--username",
                username,
                "--dbname",
                database_name,
            ],
            input_path=backup_path,
            cwd=repository_root,
            timeout_seconds=max(timeout_seconds, 300),
        )
    finally:
        if owned:
            _run_text(
                [
                    "docker",
                    "compose",
                    "stop",
                    "--timeout",
                    str(timeout_seconds),
                    "postgres",
                ],
                cwd=repository_root,
                timeout_seconds=timeout_seconds + 30,
            )


def _postgres_is_running(root: Path, *, timeout_seconds: int) -> bool:
    result = _run_text(
        ["docker", "compose", "ps", "--status", "running", "--services", "postgres"],
        cwd=root,
        timeout_seconds=timeout_seconds,
    )
    return "postgres" in {line.strip() for line in result.stdout.splitlines()}


def _run_text(
    command: list[str],
    *,
    cwd: Path,
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BackupCommandError("local backup command failed") from exc


def _run_binary_to_file(
    command: list[str],
    *,
    output_path: Path,
    cwd: Path,
    timeout_seconds: int,
) -> None:
    try:
        with output_path.open("xb") as output:
            subprocess.run(
                command,
                cwd=cwd,
                check=True,
                stdout=output,
                stderr=subprocess.PIPE,
                timeout=timeout_seconds,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BackupCommandError("database snapshot command failed") from exc


def _run_binary_from_file(
    command: list[str],
    *,
    input_path: Path,
    cwd: Path,
    timeout_seconds: int,
) -> None:
    try:
        with input_path.open("rb") as source:
            subprocess.run(
                command,
                cwd=cwd,
                check=True,
                stdin=source,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=timeout_seconds,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BackupCommandError("database restore command failed") from exc


def _safe_configuration(configuration: Settings) -> dict[str, Any]:
    database = make_url(configuration.database_url)
    return {
        "schema_version": _CONFIGURATION_SCHEMA_VERSION,
        "environment": configuration.environment,
        "log_level": configuration.log_level,
        "dashboard": {
            "host": configuration.dashboard_host,
            "port": configuration.dashboard_port,
        },
        "database": {
            "driver": database.drivername,
            "host": database.host,
            "port": database.port,
            "name": _safe_database_name(database),
            "pool_size": configuration.database_pool_size,
            "max_overflow": configuration.database_max_overflow,
            "pool_timeout_seconds": configuration.database_pool_timeout_seconds,
            "connect_timeout_seconds": configuration.database_connect_timeout_seconds,
            "command_timeout_seconds": configuration.database_command_timeout_seconds,
        },
        "redis": {
            "broker": _safe_endpoint(configuration.redis_url),
            "result_backend": _safe_endpoint(
                configuration.celery_result_backend_url
            ),
        },
        "celery": {
            "soft_time_limit_seconds": configuration.celery_soft_time_limit_seconds,
            "hard_time_limit_seconds": configuration.celery_hard_time_limit_seconds,
            "visibility_timeout_seconds": configuration.celery_visibility_timeout_seconds,
            "dispatch_lease_seconds": configuration.celery_dispatch_lease_seconds,
            "schedule_tick_seconds": configuration.celery_schedule_tick_seconds,
        },
        "health": {
            "probe_timeout_seconds": configuration.health_probe_timeout_seconds,
            "warning_percent": configuration.health_warning_percent,
            "critical_percent": configuration.health_critical_percent,
            "failure_threshold": configuration.health_alert_failure_threshold,
            "recovery_threshold": configuration.health_alert_recovery_threshold,
        },
        "runtime": {
            "startup_timeout_seconds": configuration.runtime_startup_timeout_seconds,
            "shutdown_timeout_seconds": configuration.runtime_shutdown_timeout_seconds,
        },
        "ollama": {
            "endpoint": _safe_endpoint(configuration.ollama_base_url),
            "model": configuration.ollama_model,
            "timeout_seconds": configuration.ollama_timeout,
        },
        "content_tts": {
            "backend": configuration.content_tts_backend,
            "voice_id": configuration.content_tts_voice_id,
            "language_code": configuration.content_tts_language_code,
        },
        "content_visuals": {
            "backend": configuration.content_visual_backend,
        },
        "content_captions": {
            "backend": configuration.content_caption_backend,
        },
        "content_assembly": {
            "backend": configuration.content_assembly_backend,
            "ffmpeg_expected_sha256": configuration.content_ffmpeg_expected_sha256,
            "ffprobe_expected_sha256": configuration.content_ffprobe_expected_sha256,
        },
        "content_originality": {
            "similarity_threshold": configuration.content_similarity_threshold,
            "similarity_window": configuration.content_similarity_window,
        },
        "hardware_costs": {
            "gpu_power_watts": configuration.gpu_power_watts,
            "cpu_power_watts": configuration.cpu_power_watts,
            "energy_tariff_brl_per_kwh": configuration.energy_tariff_brl_per_kwh,
        },
    }


def _safe_endpoint(value: str) -> dict[str, str | int | None]:
    parsed = urlsplit(value)
    return {
        "scheme": parsed.scheme,
        "host": parsed.hostname,
        "port": parsed.port,
        "path": parsed.path,
    }


def _safe_database_name(database: URL) -> str | None:
    if database.database is None:
        return None
    if database.drivername == "sqlite+aiosqlite":
        return Path(database.database).name
    return database.database


def _backup_artifacts(storage_root: Path, output_path: Path) -> _ArtifactPayload:
    artifact_files = _artifact_files(storage_root)
    uncompressed_bytes = 0
    try:
        with zipfile.ZipFile(
            output_path,
            mode="x",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
        ) as archive:
            for relative_path, source_path in artifact_files:
                before = source_path.stat()
                archive.write(source_path, arcname=relative_path.as_posix())
                after = source_path.stat()
                if (
                    before.st_size != after.st_size
                    or before.st_mtime_ns != after.st_mtime_ns
                ):
                    raise BackupCommandError("artifact changed during backup")
                uncompressed_bytes += after.st_size
    except (OSError, zipfile.BadZipFile) as exc:
        raise BackupCommandError("artifact backup failed") from exc
    payload = _payload(output_path)
    return _ArtifactPayload(
        filename=payload.filename,
        sha256=payload.sha256,
        size_bytes=payload.size_bytes,
        file_count=len(artifact_files),
        uncompressed_bytes=uncompressed_bytes,
    )


def _artifact_files(storage_root: Path) -> list[tuple[Path, Path]]:
    files: list[tuple[Path, Path]] = []
    for current_root, directory_names, filenames in os.walk(
        storage_root,
        topdown=True,
        followlinks=False,
    ):
        current = Path(current_root)
        if current == storage_root:
            directory_names[:] = sorted(
                name for name in directory_names if name not in _EXCLUDED_STORAGE_NAMES
            )
        else:
            directory_names.sort()
        for directory_name in directory_names:
            if (current / directory_name).is_symlink():
                raise BackupConfigurationError("artifact directories must not be symlinks")
        for filename in sorted(filenames):
            source = current / filename
            relative = source.relative_to(storage_root)
            if relative == Path(".gitkeep"):
                continue
            if source.is_symlink() or not source.is_file():
                raise BackupConfigurationError("artifacts must be regular files")
            files.append((relative, source))
    return files


def _payload(path: Path) -> _Payload:
    digest = hashlib.sha256()
    size_bytes = 0
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(_HASH_CHUNK_BYTES):
                digest.update(chunk)
                size_bytes += len(chunk)
    except OSError as exc:
        raise BackupCommandError("backup payload could not be read") from exc
    return _Payload(path.name, digest.hexdigest(), size_bytes)


def _payload_dict(payload: _Payload | _ArtifactPayload) -> dict[str, str | int]:
    return {
        "filename": payload.filename,
        "sha256": payload.sha256,
        "size_bytes": payload.size_bytes,
    }


def _existing_bundle(path: Path) -> Path:
    if path.is_symlink():
        raise BackupIntegrityError("backup bundle must not be a symlink")
    try:
        bundle = path.resolve(strict=True)
    except OSError as exc:
        raise BackupIntegrityError("backup bundle is unavailable") from exc
    if not bundle.is_dir():
        raise BackupIntegrityError("backup bundle must be a directory")
    return bundle


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        if path.is_symlink() or path.stat().st_size > _MAX_MANIFEST_BYTES:
            raise BackupIntegrityError("backup manifest is unsafe")
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupIntegrityError("backup manifest is invalid") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != _BACKUP_SCHEMA_VERSION:
        raise BackupIntegrityError("backup manifest version is unsupported")
    return payload


def _required_mapping(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload[key]
    if not isinstance(value, dict):
        raise BackupIntegrityError(f"backup {key} metadata is invalid")
    return value


def _required_text(
    payload: dict[str, Any],
    key: str,
    *,
    max_length: int,
) -> str:
    value = payload[key]
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise BackupIntegrityError(f"backup {key} is invalid")
    return value


def _required_nonnegative_integer(payload: dict[str, Any], key: str) -> int:
    value = payload[key]
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise BackupIntegrityError(f"backup {key} is invalid")
    return value


def _verified_payload(
    bundle: Path,
    metadata: dict[str, Any],
    *,
    expected_filename: str,
) -> _Payload:
    filename = _required_text(metadata, "filename", max_length=100)
    expected_hash = _required_text(metadata, "sha256", max_length=64)
    expected_size = _required_nonnegative_integer(metadata, "size_bytes")
    if filename != expected_filename:
        raise BackupIntegrityError("backup payload filename is invalid")
    path = bundle / filename
    if path.is_symlink() or not path.is_file():
        raise BackupIntegrityError("backup payload is unavailable")
    try:
        actual = _payload(path)
    except BackupCommandError as exc:
        raise BackupIntegrityError("backup payload could not be verified") from exc
    if actual.sha256 != expected_hash or actual.size_bytes != expected_size:
        raise BackupIntegrityError("backup payload integrity check failed")
    return actual


def _verify_database(path: Path, database_format: str) -> None:
    if database_format == "postgres-custom":
        try:
            with path.open("rb") as handle:
                if handle.read(5) != b"PGDMP":
                    raise BackupIntegrityError("PostgreSQL backup header is invalid")
        except OSError as exc:
            raise BackupIntegrityError("PostgreSQL backup could not be read") from exc
        return
    try:
        with closing(
            sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        ) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.Error as exc:
        raise BackupIntegrityError("SQLite backup is invalid") from exc
    if result is None or result[0] != "ok":
        raise BackupIntegrityError("SQLite backup integrity check failed")


def _verify_configuration(path: Path) -> None:
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupIntegrityError("backup configuration is invalid") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != _CONFIGURATION_SCHEMA_VERSION
        or _contains_sensitive_key(payload)
        or _contains_embedded_credentials(payload)
    ):
        raise BackupIntegrityError("backup configuration is not safely redacted")


def _contains_sensitive_key(value: Any) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).casefold()
            if any(part in normalized for part in _SENSITIVE_KEY_PARTS):
                return True
            if _contains_sensitive_key(child):
                return True
    elif isinstance(value, list):
        return any(_contains_sensitive_key(child) for child in value)
    return False


def _contains_embedded_credentials(value: Any) -> bool:
    if isinstance(value, str):
        parsed = urlsplit(value)
        return bool(parsed.scheme and (parsed.username or parsed.password))
    if isinstance(value, dict):
        return any(_contains_embedded_credentials(child) for child in value.values())
    if isinstance(value, list):
        return any(_contains_embedded_credentials(child) for child in value)
    return False


def _verify_artifacts(
    path: Path,
    *,
    expected_count: int,
    expected_uncompressed_bytes: int,
) -> None:
    with zipfile.ZipFile(path, mode="r") as archive:
        if archive.testzip() is not None:
            raise BackupIntegrityError("artifact archive CRC check failed")
        members = archive.infolist()
        names: set[str] = set()
        total_bytes = 0
        for member in members:
            member_path = PurePosixPath(member.filename)
            if (
                member.is_dir()
                or member.filename in names
                or member_path.is_absolute()
                or ".." in member_path.parts
                or not member_path.parts
                or member_path.parts[0] in _EXCLUDED_STORAGE_NAMES
                or stat.S_ISLNK(member.external_attr >> 16)
            ):
                raise BackupIntegrityError("artifact archive contains an unsafe path")
            names.add(member.filename)
            total_bytes += member.file_size
        if len(members) != expected_count or total_bytes != expected_uncompressed_bytes:
            raise BackupIntegrityError("artifact archive metadata does not match")


def _extract_artifacts(archive_path: Path, staging: Path) -> None:
    try:
        with zipfile.ZipFile(archive_path, mode="r") as archive:
            for member in archive.infolist():
                relative = PurePosixPath(member.filename)
                if (
                    member.is_dir()
                    or relative.is_absolute()
                    or ".." in relative.parts
                    or not relative.parts
                    or relative.parts[0] in _EXCLUDED_STORAGE_NAMES
                    or stat.S_ISLNK(member.external_attr >> 16)
                ):
                    raise BackupIntegrityError(
                        "artifact archive contains an unsafe path"
                    )
                target = staging.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member, mode="r") as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, length=_HASH_CHUNK_BYTES)
    except (OSError, zipfile.BadZipFile) as exc:
        raise BackupCommandError("artifact restore staging failed") from exc


def _install_staged_artifacts(staging: Path, *, storage_root: Path) -> list[Path]:
    installed: list[Path] = []
    try:
        for source in sorted(staging.iterdir(), key=lambda item: item.name):
            target = storage_root / source.name
            if target.exists() or target.is_symlink():
                raise BackupConfigurationError("artifact restore target changed")
            source.replace(target)
            installed.append(target)
    except Exception:
        _remove_installed_artifacts(installed, storage_root=storage_root)
        raise
    return installed


def _remove_installed_artifacts(paths: list[Path], *, storage_root: Path) -> None:
    for path in reversed(paths):
        try:
            relative = path.relative_to(storage_root)
        except ValueError:
            continue
        if len(relative.parts) != 1:
            continue
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path)


def _remove_restore_staging(path: Path, *, parent: Path) -> None:
    try:
        relative = path.relative_to(parent)
    except ValueError:
        return
    if (
        len(relative.parts) == 1
        and path.name.startswith(".")
        and ".restore-" in path.name
        and path.name.endswith(".partial")
        and path.exists()
    ):
        shutil.rmtree(path)


def _remove_partial_bundle(path: Path, *, destination: Path) -> None:
    try:
        path.relative_to(destination)
    except ValueError:
        return
    if path.name.startswith(".") and path.name.endswith(".partial") and path.exists():
        shutil.rmtree(path)
