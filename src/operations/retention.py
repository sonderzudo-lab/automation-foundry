"""Read-only retention inventory for the local artifact storage root.

This module never creates, moves, changes, or deletes a file. It reconciles the
files below ``STORAGE_ROOT`` with the ``Artifact`` records that describe them and
classifies every item so that a future, separately approved slice can reason
about material deletion. Every classification here is deliberately conservative:
anything the inventory cannot fully explain is held instead of marked expired.
"""

from __future__ import annotations

import os
import stat
from collections import Counter, defaultdict
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import Settings, settings
from src.platform.models import (
    Approval,
    ApprovalStatus,
    Artifact,
    Automation,
    Run,
    RunStatus,
)

_EXCLUDED_STORAGE_NAMES = frozenset({"backups", "runtime"})
_EXCLUDED_FILE_NAMES = frozenset({".gitkeep"})
_OPEN_RUN_STATUSES = frozenset(
    {
        RunStatus.QUEUED.value,
        RunStatus.RUNNING.value,
        RunStatus.AWAITING_APPROVAL.value,
    }
)


class RetentionInventoryError(ValueError):
    """Raised when the storage root cannot be inventoried safely."""


class RetentionClass(StrEnum):
    """Mutually exclusive outcome of reconciling one file or one record."""

    RETAINED = "retained"
    RETENTION_HOLD = "retention_hold"
    EXPIRED = "expired"
    PURGED = "purged"
    ORPHAN = "orphan"
    MISSING = "missing"
    UNSAFE = "unsafe"


@dataclass(frozen=True, slots=True)
class RetentionItem:
    """One reconciled artifact record or one unreferenced file on disk.

    ``relative_path`` is operator evidence for the local terminal. It must not be
    rendered by the dashboard; use :class:`RetentionClassSummary` there instead.
    """

    classification: RetentionClass
    reason: str
    relative_path: str
    size_bytes: int
    artifact_id: int | None = None
    run_id: int | None = None
    automation_slug: str | None = None
    artifact_type: str | None = None
    sensitivity: str | None = None
    registered_size_bytes: int | None = None
    retention_days: int | None = None
    retention_until: datetime | None = None
    evidence_matches: bool | None = None


@dataclass(frozen=True, slots=True)
class RetentionClassSummary:
    """Redacted count and byte total for one classification."""

    classification: RetentionClass
    file_count: int
    total_bytes: int


@dataclass(frozen=True, slots=True)
class RetentionAutomationSummary:
    """Redacted per-automation totals over registered artifacts only."""

    automation_slug: str
    file_count: int
    total_bytes: int
    expired_file_count: int
    expired_bytes: int
    hold_file_count: int
    missing_file_count: int


@dataclass(frozen=True, slots=True)
class RetentionInventory:
    """Complete, read-only reconciliation of the local storage root."""

    generated_at: datetime
    storage_root_present: bool
    scanned_file_count: int
    scanned_bytes: int
    excluded_file_count: int
    excluded_bytes: int
    classes: tuple[RetentionClassSummary, ...]
    automations: tuple[RetentionAutomationSummary, ...]
    items: tuple[RetentionItem, ...]
    dry_run: bool = True
    deleted_file_count: int = 0
    deleted_bytes: int = 0

    def summary_for(self, classification: RetentionClass) -> RetentionClassSummary:
        """Return the summary of one classification, zeroed when it is absent."""
        for entry in self.classes:
            if entry.classification is classification:
                return entry
        return RetentionClassSummary(classification, file_count=0, total_bytes=0)


@dataclass(frozen=True, slots=True)
class _DiskScan:
    files: dict[str, int]
    unsafe: dict[str, str]
    excluded_file_count: int
    excluded_bytes: int


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _resolve_storage_root(path: Path) -> Path | None:
    """Resolve the configured root without creating or changing anything."""
    if path.is_symlink():
        raise RetentionInventoryError("storage root must not be a symlink")
    try:
        root = path.resolve()
    except OSError as exc:  # pragma: no cover - platform dependent
        raise RetentionInventoryError("storage root is unavailable") from exc
    if root == Path(root.anchor):
        raise RetentionInventoryError("storage root must not be a drive root")
    if not root.exists():
        return None
    if not root.is_dir():
        raise RetentionInventoryError("storage root must be a real directory")
    return root


def _is_contained(candidate: Path, root: Path) -> bool:
    try:
        resolved = candidate.resolve()
    except OSError:
        return False
    if resolved == root:
        return True
    return root in resolved.parents


def _normalize_registered_path(value: str) -> str | None:
    """Return a safe posix relative path, or ``None`` when it escapes the root."""
    normalized = value.strip().replace("\\", "/")
    if not normalized:
        return None
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or pure.drive:
        return None
    parts = pure.parts
    if not parts or any(part in {"..", ""} for part in parts):
        return None
    return pure.as_posix()


def _aggregate_excluded_directory(directory: Path) -> tuple[int, int]:
    file_count = 0
    total_bytes = 0
    for current_root, directory_names, filenames in os.walk(
        directory,
        topdown=True,
        followlinks=False,
    ):
        current = Path(current_root)
        directory_names[:] = [
            name for name in directory_names if not (current / name).is_symlink()
        ]
        for filename in filenames:
            candidate = current / filename
            if candidate.is_symlink():
                continue
            try:
                stat_result = candidate.stat()
            except OSError:
                continue
            if not stat.S_ISREG(stat_result.st_mode):
                continue
            file_count += 1
            total_bytes += stat_result.st_size
    return file_count, total_bytes


def _scan_storage(root: Path, *, max_files: int) -> _DiskScan:
    """Walk the root once without following links and without reading contents."""
    files: dict[str, int] = {}
    unsafe: dict[str, str] = {}
    excluded_file_count = 0
    excluded_bytes = 0
    scanned = 0

    for current_root, directory_names, filenames in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        current = Path(current_root)
        if current == root:
            for name in sorted(
                name for name in directory_names if name in _EXCLUDED_STORAGE_NAMES
            ):
                count, size = _aggregate_excluded_directory(root / name)
                excluded_file_count += count
                excluded_bytes += size
            directory_names[:] = sorted(
                name for name in directory_names if name not in _EXCLUDED_STORAGE_NAMES
            )
        else:
            directory_names.sort()

        for name in list(directory_names):
            candidate = current / name
            if candidate.is_symlink():
                directory_names.remove(name)
                unsafe[_relative_label(candidate, root)] = "symlinked_directory"
            elif not _is_contained(candidate, root):
                directory_names.remove(name)
                unsafe[_relative_label(candidate, root)] = "directory_outside_storage_root"

        for filename in sorted(filenames):
            source = current / filename
            relative = _relative_label(source, root)
            if filename in _EXCLUDED_FILE_NAMES:
                excluded_file_count += 1
                with suppress(OSError):
                    excluded_bytes += source.stat().st_size
                continue
            scanned += 1
            if scanned > max_files:
                raise RetentionInventoryError(
                    "storage root exceeds the configured inventory file limit"
                )
            if source.is_symlink():
                unsafe[relative] = "symlinked_file"
                continue
            try:
                stat_result = source.stat()
            except OSError:
                unsafe[relative] = "unreadable_file"
                continue
            if not stat.S_ISREG(stat_result.st_mode):
                unsafe[relative] = "irregular_file"
                continue
            if not _is_contained(source, root):
                unsafe[relative] = "file_outside_storage_root"
                continue
            files[relative] = stat_result.st_size

    return _DiskScan(
        files=files,
        unsafe=unsafe,
        excluded_file_count=excluded_file_count,
        excluded_bytes=excluded_bytes,
    )


def _relative_label(candidate: Path, root: Path) -> str:
    try:
        return candidate.relative_to(root).as_posix()
    except ValueError:  # pragma: no cover - defensive; os.walk stays below root
        return candidate.name


async def collect_retention_inventory(
    session: AsyncSession,
    *,
    configuration: Settings = settings,
    now: datetime | None = None,
    max_files: int | None = None,
) -> RetentionInventory:
    """Reconcile storage with artifact records without deleting or changing data."""
    moment = now or _utcnow()
    limit = max_files or configuration.retention_inventory_max_files
    if limit < 1:
        raise RetentionInventoryError("inventory file limit must be at least 1")

    root = _resolve_storage_root(Path(configuration.storage_root))
    scan = (
        _scan_storage(root, max_files=limit)
        if root is not None
        else _DiskScan(files={}, unsafe={}, excluded_file_count=0, excluded_bytes=0)
    )

    rows = (
        await session.execute(
            select(Artifact, Run.status, Automation.slug)
            .join(Run, Artifact.run_id == Run.id)
            .join(Automation, Run.automation_id == Automation.id)
            .order_by(Artifact.id)
        )
    ).all()
    pending_run_ids = set(
        (
            await session.scalars(
                select(Approval.run_id).where(
                    Approval.status == ApprovalStatus.PENDING.value
                )
            )
        ).all()
    )

    # Already purged records no longer compete for a path, so they must not keep a
    # live artifact on hold forever.
    path_references: Counter[str] = Counter()
    for artifact, _run_status, _slug in rows:
        if artifact.purged_at is not None:
            continue
        normalized = _normalize_registered_path(artifact.relative_path)
        if normalized is not None:
            path_references[normalized] += 1

    items: list[RetentionItem] = []
    consumed: set[str] = set()

    for artifact, run_status, automation_slug in rows:
        items.append(
            _classify_record(
                artifact,
                run_status=run_status,
                automation_slug=automation_slug,
                scan=scan,
                pending_run_ids=pending_run_ids,
                path_references=path_references,
                consumed=consumed,
                moment=moment,
            )
        )

    for relative_path in sorted(set(scan.files) - consumed):
        items.append(
            RetentionItem(
                classification=RetentionClass.ORPHAN,
                reason="no_artifact_record",
                relative_path=relative_path,
                size_bytes=scan.files[relative_path],
            )
        )

    registered_unsafe = {
        item.relative_path for item in items if item.classification is RetentionClass.UNSAFE
    }
    for relative_path in sorted(set(scan.unsafe) - registered_unsafe):
        items.append(
            RetentionItem(
                classification=RetentionClass.UNSAFE,
                reason=scan.unsafe[relative_path],
                relative_path=relative_path,
                size_bytes=0,
            )
        )

    return RetentionInventory(
        generated_at=moment,
        storage_root_present=root is not None,
        scanned_file_count=len(scan.files),
        scanned_bytes=sum(scan.files.values()),
        excluded_file_count=scan.excluded_file_count,
        excluded_bytes=scan.excluded_bytes,
        classes=_class_summaries(items),
        automations=_automation_summaries(items),
        items=tuple(items),
    )


def _classify_record(
    artifact: Artifact,
    *,
    run_status: str,
    automation_slug: str,
    scan: _DiskScan,
    pending_run_ids: set[int],
    path_references: Counter[str],
    consumed: set[str],
    moment: datetime,
) -> RetentionItem:
    def build(
        classification: RetentionClass,
        reason: str,
        *,
        relative_path: str,
        size_bytes: int,
        evidence_matches: bool | None = None,
    ) -> RetentionItem:
        return RetentionItem(
            classification=classification,
            reason=reason,
            relative_path=relative_path,
            size_bytes=size_bytes,
            artifact_id=artifact.id,
            run_id=artifact.run_id,
            automation_slug=automation_slug,
            artifact_type=artifact.artifact_type,
            sensitivity=artifact.sensitivity,
            registered_size_bytes=artifact.size_bytes,
            retention_days=artifact.retention_days,
            retention_until=artifact.retention_until,
            evidence_matches=evidence_matches,
        )

    normalized = _normalize_registered_path(artifact.relative_path)
    if normalized is None:
        return build(
            RetentionClass.UNSAFE,
            "registered_path_escapes_storage_root",
            relative_path=_redacted_path_label(artifact),
            size_bytes=0,
        )
    if artifact.purged_at is not None:
        # An approved purge already removed this file. A file that reappears at the
        # same path is evidence of something the inventory cannot explain.
        if normalized in scan.files:
            consumed.add(normalized)
            return build(
                RetentionClass.UNSAFE,
                "purged_file_reappeared",
                relative_path=normalized,
                size_bytes=scan.files[normalized],
                evidence_matches=False,
            )
        return build(
            RetentionClass.PURGED,
            "purged_after_approval",
            relative_path=normalized,
            size_bytes=0,
        )
    if normalized in scan.unsafe:
        return build(
            RetentionClass.UNSAFE,
            scan.unsafe[normalized],
            relative_path=normalized,
            size_bytes=0,
        )
    if normalized not in scan.files:
        return build(
            RetentionClass.MISSING,
            "registered_file_absent",
            relative_path=normalized,
            size_bytes=0,
            evidence_matches=False,
        )

    consumed.add(normalized)
    disk_bytes = scan.files[normalized]
    evidence_matches = disk_bytes == artifact.size_bytes
    expired = (
        artifact.retention_until is not None and artifact.retention_until <= moment
    )
    if not expired:
        return build(
            RetentionClass.RETAINED,
            (
                "no_retention_policy"
                if artifact.retention_until is None
                else "within_retention"
            ),
            relative_path=normalized,
            size_bytes=disk_bytes,
            evidence_matches=evidence_matches,
        )

    hold_reason = _hold_reason(
        artifact,
        run_status=run_status,
        pending_run_ids=pending_run_ids,
        shared=path_references[normalized] > 1,
        evidence_matches=evidence_matches,
    )
    if hold_reason is not None:
        return build(
            RetentionClass.RETENTION_HOLD,
            hold_reason,
            relative_path=normalized,
            size_bytes=disk_bytes,
            evidence_matches=evidence_matches,
        )
    return build(
        RetentionClass.EXPIRED,
        "retention_elapsed",
        relative_path=normalized,
        size_bytes=disk_bytes,
        evidence_matches=evidence_matches,
    )


def _hold_reason(
    artifact: Artifact,
    *,
    run_status: str,
    pending_run_ids: set[int],
    shared: bool,
    evidence_matches: bool,
) -> str | None:
    """Return why an elapsed artifact must not be treated as expired."""
    if run_status in _OPEN_RUN_STATUSES:
        return "open_run"
    if artifact.run_id in pending_run_ids:
        return "pending_approval"
    if not evidence_matches:
        return "evidence_mismatch"
    if shared:
        return "shared_path"
    return None


def _redacted_path_label(artifact: Artifact) -> str:
    """Return a stable label for a record whose stored path cannot be trusted."""
    return f"<artifact:{artifact.id}>"


def _class_summaries(
    items: list[RetentionItem],
) -> tuple[RetentionClassSummary, ...]:
    counts: Counter[RetentionClass] = Counter()
    total_bytes: Counter[RetentionClass] = Counter()
    for item in items:
        counts[item.classification] += 1
        total_bytes[item.classification] += item.size_bytes
    return tuple(
        RetentionClassSummary(
            classification=classification,
            file_count=counts[classification],
            total_bytes=total_bytes[classification],
        )
        for classification in RetentionClass
    )


def _automation_summaries(
    items: list[RetentionItem],
) -> tuple[RetentionAutomationSummary, ...]:
    grouped: dict[str, list[RetentionItem]] = defaultdict(list)
    for item in items:
        if item.automation_slug is not None:
            grouped[item.automation_slug].append(item)
    return tuple(
        RetentionAutomationSummary(
            automation_slug=slug,
            file_count=len(entries),
            total_bytes=sum(entry.size_bytes for entry in entries),
            expired_file_count=sum(
                1 for entry in entries if entry.classification is RetentionClass.EXPIRED
            ),
            expired_bytes=sum(
                entry.size_bytes
                for entry in entries
                if entry.classification is RetentionClass.EXPIRED
            ),
            hold_file_count=sum(
                1
                for entry in entries
                if entry.classification is RetentionClass.RETENTION_HOLD
            ),
            missing_file_count=sum(
                1 for entry in entries if entry.classification is RetentionClass.MISSING
            ),
        )
        for slug, entries in sorted(grouped.items())
    )
