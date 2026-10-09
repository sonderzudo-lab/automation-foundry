"""Verified, local-only export projection for completed Content Engine runs.

This module does not create an upload, publish content, or copy operator data.
It reconstructs the approved A7/A8 evidence and exposes only exact local files
plus a canonical manifest that the loopback dashboard may offer for download.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.pipeline.final_review import (
    FINAL_REVIEW_APPROVAL_ACTION,
    FinalVideoReview,
    load_final_video_review,
)
from src.pipeline.thumbnail_review import (
    THUMBNAIL_REVIEW_APPROVAL_ACTION,
    load_thumbnail_review,
    thumbnail_decision_payload,
)
from src.platform.models import Approval, ApprovalStatus, Run, RunStatus

_MANIFEST_SCHEMA_VERSION = 1


class LocalExportNotAvailableError(ValueError):
    """Raised when a run has no completed, approved local export."""


class LocalExportIntegrityError(ValueError):
    """Raised when durable output and checksum-verified evidence diverge."""


@dataclass(frozen=True, slots=True)
class LocalExportFile:
    """One checksum-verified file available to the loopback dashboard."""

    role: str
    artifact_id: int
    filename: str
    media_type: str
    size_bytes: int
    sha256: str
    path: Path


@dataclass(frozen=True, slots=True)
class LocalExportPackage:
    """Canonical local export manifest and its verified files."""

    run_id: int
    final_review_approval_id: int
    thumbnail_review_approval_id: int | None
    video: LocalExportFile
    thumbnail: LocalExportFile | None
    manifest: dict[str, object]
    manifest_bytes: bytes
    manifest_sha256: str


async def load_local_export_package(
    session: AsyncSession,
    *,
    run: Run,
    storage_root: Path,
) -> LocalExportPackage:
    """Rebuild one local export from approved evidence, failing closed."""
    if run.id is None:
        raise LocalExportNotAvailableError("run must be persisted")
    if RunStatus(run.status) is not RunStatus.SUCCEEDED:
        raise LocalExportNotAvailableError("local export requires a succeeded run")
    output = run.output_payload
    if not isinstance(output, dict) or output.get("published") is not False:
        raise LocalExportNotAvailableError(
            "run does not contain a completed local-only review"
        )

    final_approval_id = _required_int(output, "final_review_approval_id")
    final_approval = await session.get(Approval, final_approval_id)
    if (
        final_approval is None
        or final_approval.run_id != run.id
        or final_approval.action != FINAL_REVIEW_APPROVAL_ACTION
        or ApprovalStatus(final_approval.status) is not ApprovalStatus.APPROVED
    ):
        raise LocalExportIntegrityError("local export is missing its approved A7 gate")
    try:
        final_review = await load_final_video_review(
            session,
            approval=final_approval,
            storage_root=storage_root,
        )
    except ValueError as exc:
        raise LocalExportIntegrityError(
            "local export final-video evidence is unavailable"
        ) from exc
    _require_output_match(
        output,
        final_video_artifact_id=final_review.video_artifact_id,
        final_video_sha256=final_review.video_sha256,
        originality_report_artifact_id=final_review.report_artifact_id,
        originality_report_sha256=final_review.report_sha256,
    )
    video = LocalExportFile(
        role="final_video",
        artifact_id=final_review.video_artifact_id,
        filename=f"run-{run.id}-final-video.mp4",
        media_type="video/mp4",
        size_bytes=final_review.video_size_bytes,
        sha256=final_review.video_sha256,
        path=final_review.video_path,
    )

    thumbnail, thumbnail_review_approval_id = await _load_thumbnail(
        session,
        run=run,
        output=output,
        storage_root=storage_root,
        final_review=final_review,
    )
    files = [
        _manifest_file(video),
        *([] if thumbnail is None else [_manifest_file(thumbnail)]),
    ]
    manifest: dict[str, object] = {
        "schema_version": _MANIFEST_SCHEMA_VERSION,
        "run_id": run.id,
        "published": False,
        "approvals": {
            "final_review_approval_id": final_approval_id,
            "thumbnail_review_approval_id": thumbnail_review_approval_id,
        },
        "files": files,
    }
    manifest_bytes = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return LocalExportPackage(
        run_id=run.id,
        final_review_approval_id=final_approval_id,
        thumbnail_review_approval_id=thumbnail_review_approval_id,
        video=video,
        thumbnail=thumbnail,
        manifest=manifest,
        manifest_bytes=manifest_bytes,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
    )


async def _load_thumbnail(
    session: AsyncSession,
    *,
    run: Run,
    output: dict[str, object],
    storage_root: Path,
    final_review: FinalVideoReview,
) -> tuple[LocalExportFile | None, int | None]:
    keys = (
        "thumbnail_artifact_id",
        "thumbnail_sha256",
        "thumbnail_review_approval_id",
    )
    present = tuple(key in output for key in keys)
    approvals = tuple(
        (
            await session.scalars(
                select(Approval).where(
                    Approval.run_id == run.id,
                    Approval.action == THUMBNAIL_REVIEW_APPROVAL_ACTION,
                )
            )
        ).all()
    )
    if not any(present):
        if approvals:
            raise LocalExportIntegrityError(
                "local export omits existing A8 evidence"
            )
        return None, None
    if not all(present):
        raise LocalExportIntegrityError("local export has partial A8 evidence")
    if len(approvals) != 1:
        raise LocalExportIntegrityError(
            "local export requires exactly one A8 approval"
        )

    approval_id = _required_int(output, "thumbnail_review_approval_id")
    approval = approvals[0]
    if (
        approval.id != approval_id
        or approval.run_id != run.id
        or approval.action != THUMBNAIL_REVIEW_APPROVAL_ACTION
        or ApprovalStatus(approval.status) is not ApprovalStatus.APPROVED
    ):
        raise LocalExportIntegrityError("local export is missing its approved A8 gate")
    try:
        review = await load_thumbnail_review(
            session,
            approval=approval,
            storage_root=storage_root,
            verified_final_review=final_review,
        )
        artifact_id = _required_int(output, "thumbnail_artifact_id")
        expected_decision = thumbnail_decision_payload(
            review,
            thumbnail_artifact_id=artifact_id,
        )
    except ValueError as exc:
        raise LocalExportIntegrityError(
            "local export thumbnail evidence is unavailable"
        ) from exc
    if approval.decision_payload != expected_decision:
        raise LocalExportIntegrityError("local export A8 decision payload diverges")
    _require_output_match(
        output,
        thumbnail_artifact_id=expected_decision["thumbnail_artifact_id"],
        thumbnail_sha256=expected_decision["thumbnail_sha256"],
    )
    selected = next(
        candidate
        for candidate in review.candidates
        if candidate.artifact_id == artifact_id
    )
    return (
        LocalExportFile(
            role="thumbnail",
            artifact_id=selected.artifact_id,
            filename=f"run-{run.id}-thumbnail.png",
            media_type="image/png",
            size_bytes=selected.size_bytes,
            sha256=selected.sha256,
            path=selected.image_path,
        ),
        approval_id,
    )


def _required_int(output: dict[str, object], key: str) -> int:
    value = output.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise LocalExportIntegrityError(f"local export field {key} is invalid")
    return value


def _require_output_match(
    output: dict[str, object],
    **expected: object,
) -> None:
    if any(output.get(key) != value for key, value in expected.items()):
        raise LocalExportIntegrityError("local export output evidence diverges")


def _manifest_file(file: LocalExportFile) -> dict[str, object]:
    return {
        "role": file.role,
        "artifact_id": file.artifact_id,
        "filename": file.filename,
        "media_type": file.media_type,
        "size_bytes": file.size_bytes,
        "sha256": file.sha256,
    }
