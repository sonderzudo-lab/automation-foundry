"""Content Engine A8: local human selection of one verified A3 thumbnail.

A8 is enabled only after the A7 final-video approval. It freezes the exact set
of A3 ``visual_image`` artifacts in the approval digest, serves only verified
local PNGs, and requires one server-validated selection. Approving A8 concludes
the same run locally with ``published=false``; it never uploads or publishes.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.pipeline.final_review import (
    FINAL_REVIEW_APPROVAL_ACTION,
    FINAL_REVIEW_STEP_NAME,
    FinalVideoReview,
    load_final_video_review,
)
from src.pipeline.visuals import VISUAL_STEP_NAME
from src.platform.approval_service import approval_payload_digest, request_approval
from src.platform.models import (
    Approval,
    ApprovalStatus,
    Artifact,
    LedgerEntry,
    MetricPoint,
    Run,
    RunStatus,
    StepRun,
)
from src.platform.run_service import transition_run

THUMBNAIL_REVIEW_APPROVAL_ACTION = "select_content_thumbnail_a8"
_A3_ARTIFACT_ORIGIN = "content-engine:a3"
_MAX_CANDIDATES = 60
_MAX_IMAGE_BYTES = 50 * 1024 * 1024
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_APPROVAL_SCHEMA_VERSION = 1


class ThumbnailReviewGateError(ValueError):
    """Raised when A8 evidence or a requested selection is unsafe."""


@dataclass(frozen=True, slots=True)
class ThumbnailReviewRequestResult:
    """Observable identifiers produced when A8 opens its human gate."""

    run_id: int
    approval_id: int
    run_status: str
    created: bool


@dataclass(frozen=True, slots=True)
class ThumbnailCandidateReview:
    """One checksum-verified A3 PNG candidate.

    ``image_path`` is server-side only and must never be rendered in a template.
    """

    artifact_id: int
    sha256: str
    size_bytes: int
    image_path: Path


@dataclass(frozen=True, slots=True)
class ThumbnailReview:
    """Complete A8 projection bound to one immutable candidate set."""

    approval_id: int
    run_id: int
    final_review_approval_id: int
    final_video_sha256: str
    candidate_set_sha256: str
    candidates: tuple[ThumbnailCandidateReview, ...]


async def request_thumbnail_approval(
    session: AsyncSession,
    *,
    final_review_approval: Approval,
    storage_root: Path,
) -> ThumbnailReviewRequestResult:
    """Open A8 after an approved and still-integral A7 review."""
    if final_review_approval.action != FINAL_REVIEW_APPROVAL_ACTION:
        raise ThumbnailReviewGateError("A8 requires the exact A7 approval")
    if ApprovalStatus(final_review_approval.status) is not ApprovalStatus.APPROVED:
        raise ThumbnailReviewGateError("A7 final review is not approved")
    run = await session.get(Run, final_review_approval.run_id)
    if run is None:
        raise ThumbnailReviewGateError("A7 approval run does not exist")
    if RunStatus(run.status) not in {
        RunStatus.RUNNING,
        RunStatus.AWAITING_APPROVAL,
    }:
        raise ThumbnailReviewGateError("A8 gate requires an active run")
    final_review = await load_final_video_review(
        session,
        approval=final_review_approval,
        storage_root=storage_root,
    )
    candidates = await _verified_candidates(
        session,
        run=run,
        storage_root=storage_root,
    )
    creation = await request_approval(
        session,
        run=run,
        idempotency_key=f"{run.idempotency_key}:thumbnail-review",
        action=THUMBNAIL_REVIEW_APPROVAL_ACTION,
        summary="Escolher uma thumbnail local entre os PNGs verificados da A3",
        input_payload=_approval_input(
            run=run,
            final_review_approval=final_review_approval,
            final_review=final_review,
            candidates=candidates,
        ),
        review_payload=_review_payload(
            final_review=final_review,
            candidates=candidates,
        ),
    )
    return ThumbnailReviewRequestResult(
        run_id=_persisted_id(run.id, "run"),
        approval_id=_persisted_id(creation.approval.id, "approval"),
        run_status=run.status,
        created=creation.created,
    )


async def load_thumbnail_review(
    session: AsyncSession,
    *,
    approval: Approval,
    storage_root: Path,
    verified_final_review: FinalVideoReview | None = None,
) -> ThumbnailReview:
    """Reconstruct and verify the frozen A8 candidate set."""
    if approval.action != THUMBNAIL_REVIEW_APPROVAL_ACTION:
        raise ThumbnailReviewGateError("approval is not an A8 thumbnail review")
    run = await session.get(Run, approval.run_id)
    if run is None:
        raise ThumbnailReviewGateError("approval run does not exist")
    final_approval = await _required_final_review_approval(session, run=run)
    if verified_final_review is None:
        final_review = await load_final_video_review(
            session,
            approval=final_approval,
            storage_root=storage_root,
        )
    else:
        if (
            verified_final_review.run_id != run.id
            or verified_final_review.approval_id != final_approval.id
        ):
            raise ThumbnailReviewGateError(
                "preverified A7 evidence belongs to a different approval"
            )
        final_review = verified_final_review
    candidates = await _verified_candidates(
        session,
        run=run,
        storage_root=storage_root,
    )
    expected = _approval_input(
        run=run,
        final_review_approval=final_approval,
        final_review=final_review,
        candidates=candidates,
    )
    if approval.payload_digest != approval_payload_digest(expected):
        raise ThumbnailReviewGateError(
            "approval does not match the frozen thumbnail candidates"
        )
    return ThumbnailReview(
        approval_id=_persisted_id(approval.id, "approval"),
        run_id=_persisted_id(run.id, "run"),
        final_review_approval_id=_persisted_id(final_approval.id, "approval"),
        final_video_sha256=final_review.video_sha256,
        candidate_set_sha256=_candidate_set_sha256(candidates),
        candidates=candidates,
    )


def thumbnail_decision_payload(
    review: ThumbnailReview,
    *,
    thumbnail_artifact_id: int,
) -> dict[str, object]:
    """Validate one browser selection against the server-verified frozen set."""
    if not isinstance(thumbnail_artifact_id, int) or isinstance(
        thumbnail_artifact_id, bool
    ):
        raise ThumbnailReviewGateError("thumbnail artifact ID is invalid")
    selected = next(
        (
            candidate
            for candidate in review.candidates
            if candidate.artifact_id == thumbnail_artifact_id
        ),
        None,
    )
    if selected is None:
        raise ThumbnailReviewGateError(
            "thumbnail artifact is not in the frozen candidate set"
        )
    return {
        "thumbnail_artifact_id": selected.artifact_id,
        "thumbnail_sha256": selected.sha256,
    }


async def finalize_thumbnail_approval(
    session: AsyncSession,
    *,
    approval: Approval,
    storage_root: Path,
) -> bool:
    """Conclude an approved A8 run locally with one exact thumbnail selection."""
    if approval.action != THUMBNAIL_REVIEW_APPROVAL_ACTION:
        return False
    if ApprovalStatus(approval.status) is not ApprovalStatus.APPROVED:
        raise ThumbnailReviewGateError("A8 thumbnail review is not approved")
    review = await load_thumbnail_review(
        session,
        approval=approval,
        storage_root=storage_root,
    )
    decision = approval.decision_payload
    if not isinstance(decision, dict) or set(decision) != {
        "thumbnail_artifact_id",
        "thumbnail_sha256",
    }:
        raise ThumbnailReviewGateError("A8 decision payload is missing or invalid")
    artifact_id = decision.get("thumbnail_artifact_id")
    if not isinstance(artifact_id, int) or isinstance(artifact_id, bool):
        raise ThumbnailReviewGateError("A8 decision payload is missing or invalid")
    expected_decision = thumbnail_decision_payload(
        review,
        thumbnail_artifact_id=artifact_id,
    )
    if decision != expected_decision:
        raise ThumbnailReviewGateError(
            "A8 decision payload diverges from the frozen candidate"
        )
    run = await session.get(Run, approval.run_id)
    if run is None:
        raise ThumbnailReviewGateError("approval run does not exist")
    output_payload = await _output_payload(
        session,
        approval=approval,
        review=review,
        decision=expected_decision,
        storage_root=storage_root,
    )
    current = RunStatus(run.status)
    if current is RunStatus.SUCCEEDED:
        if (run.output_payload or {}) != output_payload:
            raise ThumbnailReviewGateError(
                "completed A8 run has conflicting thumbnail evidence"
            )
        return False
    if current is not RunStatus.RUNNING:
        raise ThumbnailReviewGateError(
            "approved A8 run must be running before finalization"
        )
    await transition_run(
        session,
        run,
        RunStatus.SUCCEEDED,
        output_payload=output_payload,
        note=(
            "Content Engine A8 thumbnail selected from verified local A3 PNGs; "
            "nothing published"
        ),
    )
    return False


async def _verified_candidates(
    session: AsyncSession,
    *,
    run: Run,
    storage_root: Path,
) -> tuple[ThumbnailCandidateReview, ...]:
    step = await session.scalar(
        select(StepRun)
        .where(StepRun.run_id == run.id, StepRun.name == VISUAL_STEP_NAME)
        .order_by(StepRun.id.desc())
        .limit(1)
    )
    if step is None or step.status != "succeeded":
        raise ThumbnailReviewGateError("A8 run is missing its successful A3 step")
    artifacts = tuple(
        (
            await session.scalars(
                select(Artifact)
                .where(
                    Artifact.run_id == run.id,
                    Artifact.step_run_id == step.id,
                    Artifact.artifact_type == "visual_image",
                )
                .order_by(Artifact.id)
            )
        ).all()
    )
    if not 1 <= len(artifacts) <= _MAX_CANDIDATES:
        raise ThumbnailReviewGateError("A8 requires between 1 and 60 A3 candidates")
    candidates: list[ThumbnailCandidateReview] = []
    for artifact in artifacts:
        if (
            artifact.run_id != run.id
            or artifact.step_run_id != step.id
            or artifact.artifact_type != "visual_image"
            or artifact.media_type.casefold() != "image/png"
            or artifact.origin != _A3_ARTIFACT_ORIGIN
            or Path(artifact.relative_path).suffix.casefold() != ".png"
        ):
            raise ThumbnailReviewGateError("A8 candidate is not a verified A3 PNG")
        image_path = _verify_candidate_file(storage_root, artifact)
        candidates.append(
            ThumbnailCandidateReview(
                artifact_id=_persisted_id(artifact.id, "artifact"),
                sha256=artifact.sha256,
                size_bytes=artifact.size_bytes,
                image_path=image_path,
            )
        )
    return tuple(candidates)


def _verify_candidate_file(storage_root: Path, artifact: Artifact) -> Path:
    try:
        root = storage_root.resolve(strict=True)
        candidate = (root / artifact.relative_path).resolve(strict=True)
        candidate.relative_to(root)
        if not candidate.is_file():
            raise OSError("not a regular file")
        size = candidate.stat().st_size
        if size != artifact.size_bytes:
            raise ThumbnailReviewGateError(
                "A8 candidate size does not match durable metadata"
            )
        if not 8 <= size <= _MAX_IMAGE_BYTES:
            raise ThumbnailReviewGateError("A8 candidate size is invalid")
        digest = hashlib.sha256()
        signature = b""
        with candidate.open("rb") as handle:
            signature = handle.read(8)
            digest.update(signature)
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except ThumbnailReviewGateError:
        raise
    except (OSError, ValueError) as exc:
        raise ThumbnailReviewGateError("A8 candidate is unavailable") from exc
    if signature != _PNG_SIGNATURE:
        raise ThumbnailReviewGateError("A8 candidate is not a PNG")
    if digest.hexdigest() != artifact.sha256:
        raise ThumbnailReviewGateError(
            "A8 candidate checksum does not match durable metadata"
        )
    return candidate


async def _required_final_review_approval(
    session: AsyncSession,
    *,
    run: Run,
) -> Approval:
    approvals = tuple(
        (
            await session.scalars(
                select(Approval).where(
                    Approval.run_id == run.id,
                    Approval.action == FINAL_REVIEW_APPROVAL_ACTION,
                )
            )
        ).all()
    )
    if len(approvals) != 1:
        raise ThumbnailReviewGateError("A8 requires exactly one A7 approval")
    approval = approvals[0]
    if ApprovalStatus(approval.status) is not ApprovalStatus.APPROVED:
        raise ThumbnailReviewGateError("A7 final review is not approved")
    return approval


def _approval_input(
    *,
    run: Run,
    final_review_approval: Approval,
    final_review: FinalVideoReview,
    candidates: tuple[ThumbnailCandidateReview, ...],
) -> dict[str, object]:
    return {
        "schema_version": _APPROVAL_SCHEMA_VERSION,
        "run_id": _persisted_id(run.id, "run"),
        "final_review_approval_id": _persisted_id(
            final_review_approval.id,
            "approval",
        ),
        "final_review_payload_digest": final_review_approval.payload_digest,
        "final_video_artifact_id": final_review.video_artifact_id,
        "final_video_sha256": final_review.video_sha256,
        "candidates": [_candidate_payload(candidate) for candidate in candidates],
    }


def _candidate_payload(candidate: ThumbnailCandidateReview) -> dict[str, object]:
    return {
        "artifact_id": candidate.artifact_id,
        "sha256": candidate.sha256,
        "size_bytes": candidate.size_bytes,
    }


def _candidate_set_sha256(
    candidates: tuple[ThumbnailCandidateReview, ...],
) -> str:
    return approval_payload_digest(
        {"candidates": [_candidate_payload(candidate) for candidate in candidates]}
    )


def _review_payload(
    *,
    final_review: FinalVideoReview,
    candidates: tuple[ThumbnailCandidateReview, ...],
) -> dict[str, str]:
    return {
        "candidates": f"{len(candidates)} PNGs íntegros produzidos pela A3 nesta run",
        "candidate_set_sha256": _candidate_set_sha256(candidates),
        "final_video_sha256": final_review.video_sha256,
        "selection": "Escolha exatamente uma imagem na página dedicada da A8.",
        "publication": (
            "Aprovar registra apenas a thumbnail local escolhida e published=false; "
            "nenhum upload, envio ou gasto existe."
        ),
    }


async def _output_payload(
    session: AsyncSession,
    *,
    approval: Approval,
    review: ThumbnailReview,
    decision: dict[str, object],
    storage_root: Path,
) -> dict[str, object]:
    final_approval = await session.get(Approval, review.final_review_approval_id)
    if final_approval is None:
        raise ThumbnailReviewGateError("A8 is missing its A7 approval")
    final_review = await load_final_video_review(
        session,
        approval=final_approval,
        storage_root=storage_root,
    )
    originality_step_id = await session.scalar(
        select(StepRun.id)
        .where(
            StepRun.run_id == approval.run_id,
            StepRun.name == FINAL_REVIEW_STEP_NAME,
        )
        .order_by(StepRun.id.desc())
        .limit(1)
    )
    if not isinstance(originality_step_id, int):
        raise ThumbnailReviewGateError("A8 run is missing its A6 step")
    metric_ids = list(
        (
            await session.scalars(
                select(MetricPoint.id)
                .where(MetricPoint.run_id == approval.run_id)
                .order_by(MetricPoint.id)
            )
        ).all()
    )
    ledger_ids = list(
        (
            await session.scalars(
                select(LedgerEntry.id)
                .where(LedgerEntry.run_id == approval.run_id)
                .order_by(LedgerEntry.id)
            )
        ).all()
    )
    output: dict[str, object] = {
        "final_video_artifact_id": final_review.video_artifact_id,
        "final_video_sha256": final_review.video_sha256,
        "originality_report_artifact_id": final_review.report_artifact_id,
        "originality_report_sha256": final_review.report_sha256,
        "originality_step_run_id": originality_step_id,
        "originality_max_similarity": final_review.max_similarity,
        "originality_threshold": final_review.threshold,
        "originality_reference_count": final_review.reference_count,
        "final_review_approval_id": review.final_review_approval_id,
        "thumbnail_artifact_id": decision["thumbnail_artifact_id"],
        "thumbnail_sha256": decision["thumbnail_sha256"],
        "thumbnail_review_approval_id": review.approval_id,
        "metric_point_ids": metric_ids,
        "ledger_entry_ids": ledger_ids,
        "published": False,
    }
    if final_review.alignment_report_artifact_id is not None:
        output.update(
            {
                "alignment_gate_step_run_id": (
                    final_review.alignment_gate_step_run_id
                ),
                "alignment_report_artifact_id": (
                    final_review.alignment_report_artifact_id
                ),
                "alignment_report_sha256": final_review.alignment_report_sha256,
                "alignment_parameters_digest": (
                    final_review.alignment_parameters_digest
                ),
            }
        )
    return output


def _persisted_id(value: int | None, kind: str) -> int:
    if not isinstance(value, int):
        raise ThumbnailReviewGateError(f"{kind} must be persisted")
    return value
