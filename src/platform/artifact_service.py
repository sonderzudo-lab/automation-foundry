"""Safe registration of durable metadata for files in local artifact storage."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.platform.models import Artifact, ArtifactSensitivity, Run, StepRun
from src.platform.run_service import IdempotencyConflictError

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_HASH_CHUNK_BYTES = 1024 * 1024


class ArtifactStorageError(ValueError):
    """Raised when a requested file cannot be safely read below the storage root."""


@dataclass(frozen=True, slots=True)
class ArtifactRegistrationResult:
    """Result of an idempotent local artifact registration."""

    artifact: Artifact
    created: bool


@dataclass(frozen=True, slots=True)
class _InspectedFile:
    relative_path: str
    sha256: str
    size_bytes: int


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _require_text(value: str, field: str, *, max_length: int) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} must not be empty")
    if len(normalized) > max_length:
        raise ValueError(f"{field} exceeds {max_length} characters")
    return normalized


def _normalize_expected_sha256(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if _SHA256_PATTERN.fullmatch(normalized) is None:
        raise ValueError("expected_sha256 must be a 64-character hexadecimal digest")
    return normalized


def _inspect_local_file(storage_root: Path, file_path: Path) -> _InspectedFile:
    try:
        root = storage_root.resolve(strict=True)
    except OSError as exc:
        raise ArtifactStorageError("artifact storage root is unavailable") from exc
    if not root.is_dir():
        raise ArtifactStorageError("artifact storage root is not a directory")

    candidate = file_path if file_path.is_absolute() else root / file_path
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ArtifactStorageError("artifact file is unavailable") from exc
    try:
        relative_path = resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise ArtifactStorageError(
            "artifact path must remain below the configured storage root"
        ) from exc
    if not resolved.is_file():
        raise ArtifactStorageError("artifact path is not a regular file")

    digest = hashlib.sha256()
    size_bytes = 0
    try:
        with resolved.open("rb") as file_handle:
            initial_stat = os.fstat(file_handle.fileno())
            while chunk := file_handle.read(_HASH_CHUNK_BYTES):
                digest.update(chunk)
                size_bytes += len(chunk)
            final_stat = os.fstat(file_handle.fileno())
    except OSError as exc:
        raise ArtifactStorageError("artifact file could not be read") from exc
    if (
        initial_stat.st_size != final_stat.st_size
        or initial_stat.st_mtime_ns != final_stat.st_mtime_ns
        or final_stat.st_size != size_bytes
    ):
        raise ArtifactStorageError("artifact file changed while it was being inspected")

    return _InspectedFile(
        relative_path=relative_path,
        sha256=digest.hexdigest(),
        size_bytes=size_bytes,
    )


async def register_local_artifact(
    session: AsyncSession,
    *,
    run: Run,
    storage_root: Path,
    file_path: Path,
    idempotency_key: str,
    artifact_type: str,
    media_type: str,
    origin: str,
    step_run: StepRun | None = None,
    sensitivity: ArtifactSensitivity = ArtifactSensitivity.INTERNAL,
    retention_days: int | None = None,
    expected_sha256: str | None = None,
) -> ArtifactRegistrationResult:
    """Inspect one existing local file and persist verified, idempotent metadata.

    This function never creates, copies, changes, or deletes the underlying file.
    """
    if run.id is None:
        raise ValueError("run must be persisted before registering an artifact")
    if step_run is not None:
        if step_run.id is None:
            raise ValueError("step_run must be persisted before registering an artifact")
        if step_run.run_id != run.id:
            raise ValueError("step_run must belong to the artifact run")
    if retention_days is not None and retention_days < 1:
        raise ValueError("retention_days must be at least 1")

    idempotency_key = _require_text(
        idempotency_key,
        "idempotency_key",
        max_length=255,
    )
    artifact_type = _require_text(
        artifact_type,
        "artifact_type",
        max_length=100,
    )
    media_type = _require_text(media_type, "media_type", max_length=255)
    origin = _require_text(origin, "origin", max_length=200)
    expected_digest = _normalize_expected_sha256(expected_sha256)
    inspected = _inspect_local_file(storage_root, file_path)
    if expected_digest is not None and inspected.sha256 != expected_digest:
        raise ArtifactStorageError("artifact checksum does not match expected_sha256")

    existing = await session.scalar(
        select(Artifact).where(
            Artifact.run_id == run.id,
            Artifact.idempotency_key == idempotency_key,
        )
    )
    expected = (
        step_run.id if step_run is not None else None,
        artifact_type,
        inspected.relative_path,
        media_type,
        inspected.sha256,
        inspected.size_bytes,
        origin,
        sensitivity.value,
        retention_days,
    )
    if existing is not None:
        actual = (
            existing.step_run_id,
            existing.artifact_type,
            existing.relative_path,
            existing.media_type,
            existing.sha256,
            existing.size_bytes,
            existing.origin,
            existing.sensitivity,
            existing.retention_days,
        )
        if actual != expected:
            raise IdempotencyConflictError(
                "artifact idempotency key already exists with different metadata"
            )
        return ArtifactRegistrationResult(existing, created=False)

    now = _utcnow()
    artifact = Artifact(
        run_id=run.id,
        step_run_id=step_run.id if step_run is not None else None,
        idempotency_key=idempotency_key,
        artifact_type=artifact_type,
        relative_path=inspected.relative_path,
        media_type=media_type,
        sha256=inspected.sha256,
        size_bytes=inspected.size_bytes,
        origin=origin,
        sensitivity=sensitivity.value,
        retention_days=retention_days,
        retention_until=(
            now + timedelta(days=retention_days)
            if retention_days is not None
            else None
        ),
        created_at=now,
        verified_at=now,
    )
    session.add(artifact)
    await session.flush()
    return ArtifactRegistrationResult(artifact, created=True)
