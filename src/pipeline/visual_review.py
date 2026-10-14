"""Content Engine A3 human review gate before captions and assembly.

The gate binds a human decision to the exact A3 manifest and to every image it
lists, and shows each image next to the stretch of narration it covers. It is
where a person checks what no code checks: a real person's likeness, brands,
readable text, visual defects and whether the image fits what is said. It never
publishes, uploads, copies, regenerates or modifies the reviewed images.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.pipeline.visuals import QUALITY_TEST_USAGE_SCOPE, VISUAL_STEP_NAME
from src.platform.approval_service import approval_payload_digest, request_approval
from src.platform.models import (
    Approval,
    ApprovalStatus,
    Artifact,
    Run,
    RunStatus,
    StepRun,
)

VISUAL_REVIEW_APPROVAL_ACTION = "review_content_visuals_a3"
_MAX_MANIFEST_BYTES = 1024 * 1024
_MAX_IMAGE_BYTES = 50 * 1024 * 1024
_MAX_IMAGES = 60
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class VisualReviewGateError(ValueError):
    """Raised when A3 review evidence cannot be trusted."""


@dataclass(frozen=True, slots=True)
class VisualReviewRequestResult:
    """Observable result of opening or replaying the A3 review gate."""

    run_id: int
    approval_id: int
    run_status: str
    created: bool


@dataclass(frozen=True, slots=True)
class VisualReviewImage:
    """One checksum-verified A3 image and the narration it covers.

    ``image_path`` is server-side only and must never be rendered in a template.
    """

    index: int
    artifact_id: int
    sha256: str
    size_bytes: int
    narration_text: str
    generation_prompt: str
    source_type: str
    image_path: Path


@dataclass(frozen=True, slots=True)
class VisualReview:
    """Complete A3 projection bound to one immutable set of images."""

    approval_id: int
    run_id: int
    manifest_artifact_id: int
    manifest_sha256: str
    width: int
    height: int
    # "quality-test-only" when the images carry no commercial-use claim.
    usage_scope: str | None
    licenses: tuple[str, ...]
    images: tuple[VisualReviewImage, ...]


@dataclass(frozen=True, slots=True)
class _VerifiedVisuals:
    manifest: Artifact
    width: int
    height: int
    usage_scope: str | None
    licenses: tuple[str, ...]
    images: tuple[VisualReviewImage, ...]


async def request_visual_approval(
    session: AsyncSession,
    *,
    run: Run,
    idempotency_key: str,
    storage_root: Path,
) -> VisualReviewRequestResult:
    """Open A3 review after verifying the manifest and every image it lists."""
    if RunStatus(run.status) is not RunStatus.RUNNING:
        raise VisualReviewGateError("A3 review requires a running run")
    evidence = await _verify_evidence(session, run=run, storage_root=storage_root)
    creation = await request_approval(
        session,
        run=run,
        idempotency_key=f"{idempotency_key}:visual-review",
        action=VISUAL_REVIEW_APPROVAL_ACTION,
        summary="Conferir as imagens A3 ao lado da narração antes de legendar e montar",
        input_payload=_approval_input(run=run, evidence=evidence),
        review_payload=_review_payload(evidence),
    )
    return VisualReviewRequestResult(
        run_id=_persisted_id(run.id, "run"),
        approval_id=_persisted_id(creation.approval.id, "approval"),
        run_status=run.status,
        created=creation.created,
    )


async def load_visual_review(
    session: AsyncSession,
    *,
    approval: Approval,
    storage_root: Path,
) -> VisualReview:
    """Reconstruct and verify the exact set of images one decision is bound to."""
    if approval.action != VISUAL_REVIEW_APPROVAL_ACTION:
        raise VisualReviewGateError("approval is not an A3 visual review")
    run = await session.get(Run, approval.run_id)
    if run is None:
        raise VisualReviewGateError("approval run does not exist")
    evidence = await _verify_evidence(session, run=run, storage_root=storage_root)
    if approval.payload_digest != approval_payload_digest(
        _approval_input(run=run, evidence=evidence)
    ):
        raise VisualReviewGateError("approval does not match the reviewed images")
    return VisualReview(
        approval_id=_persisted_id(approval.id, "approval"),
        run_id=_persisted_id(run.id, "run"),
        manifest_artifact_id=_persisted_id(evidence.manifest.id, "artifact"),
        manifest_sha256=evidence.manifest.sha256,
        width=evidence.width,
        height=evidence.height,
        usage_scope=evidence.usage_scope,
        licenses=evidence.licenses,
        images=evidence.images,
    )


async def finalize_visual_approval(
    session: AsyncSession,
    *,
    approval: Approval,
    storage_root: Path,
) -> bool:
    """Verify an approved A3 decision and request durable continuation."""
    if approval.action != VISUAL_REVIEW_APPROVAL_ACTION:
        return False
    if ApprovalStatus(approval.status) is not ApprovalStatus.APPROVED:
        raise VisualReviewGateError("A3 visual review is not approved")
    await load_visual_review(session, approval=approval, storage_root=storage_root)
    run = await session.get(Run, approval.run_id)
    if run is None:
        raise VisualReviewGateError("approval run does not exist")
    current = RunStatus(run.status)
    if current is RunStatus.RUNNING:
        return True
    if current is RunStatus.SUCCEEDED:
        return False
    raise VisualReviewGateError(
        "approved A3 visual run must be running before continuation"
    )


async def _verify_evidence(
    session: AsyncSession,
    *,
    run: Run,
    storage_root: Path,
) -> _VerifiedVisuals:
    step = await session.scalar(
        select(StepRun)
        .where(
            StepRun.run_id == run.id,
            StepRun.name == VISUAL_STEP_NAME,
            StepRun.status == RunStatus.SUCCEEDED.value,
        )
        .order_by(StepRun.id.desc())
        .limit(1)
    )
    if step is None:
        raise VisualReviewGateError("A3 review requires a completed visual step")
    artifacts = list(
        (
            await session.scalars(
                select(Artifact)
                .where(Artifact.run_id == run.id, Artifact.step_run_id == step.id)
                .order_by(Artifact.id)
            )
        ).all()
    )
    manifests = [item for item in artifacts if item.artifact_type == "visual_manifest"]
    visuals = [item for item in artifacts if item.artifact_type == "visual_image"]
    if len(manifests) != 1 or not 1 <= len(visuals) <= _MAX_IMAGES:
        raise VisualReviewGateError("A3 review evidence is missing or ambiguous")

    _, encoded = _verify_artifact_file(
        storage_root, manifests[0], max_bytes=_MAX_MANIFEST_BYTES, read_content=True
    )
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise VisualReviewGateError("A3 manifest is not readable") from exc
    assets = payload.get("assets") if isinstance(payload, dict) else None
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or not isinstance(assets, list)
        or len(assets) != len(visuals)
    ):
        raise VisualReviewGateError("A3 manifest does not describe the reviewed images")
    usage_scope = payload.get("usage_scope")
    if usage_scope not in {None, QUALITY_TEST_USAGE_SCOPE}:
        raise VisualReviewGateError("A3 manifest usage scope is not supported")

    images: list[VisualReviewImage] = []
    licenses: set[str] = set()
    dimensions: set[tuple[object, object]] = set()
    for position, (asset, artifact) in enumerate(zip(assets, visuals, strict=True), 1):
        if (
            not isinstance(asset, dict)
            or artifact.media_type != "image/png"
            or asset.get("file_name") != Path(artifact.relative_path).name
            or asset.get("sha256") != artifact.sha256
            or asset.get("size_bytes") != artifact.size_bytes
            or asset.get("index") != position
            or not isinstance(asset.get("commercial_use"), bool)
            or (asset.get("commercial_use") is False) != (usage_scope is not None)
        ):
            raise VisualReviewGateError("A3 manifest conflicts with a reviewed image")
        path, _ = _verify_artifact_file(
            storage_root, artifact, max_bytes=_MAX_IMAGE_BYTES, read_content=False
        )
        with path.open("rb") as stream:
            if stream.read(len(_PNG_SIGNATURE)) != _PNG_SIGNATURE:
                raise VisualReviewGateError("A3 reviewed image is not a PNG")
        licenses.add(_asset_text(asset, "license_id", max_length=200))
        dimensions.add((asset.get("width"), asset.get("height")))
        images.append(
            VisualReviewImage(
                index=position,
                artifact_id=_persisted_id(artifact.id, "artifact"),
                sha256=artifact.sha256,
                size_bytes=artifact.size_bytes,
                narration_text=_optional_text(asset, "narration_text", max_length=2000),
                generation_prompt=_optional_text(asset, "generation_prompt", max_length=2000),
                source_type=_asset_text(asset, "source_type", max_length=20),
                image_path=path,
            )
        )
    if len(dimensions) != 1:
        raise VisualReviewGateError("A3 reviewed images have conflicting dimensions")
    width, height = next(iter(dimensions))
    if not isinstance(width, int) or not isinstance(height, int):
        raise VisualReviewGateError("A3 reviewed image dimensions are invalid")
    return _VerifiedVisuals(
        manifest=manifests[0],
        width=width,
        height=height,
        usage_scope=usage_scope if isinstance(usage_scope, str) else None,
        licenses=tuple(sorted(licenses)),
        images=tuple(images),
    )


def _verify_artifact_file(
    storage_root: Path,
    artifact: Artifact,
    *,
    max_bytes: int,
    read_content: bool,
) -> tuple[Path, bytes]:
    if artifact.size_bytes < 1 or artifact.size_bytes > max_bytes:
        raise VisualReviewGateError("review artifact size is invalid")
    try:
        root = storage_root.resolve(strict=True)
        candidate = (root / artifact.relative_path).resolve(strict=True)
        candidate.relative_to(root)
        if not candidate.is_file():
            raise OSError("not a regular file")
        digest = hashlib.sha256()
        content = bytearray() if read_content else None
        actual_size = 0
        with candidate.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                actual_size += len(chunk)
                if actual_size > max_bytes:
                    raise OSError("artifact exceeds review limit")
                digest.update(chunk)
                if content is not None:
                    content.extend(chunk)
    except (OSError, ValueError) as exc:
        raise VisualReviewGateError("review artifact is unavailable") from exc
    if actual_size != artifact.size_bytes:
        raise VisualReviewGateError("review artifact size does not match")
    if digest.hexdigest() != artifact.sha256:
        raise VisualReviewGateError("review artifact checksum does not match")
    return candidate, bytes(content or b"")


def _approval_input(*, run: Run, evidence: _VerifiedVisuals) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "run_id": _persisted_id(run.id, "run"),
        "manifest_artifact_id": _persisted_id(evidence.manifest.id, "artifact"),
        "manifest_sha256": evidence.manifest.sha256,
        "images": [[image.artifact_id, image.sha256] for image in evidence.images],
    }
    if evidence.usage_scope is not None:
        payload["usage_scope"] = evidence.usage_scope
    return payload


def _review_payload(evidence: _VerifiedVisuals) -> dict[str, str]:
    provenance = "; ".join(evidence.licenses)
    payload = {
        "visuals": (
            f"{len(evidence.images)} imagens de {evidence.width}x{evidence.height} · "
            f"manifesto #{evidence.manifest.id}"
        ),
        "provenance": provenance if len(provenance) <= 300 else f"{provenance[:297]}...",
        "manifest_sha256": evidence.manifest.sha256,
        "checklist": (
            "Conferir em cada imagem: pessoa real, marca, texto legível, defeitos "
            "visíveis e se ela combina com o trecho narrado."
        ),
        "publication": "Nenhum upload, envio ou gasto é executado por esta aprovação.",
    }
    if evidence.usage_scope is not None:
        payload["usage"] = (
            "Teste de qualidade, não monetizável: as imagens não têm uso comercial "
            "declarado."
        )
    return payload


def _asset_text(asset: dict[str, object], key: str, *, max_length: int) -> str:
    value = asset.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise VisualReviewGateError("A3 manifest asset metadata is invalid")
    return value.strip()


def _optional_text(asset: dict[str, object], key: str, *, max_length: int) -> str:
    value = asset.get(key)
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > max_length:
        raise VisualReviewGateError("A3 manifest asset metadata is invalid")
    return value.strip()


def _persisted_id(value: int | None, kind: str) -> int:
    if not isinstance(value, int):
        raise VisualReviewGateError(f"{kind} must be persisted")
    return value
