"""Human-approved purge of expired local artifacts.

This is the only code path in the project that deletes operator data. It is
disabled by default and deliberately split in two deliberate acts:

1. :func:`plan_retention_purge` turns the read-only retention inventory into an
   immutable, hash-verified list of expired artifacts and opens one human
   approval bound to the digest of exactly that list. Nothing is deleted.
2. :func:`execute_approved_purge` runs only after that approval, only with the
   runtime stopped, only after a verified backup of the current state, and it
   re-reads and re-hashes every single file immediately before removing it.

Any divergence between the approved plan and what is on disk aborts before the
first removal. A failure after the first removal is recorded as evidence and
leaves the run failed rather than silently half-applied; the pre-purge backup is
the documented recovery path.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import Settings, settings
from src.operations.backup import create_backup
from src.operations.retention import (
    RetentionClass,
    RetentionInventory,
    collect_retention_inventory,
)
from src.platform.approval_service import approval_payload_digest, request_approval
from src.platform.metric_service import record_metric_point
from src.platform.models import (
    Approval,
    ApprovalStatus,
    Artifact,
    Automation,
    MetricKind,
    QueueClass,
    Run,
    RunStatus,
)
from src.platform.run_service import (
    get_or_create_automation,
    get_or_create_run,
    transition_run,
)
from src.platform.step_service import get_or_create_step_run, transition_step_run
from src.runtime.windows import runtime_mutex_is_owned

PURGE_AUTOMATION_SLUG = "platform-storage-retention"
PURGE_AUTOMATION_NAME = "Plataforma · retenção de storage"
PURGE_APPROVAL_ACTION = "purge_expired_artifacts"
_PLAN_SCHEMA_VERSION = 1
_HASH_CHUNK_BYTES = 1024 * 1024
_MAX_PLAN_ENTRIES = 5_000


class PurgeError(ValueError):
    """Base error for every refusal on the approved purge path."""


class PurgeDisabledError(PurgeError):
    """Raised when the purge feature is not explicitly enabled locally."""


class PurgeConfigurationError(PurgeError):
    """Raised when the local environment is unsafe for a material deletion."""


class PurgeEvidenceError(PurgeError):
    """Raised when disk evidence no longer matches the approved plan."""


@dataclass(frozen=True, slots=True)
class PurgePlanEntry:
    """One expired artifact whose removal is being proposed."""

    artifact_id: int
    run_id: int
    automation_slug: str
    artifact_type: str
    sensitivity: str
    relative_path: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class PurgePlan:
    """An immutable, hash-bound proposal awaiting one human decision."""

    plan_digest: str
    entry_count: int
    total_bytes: int
    entries: tuple[PurgePlanEntry, ...]
    run_id: int | None = None
    approval_id: int | None = None
    created: bool = False

    @property
    def requires_decision(self) -> bool:
        """Report whether a human still has to decide about this plan."""
        return self.approval_id is not None


@dataclass(frozen=True, slots=True)
class PurgeExecutionResult:
    """Redacted evidence of one executed or replayed purge."""

    run_id: int
    approval_id: int
    plan_digest: str
    backup_id: str | None
    deleted_file_count: int
    deleted_bytes: int
    failed_entry_count: int
    replayed: bool


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _require_text(value: str, field: str, *, max_length: int = 200) -> str:
    normalized = value.strip()
    if not normalized:
        raise PurgeError(f"{field} must not be empty")
    if len(normalized) > max_length:
        raise PurgeError(f"{field} exceeds {max_length} characters")
    return normalized


def _resolve_within_storage(storage_root: Path, relative_path: str) -> Path:
    """Resolve one plan path strictly below the storage root, refusing links."""
    root = storage_root.resolve()
    candidate = (root / relative_path).resolve()
    if candidate != root and root not in candidate.parents:
        raise PurgeEvidenceError("purge path must remain below the storage root")
    if candidate.is_symlink():
        raise PurgeEvidenceError("purge path must not be a symlink")
    return candidate


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size_bytes = 0
    with path.open("rb") as handle:
        initial = os.fstat(handle.fileno())
        while chunk := handle.read(_HASH_CHUNK_BYTES):
            digest.update(chunk)
            size_bytes += len(chunk)
        final = os.fstat(handle.fileno())
    if initial.st_size != final.st_size or initial.st_mtime_ns != final.st_mtime_ns:
        raise PurgeEvidenceError("file changed while it was being verified")
    return digest.hexdigest(), size_bytes


def _verify_entry(storage_root: Path, entry: PurgePlanEntry) -> Path:
    """Re-read one file and refuse unless it is byte-for-byte the approved one."""
    path = _resolve_within_storage(storage_root, entry.relative_path)
    if not path.is_file():
        raise PurgeEvidenceError("approved file is no longer a regular file")
    digest, size_bytes = _hash_file(path)
    if digest != entry.sha256 or size_bytes != entry.size_bytes:
        raise PurgeEvidenceError("approved file no longer matches its checksum")
    return path


def _plan_payload(entries: tuple[PurgePlanEntry, ...]) -> dict[str, Any]:
    return {
        "schema_version": _PLAN_SCHEMA_VERSION,
        "entry_count": len(entries),
        "total_bytes": sum(entry.size_bytes for entry in entries),
        "entries": [
            {
                "artifact_id": entry.artifact_id,
                "relative_path": entry.relative_path,
                "sha256": entry.sha256,
                "size_bytes": entry.size_bytes,
            }
            for entry in entries
        ],
    }


def _entries_from_payload(payload: dict[str, Any]) -> tuple[PurgePlanEntry, ...]:
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        raise PurgeEvidenceError("approved plan has no entry list")
    entries: list[PurgePlanEntry] = []
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise PurgeEvidenceError("approved plan entry is malformed")
        entries.append(
            PurgePlanEntry(
                artifact_id=int(raw["artifact_id"]),
                run_id=0,
                automation_slug="",
                artifact_type="",
                sensitivity="",
                relative_path=str(raw["relative_path"]),
                sha256=str(raw["sha256"]),
                size_bytes=int(raw["size_bytes"]),
            )
        )
    return tuple(entries)


def _review_projection(entries: tuple[PurgePlanEntry, ...], digest: str) -> dict[str, str]:
    """Build the limited, path-free projection an operator decides on."""
    slugs = sorted({entry.automation_slug for entry in entries})
    types = sorted({entry.artifact_type for entry in entries})
    sensitivities = sorted({entry.sensitivity for entry in entries})
    total_bytes = sum(entry.size_bytes for entry in entries)
    return {
        "action": "Apagar permanentemente artefatos expirados do storage local.",
        "files": f"{len(entries)} arquivo(s)",
        "bytes": f"{total_bytes} bytes",
        "automations": ", ".join(slugs)[:300],
        "artifact_types": ", ".join(types)[:300],
        "sensitivity": ", ".join(sensitivities)[:300],
        "plan_digest": digest,
        "irreversible": (
            "A exclusão é irreversível. Um backup verificado é criado antes de "
            "remover qualquer arquivo e cada hash é reconferido no momento da remoção."
        ),
    }


async def plan_retention_purge(
    session: AsyncSession,
    *,
    actor: str,
    reason: str,
    configuration: Settings = settings,
    inventory: RetentionInventory | None = None,
) -> PurgePlan:
    """Propose one hash-verified purge and open its human approval gate."""
    if not configuration.retention_purge_enabled:
        raise PurgeDisabledError("retention purge is disabled by configuration")
    actor = _require_text(actor, "actor")
    reason = _require_text(reason, "reason", max_length=500)

    report = inventory or await collect_retention_inventory(
        session,
        configuration=configuration,
    )
    expired = [
        item
        for item in report.items
        if item.classification is RetentionClass.EXPIRED and item.artifact_id is not None
    ]
    if len(expired) > _MAX_PLAN_ENTRIES:
        raise PurgeConfigurationError(
            "purge plan exceeds the maximum number of reviewable entries"
        )

    storage_root = Path(configuration.storage_root)
    entries: list[PurgePlanEntry] = []
    for item in sorted(expired, key=lambda candidate: candidate.artifact_id or 0):
        artifact = await session.get(Artifact, item.artifact_id)
        if artifact is None:
            raise PurgeEvidenceError("expired artifact record disappeared while planning")
        if artifact.purged_at is not None:
            raise PurgeEvidenceError("expired artifact is already recorded as purged")
        candidate = PurgePlanEntry(
            artifact_id=artifact.id,
            run_id=artifact.run_id,
            automation_slug=item.automation_slug or "",
            artifact_type=artifact.artifact_type,
            sensitivity=artifact.sensitivity,
            relative_path=item.relative_path,
            sha256=artifact.sha256,
            size_bytes=artifact.size_bytes,
        )
        # Fail the whole plan rather than silently proposing a subset.
        _verify_entry(storage_root, candidate)
        entries.append(candidate)

    proposal = tuple(entries)
    payload = _plan_payload(proposal)
    digest = approval_payload_digest(payload)
    plan = PurgePlan(
        plan_digest=digest,
        entry_count=len(proposal),
        total_bytes=payload["total_bytes"],
        entries=proposal,
    )
    if not proposal:
        return plan

    automation = (
        await get_or_create_automation(
            session,
            slug=PURGE_AUTOMATION_SLUG,
            name=PURGE_AUTOMATION_NAME,
            owner=actor,
        )
    ).automation
    creation = await get_or_create_run(
        session,
        automation=automation,
        idempotency_key=f"purge:{digest}",
        input_payload={"plan": payload, "reason": reason},
    )
    run = creation.run
    if RunStatus(run.status) is RunStatus.QUEUED:
        await transition_run(session, run, RunStatus.RUNNING, note="purge plan prepared")
    existing_approval = await session.scalar(
        select(Approval).where(
            Approval.run_id == run.id,
            Approval.action == PURGE_APPROVAL_ACTION,
        )
    )
    if existing_approval is not None:
        return PurgePlan(
            plan_digest=digest,
            entry_count=len(proposal),
            total_bytes=payload["total_bytes"],
            entries=proposal,
            run_id=run.id,
            approval_id=existing_approval.id,
            created=False,
        )

    step = (
        await get_or_create_step_run(
            session,
            run=run,
            name="plan-purge",
            queue=QueueClass.IO,
            ordinal=1,
            idempotency_key=f"purge:{digest}:plan",
        )
    ).step_run
    await transition_step_run(
        session,
        step,
        RunStatus.RUNNING,
        note="verifying expired artifacts",
    )
    await transition_step_run(
        session,
        step,
        RunStatus.SUCCEEDED,
        output_payload={
            "entry_count": len(proposal),
            "total_bytes": payload["total_bytes"],
        },
    )
    approval = (
        await request_approval(
            session,
            run=run,
            idempotency_key=f"purge:{digest}",
            action=PURGE_APPROVAL_ACTION,
            summary=(
                f"Apagar {len(proposal)} artefato(s) expirado(s) "
                f"({payload['total_bytes']} bytes) do storage local."
            ),
            input_payload=payload,
            review_payload=_review_projection(proposal, digest),
        )
    ).approval

    return PurgePlan(
        plan_digest=digest,
        entry_count=len(proposal),
        total_bytes=payload["total_bytes"],
        entries=proposal,
        run_id=run.id,
        approval_id=approval.id,
        created=True,
    )


async def execute_approved_purge(
    session: AsyncSession,
    *,
    approval_id: int,
    confirmation: str,
    actor: str,
    configuration: Settings = settings,
    repository_root: Path | None = None,
) -> PurgeExecutionResult:
    """Delete exactly the approved files after re-verifying every one of them."""
    if not configuration.retention_purge_enabled:
        raise PurgeDisabledError("retention purge is disabled by configuration")
    actor = _require_text(actor, "actor")
    if runtime_mutex_is_owned():
        raise PurgeConfigurationError("runtime must be stopped before a purge")

    approval = await session.get(Approval, approval_id)
    if approval is None:
        raise PurgeError("approval does not exist")
    if approval.action != PURGE_APPROVAL_ACTION:
        raise PurgeError("approval does not authorize a storage purge")
    if ApprovalStatus(approval.status) is not ApprovalStatus.APPROVED:
        raise PurgeError("purge requires an approved human decision")

    run = await session.get(Run, approval.run_id)
    if run is None:
        raise PurgeError("purge run does not exist")

    stored_plan = run.input_payload.get("plan")
    if not isinstance(stored_plan, dict):
        raise PurgeEvidenceError("purge run does not carry its approved plan")
    digest = approval_payload_digest(stored_plan)
    if digest != approval.payload_digest:
        raise PurgeEvidenceError("stored purge plan no longer matches the approval")
    if confirmation.strip() != f"PURGE {digest}":
        raise PurgeError("confirmation must repeat the exact approved plan digest")

    status = RunStatus(run.status)
    if status in {RunStatus.SUCCEEDED, RunStatus.FAILED}:
        return _replayed_result(run, approval_id=approval_id, plan_digest=digest)
    if status is not RunStatus.RUNNING:
        raise PurgeError("purge run is not ready to execute")

    entries = _entries_from_payload(stored_plan)
    storage_root = Path(configuration.storage_root)

    step = (
        await get_or_create_step_run(
            session,
            run=run,
            name="execute-purge",
            queue=QueueClass.IO,
            ordinal=2,
            idempotency_key=f"purge:{digest}:execute",
            approval_id=approval_id,
        )
    ).step_run
    await transition_step_run(session, step, RunStatus.RUNNING, note="verifying evidence")

    # Full verification pass first: nothing is removed unless every approved file
    # is still exactly the file that was approved.
    verified: list[tuple[PurgePlanEntry, Path]] = []
    for entry in entries:
        verified.append((entry, _verify_entry(storage_root, entry)))

    backup = create_backup(configuration, repository_root=repository_root)

    deleted_ids: list[int] = []
    deleted_bytes = 0
    failure: str | None = None
    for entry, path in verified:
        try:
            # Re-verify immediately before the removal to narrow the window in
            # which the file could have been replaced.
            _verify_entry(storage_root, entry)
            path.unlink()
        except (OSError, PurgeEvidenceError) as exc:
            failure = type(exc).__name__
            break
        deleted_ids.append(entry.artifact_id)
        deleted_bytes += entry.size_bytes

    purged_at = _utcnow()
    for artifact_id in deleted_ids:
        artifact = await session.get(Artifact, artifact_id)
        if artifact is not None:
            artifact.purged_at = purged_at
            artifact.purged_by_approval_id = approval_id
    await session.flush()

    failed_entry_count = len(entries) - len(deleted_ids)
    evidence: dict[str, Any] = {
        "plan_digest": digest,
        "backup_id": backup.backup_id,
        "deleted_file_count": len(deleted_ids),
        "deleted_bytes": deleted_bytes,
        "failed_entry_count": failed_entry_count,
        "actor": actor,
    }
    if failure is not None:
        evidence["failure"] = failure

    await _record_purge_metrics(
        session,
        run=run,
        step_run_id=step.id,
        digest=digest,
        deleted_file_count=len(deleted_ids),
        deleted_bytes=deleted_bytes,
    )

    if failure is None:
        await transition_step_run(
            session,
            step,
            RunStatus.SUCCEEDED,
            output_payload=evidence,
        )
        await transition_run(
            session,
            run,
            RunStatus.SUCCEEDED,
            output_payload={"purge": evidence},
            note="approved purge completed",
        )
    else:
        await transition_step_run(
            session,
            step,
            RunStatus.FAILED,
            error={"code": "PURGE_PARTIAL", "detail": failure},
        )
        await transition_run(
            session,
            run,
            RunStatus.FAILED,
            output_payload={"purge": evidence},
            error={"code": "PURGE_PARTIAL", "detail": failure},
            note="approved purge stopped after a divergence",
        )

    return PurgeExecutionResult(
        run_id=run.id,
        approval_id=approval_id,
        plan_digest=digest,
        backup_id=backup.backup_id,
        deleted_file_count=len(deleted_ids),
        deleted_bytes=deleted_bytes,
        failed_entry_count=failed_entry_count,
        replayed=False,
    )


def _replayed_result(
    run: Run,
    *,
    approval_id: int,
    plan_digest: str,
) -> PurgeExecutionResult:
    evidence = (run.output_payload or {}).get("purge", {})
    if not isinstance(evidence, dict):
        evidence = {}
    backup_id = evidence.get("backup_id")
    return PurgeExecutionResult(
        run_id=run.id,
        approval_id=approval_id,
        plan_digest=plan_digest,
        backup_id=backup_id if isinstance(backup_id, str) else None,
        deleted_file_count=int(evidence.get("deleted_file_count", 0)),
        deleted_bytes=int(evidence.get("deleted_bytes", 0)),
        failed_entry_count=int(evidence.get("failed_entry_count", 0)),
        replayed=True,
    )


async def _record_purge_metrics(
    session: AsyncSession,
    *,
    run: Run,
    step_run_id: int | None,
    digest: str,
    deleted_file_count: int,
    deleted_bytes: int,
) -> None:
    automation = await session.get(Automation, run.automation_id)
    if automation is None:  # pragma: no cover - defensive
        return
    step_run = None
    if step_run_id is not None:
        from src.platform.models import StepRun

        step_run = await session.get(StepRun, step_run_id)
    for name, value, unit in (
        ("storage.purged_files", Decimal(deleted_file_count), "count"),
        ("storage.purged_bytes", Decimal(deleted_bytes), "bytes"),
    ):
        await record_metric_point(
            session,
            automation=automation,
            run=run,
            step_run=step_run,
            idempotency_key=f"purge:{digest}:{name}",
            name=name,
            kind=MetricKind.COUNTER,
            value=value,
            unit=unit,
            source="storage-retention",
        )
