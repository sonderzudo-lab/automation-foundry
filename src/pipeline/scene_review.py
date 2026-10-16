"""Content Engine A5a human review gate for the AI-animated scene clips.

Same design as the A3 draft rounds (``visual_drafts`` and ``visual_review``), with
clips in place of images. With the gate on, every A5a round is a GPU step of its
own (``draft-scenes-a5a-rN``) with its own manifest and clips. The approval binds
a decision to that manifest and to the SHA-256 of every clip, and the page plays
each clip next to the narrated stretch, the source image and the motion sentence.

Besides approving or rejecting, the reviewer may send clips back with a note, or
mark a scene to keep its still image. The decision is then an approval whose
payload carries ``choice=redo``, one ``note_<index>`` per clip to redo and one
``still_<index>`` per scene kept still. The next round rewrites only those motion
sentences, generates only those clips and copies the others after checking their
SHA-256. Only the approved round goes to A5: the official A5a step imports its
clips and manifest unchanged. The gate never publishes, uploads or modifies the
reviewed files.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.pipeline.scene_animation import (
    SCENE_CLIP_ARTIFACT,
    SCENE_CLIPS_DRAFT_STEP_PREFIX,
    SCENE_CLIPS_MANIFEST_ARTIFACT,
    CarriedClip,
    SceneRoundPlan,
)
from src.pipeline.scene_clips import AnimatedScene, SceneClipsManifest, StillReason, StillScene
from src.pipeline.scene_motion import NEUTRAL_MOTION
from src.pipeline.visuals import VISUAL_STEP_NAME
from src.platform.approval_service import approval_payload_digest, request_approval
from src.platform.models import (
    Approval,
    ApprovalStatus,
    Artifact,
    Run,
    RunStatus,
    StepRun,
)

SCENE_CLIPS_REVIEW_APPROVAL_ACTION = "review_content_scene_clips_a5a"
REDO_CHOICE = "redo"
# The decision payload holds at most eight fields: the choice and seven others.
MAX_CHANGES_PER_ROUND = 7
MAX_REDO_NOTE_CHARS = 250
_ROUND_KEY_RE = re.compile(r":scene-clips-review-r([1-9][0-9]{0,3})$")
_MAX_MANIFEST_BYTES = 1024 * 1024
_MAX_CLIP_BYTES = 200 * 1024 * 1024
_MAX_IMAGE_BYTES = 50 * 1024 * 1024
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_DEFAULT_REDO_NOTE = "Refazer este clipe com outro movimento."


class SceneClipReviewGateError(ValueError):
    """Raised when A5a review evidence cannot be trusted."""


@dataclass(frozen=True, slots=True)
class SceneClipReviewRequestResult:
    """Observable result of opening or replaying the A5a review gate."""

    run_id: int
    approval_id: int
    run_status: str
    created: bool


@dataclass(frozen=True, slots=True)
class SceneClipReviewScene:
    """One scene of the reviewed round: a checked clip, or the still image it keeps.

    ``clip_path`` and ``image_path`` are server-side only and must never be
    rendered in a template.
    """

    index: int
    start_ms: int
    end_ms: int
    narration_text: str
    # The A3 image the clip starts from, shown beside the clip.
    image_artifact_id: int
    image_sha256: str
    image_path: Path
    # None when the scene keeps its still image.
    clip_artifact_id: int | None
    clip_sha256: str | None
    clip_size_bytes: int | None
    clip_path: Path | None
    motion: str
    seed: int | None
    frames: int | None
    still_reason: StillReason | None
    # The reviewer's note that produced this clip in a later round, if any.
    revision_note: str = ""

    @property
    def is_clip(self) -> bool:
        return self.clip_artifact_id is not None

    @property
    def can_redo(self) -> bool:
        """A scene too long for a clip can never be animated."""
        return self.still_reason != "too_long"

    @property
    def can_keep_still(self) -> bool:
        return self.is_clip


@dataclass(frozen=True, slots=True)
class SceneClipReview:
    """Complete A5a projection bound to one immutable round of clips."""

    approval_id: int
    run_id: int
    round_number: int
    manifest_artifact_id: int
    manifest_sha256: str
    width: int
    height: int
    # "quality-test-only" when the source images carry no commercial-use claim.
    usage_scope: str | None
    provider: str
    model_id: str
    license_id: str
    scenes: tuple[SceneClipReviewScene, ...]
    # Scenes this decision sent back (with the notes) and the ones kept still.
    requested_redo: tuple[tuple[int, str], ...] = ()
    requested_still: tuple[int, ...] = ()

    @property
    def can_redo(self) -> bool:
        return True

    @property
    def clip_count(self) -> int:
        return sum(1 for scene in self.scenes if scene.is_clip)


@dataclass(frozen=True, slots=True)
class _Verified:
    manifest_artifact: Artifact
    manifest: SceneClipsManifest
    scenes: tuple[SceneClipReviewScene, ...]
    round_number: int


@dataclass(frozen=True, slots=True)
class ApprovedSceneClips:
    """The verified round a decision is bound to, ready to carry into a next step."""

    round_number: int
    manifest: SceneClipsManifest
    manifest_sha256: str
    scenes: tuple[SceneClipReviewScene, ...]


async def request_scene_clip_approval(
    session: AsyncSession,
    *,
    run: Run,
    idempotency_key: str,
    storage_root: Path,
    round_number: int,
) -> SceneClipReviewRequestResult:
    """Open A5a review after verifying the manifest and every clip it lists."""
    if RunStatus(run.status) is not RunStatus.RUNNING:
        raise SceneClipReviewGateError("A5a review requires a running run")
    evidence = await _verify_evidence(
        session, run=run, storage_root=storage_root, round_number=round_number
    )
    creation = await request_approval(
        session,
        run=run,
        idempotency_key=f"{idempotency_key}:scene-clips-review-r{round_number}",
        action=SCENE_CLIPS_REVIEW_APPROVAL_ACTION,
        summary="Conferir os clipes animados ao lado da narração antes de montar o vídeo",
        input_payload=_approval_input(run=run, evidence=evidence),
        review_payload=_review_payload(evidence),
    )
    return SceneClipReviewRequestResult(
        run_id=_persisted_id(run.id, "run"),
        approval_id=_persisted_id(creation.approval.id, "approval"),
        run_status=run.status,
        created=creation.created,
    )


async def load_scene_clip_review(
    session: AsyncSession,
    *,
    approval: Approval,
    storage_root: Path,
) -> SceneClipReview:
    """Reconstruct and verify the exact set of clips one decision is bound to."""
    run, evidence = await _load_for_approval(session, approval, storage_root)
    notes, stills = requested_changes(approval)
    scenes = evidence.scenes
    if evidence.round_number > 1:
        # The notes of the previous decision explain why these clips were made again.
        previous = await session.scalar(
            select(Approval).where(
                Approval.run_id == run.id,
                Approval.action == SCENE_CLIPS_REVIEW_APPROVAL_ACTION,
                Approval.idempotency_key.like(
                    f"%:scene-clips-review-r{evidence.round_number - 1}"
                ),
            )
        )
        earlier = requested_changes(previous)[0] if previous is not None else {}
        scenes = tuple(
            replace(scene, revision_note=earlier[scene.index])
            if scene.index in earlier and scene.is_clip
            else scene
            for scene in scenes
        )
    return SceneClipReview(
        approval_id=_persisted_id(approval.id, "approval"),
        run_id=_persisted_id(run.id, "run"),
        round_number=evidence.round_number,
        manifest_artifact_id=_persisted_id(evidence.manifest_artifact.id, "artifact"),
        manifest_sha256=evidence.manifest_artifact.sha256,
        width=evidence.manifest.width,
        height=evidence.manifest.height,
        usage_scope=evidence.manifest.usage_scope,
        provider=evidence.manifest.provider,
        model_id=evidence.manifest.model_id,
        license_id=evidence.manifest.license_id,
        scenes=scenes,
        requested_redo=tuple(sorted(notes.items())),
        requested_still=tuple(sorted(stills)),
    )


async def load_reviewed_scene_clips(
    session: AsyncSession,
    *,
    approval: Approval,
    storage_root: Path,
) -> ApprovedSceneClips:
    """The verified round of an approval, with the data a next step carries over."""
    _run, evidence = await _load_for_approval(session, approval, storage_root)
    return ApprovedSceneClips(
        round_number=evidence.round_number,
        manifest=evidence.manifest,
        manifest_sha256=evidence.manifest_artifact.sha256,
        scenes=evidence.scenes,
    )


def approval_round(approval: Approval) -> int | None:
    """The round an A5a approval reviews, read from its idempotency key."""
    match = _ROUND_KEY_RE.search(approval.idempotency_key or "")
    return int(match.group(1)) if match else None


def requested_changes(approval: Approval) -> tuple[dict[int, str], set[int]]:
    """Clips an approved decision sent back with notes, and scenes kept still."""
    payload = approval.decision_payload
    if (
        ApprovalStatus(approval.status) is not ApprovalStatus.APPROVED
        or not isinstance(payload, dict)
        or payload.get("choice") != REDO_CHOICE
    ):
        return {}, set()
    notes: dict[int, str] = {}
    stills: set[int] = set()
    for key, value in payload.items():
        if key.startswith("note_") and key[5:].isdigit() and isinstance(value, str):
            notes[int(key[5:])] = value
        elif key.startswith("still_") and key[6:].isdigit() and value is True:
            stills.add(int(key[6:]))
    return notes, stills


def redo_decision_payload(
    review: SceneClipReview,
    notes: Mapping[int, str],
    keep_still: frozenset[int] | set[int] = frozenset(),
) -> dict[str, object]:
    """Validate a request to redo clips and/or keep scenes as still images."""
    if not notes and not keep_still:
        raise SceneClipReviewGateError("choose at least one clip to redo or keep still")
    if len(notes) + len(keep_still) > MAX_CHANGES_PER_ROUND:
        raise SceneClipReviewGateError(
            f"at most {MAX_CHANGES_PER_ROUND} scenes can be changed per round"
        )
    if set(notes) & set(keep_still):
        raise SceneClipReviewGateError("a scene is either redone or kept still, not both")
    by_index = {scene.index: scene for scene in review.scenes}
    if any(index not in by_index or not by_index[index].can_redo for index in notes):
        raise SceneClipReviewGateError("redo request names an unknown or non-animatable scene")
    if any(index not in by_index or not by_index[index].can_keep_still for index in keep_still):
        raise SceneClipReviewGateError("only an animated scene can be kept as a still image")
    remaining = {
        scene.index for scene in review.scenes if scene.is_clip and scene.index not in keep_still
    } | set(notes)
    if not remaining:
        raise SceneClipReviewGateError(
            "at least one scene must stay animated; reject the clips instead"
        )
    payload: dict[str, object] = {"choice": REDO_CHOICE}
    for index, note in sorted(notes.items()):
        text = " ".join(note.split())
        if len(text) > MAX_REDO_NOTE_CHARS:
            raise SceneClipReviewGateError("redo note is too long")
        payload[f"note_{index}"] = text or _DEFAULT_REDO_NOTE
    for index in sorted(keep_still):
        payload[f"still_{index}"] = True
    return payload


def next_round_plan(
    reviewed: ApprovedSceneClips,
    *,
    notes: Mapping[int, str],
    keep_still: frozenset[int] | set[int],
) -> SceneRoundPlan:
    """Round N+1: rewrite and regenerate the noted scenes, carry everything else."""
    unknown = (set(notes) | set(keep_still)) - {scene.index for scene in reviewed.scenes}
    if unknown or set(notes) & set(keep_still):
        raise SceneClipReviewGateError("the requested changes do not match the reviewed round")
    plan = _carry_plan(
        reviewed, round_number=reviewed.round_number + 1, notes=notes, keep_still=keep_still
    )
    return plan


def import_plan(reviewed: ApprovedSceneClips) -> SceneRoundPlan:
    """The official A5a step: publish the approved round's clips and manifest unchanged."""
    return _carry_plan(reviewed, round_number=None, notes={}, keep_still=frozenset())


def _carry_plan(
    reviewed: ApprovedSceneClips,
    *,
    round_number: int | None,
    notes: Mapping[int, str],
    keep_still: frozenset[int] | set[int],
) -> SceneRoundPlan:
    manifest = reviewed.manifest
    motions: list[str] = []
    carried: list[CarriedClip] = []
    stills: dict[int, StillReason] = {}
    for entry, scene in zip(manifest.scenes, reviewed.scenes, strict=True):
        if isinstance(entry, StillScene):
            motions.append(NEUTRAL_MOTION)
            if entry.index not in notes:
                stills[entry.index] = entry.reason
            continue
        motions.append(entry.motion)
        if entry.index in keep_still:
            stills[entry.index] = "owner_choice"
        elif entry.index not in notes:
            if scene.clip_path is None or not isinstance(entry, AnimatedScene):
                raise SceneClipReviewGateError("reviewed clip is missing")
            carried.append(
                CarriedClip(
                    index=entry.index,
                    image_sha256=entry.image_sha256,
                    path=scene.clip_path,
                    sha256=entry.video_sha256,
                    motion=entry.motion,
                    seed=entry.seed,
                    frames=entry.frames,
                )
            )
    return SceneRoundPlan(
        round_number=round_number,
        motions=tuple(motions),
        carried=tuple(carried),
        stills=stills,
        redo=dict(notes),
        identity={
            "provider": manifest.provider,
            "model_id": manifest.model_id,
            "graph_version": manifest.graph_version,
            "license_id": manifest.license_id,
            "inventory_sha256": manifest.model_inventory_sha256,
        },
        source_round=reviewed.round_number,
        source_manifest_sha256=reviewed.manifest_sha256,
    )


async def finalize_scene_clip_approval(
    session: AsyncSession,
    *,
    approval: Approval,
    storage_root: Path,
) -> bool:
    """Verify an approved A5a decision and request durable continuation."""
    if approval.action != SCENE_CLIPS_REVIEW_APPROVAL_ACTION:
        return False
    if ApprovalStatus(approval.status) is not ApprovalStatus.APPROVED:
        raise SceneClipReviewGateError("A5a scene clip review is not approved")
    await load_scene_clip_review(session, approval=approval, storage_root=storage_root)
    run = await session.get(Run, approval.run_id)
    if run is None:
        raise SceneClipReviewGateError("approval run does not exist")
    current = RunStatus(run.status)
    if current is RunStatus.RUNNING:
        return True
    if current is RunStatus.SUCCEEDED:
        return False
    raise SceneClipReviewGateError(
        "approved A5a scene clip run must be running before continuation"
    )


async def _load_for_approval(
    session: AsyncSession,
    approval: Approval,
    storage_root: Path,
) -> tuple[Run, _Verified]:
    if approval.action != SCENE_CLIPS_REVIEW_APPROVAL_ACTION:
        raise SceneClipReviewGateError("approval is not an A5a scene clip review")
    round_number = approval_round(approval)
    if round_number is None:
        raise SceneClipReviewGateError("approval does not name a clip round")
    run = await session.get(Run, approval.run_id)
    if run is None:
        raise SceneClipReviewGateError("approval run does not exist")
    evidence = await _verify_evidence(
        session, run=run, storage_root=storage_root, round_number=round_number
    )
    if approval.payload_digest != approval_payload_digest(
        _approval_input(run=run, evidence=evidence)
    ):
        raise SceneClipReviewGateError("approval does not match the reviewed clips")
    return run, evidence


async def _verify_evidence(
    session: AsyncSession,
    *,
    run: Run,
    storage_root: Path,
    round_number: int,
) -> _Verified:
    step = await session.scalar(
        select(StepRun)
        .where(
            StepRun.run_id == run.id,
            StepRun.name == f"{SCENE_CLIPS_DRAFT_STEP_PREFIX}{round_number}",
            StepRun.status == RunStatus.SUCCEEDED.value,
        )
        .order_by(StepRun.id.desc())
        .limit(1)
    )
    if step is None:
        raise SceneClipReviewGateError("A5a review requires a completed clip round")
    manifests = list(
        (
            await session.scalars(
                select(Artifact).where(
                    Artifact.run_id == run.id,
                    Artifact.step_run_id == step.id,
                    Artifact.artifact_type == SCENE_CLIPS_MANIFEST_ARTIFACT,
                )
            )
        ).all()
    )
    if len(manifests) != 1:
        raise SceneClipReviewGateError("A5a review evidence is missing or ambiguous")
    manifest_artifact = manifests[0]
    _, encoded = _verified_file(
        storage_root, manifest_artifact, max_bytes=_MAX_MANIFEST_BYTES, read_content=True
    )
    try:
        manifest = SceneClipsManifest.model_validate_json(encoded)
    except (ValidationError, ValueError) as exc:
        raise SceneClipReviewGateError("A5a manifest is not readable") from exc

    narrations = await _narrations_and_images(session, run=run, storage_root=storage_root)
    clips_directory = Path(manifest_artifact.relative_path).parent / "clips"
    scenes: list[SceneClipReviewScene] = []
    for entry in manifest.scenes:
        image = narrations.get((entry.image, entry.image_sha256))
        if image is None:
            raise SceneClipReviewGateError("A5a manifest names an image A3 never published")
        image_artifact, image_path, narration = image
        if isinstance(entry, StillScene):
            scenes.append(
                SceneClipReviewScene(
                    index=entry.index,
                    start_ms=entry.start_ms,
                    end_ms=entry.end_ms,
                    narration_text=narration,
                    image_artifact_id=_persisted_id(image_artifact.id, "artifact"),
                    image_sha256=entry.image_sha256,
                    image_path=image_path,
                    clip_artifact_id=None,
                    clip_sha256=None,
                    clip_size_bytes=None,
                    clip_path=None,
                    motion="",
                    seed=None,
                    frames=None,
                    still_reason=entry.reason,
                )
            )
            continue
        clip_artifacts = list(
            (
                await session.scalars(
                    select(Artifact).where(
                        Artifact.run_id == run.id,
                        Artifact.artifact_type == SCENE_CLIP_ARTIFACT,
                        Artifact.sha256 == entry.video_sha256,
                        Artifact.relative_path == (clips_directory / entry.video).as_posix(),
                    )
                )
            ).all()
        )
        if len(clip_artifacts) != 1 or clip_artifacts[0].media_type != "video/mp4":
            raise SceneClipReviewGateError("A5a manifest names a clip that was not published")
        clip = clip_artifacts[0]
        clip_path, _ = _verified_file(
            storage_root, clip, max_bytes=_MAX_CLIP_BYTES, read_content=False
        )
        with clip_path.open("rb") as stream:
            header = stream.read(12)
        if len(header) != 12 or header[4:8] != b"ftyp":
            raise SceneClipReviewGateError("A5a reviewed clip is not an MP4")
        scenes.append(
            SceneClipReviewScene(
                index=entry.index,
                start_ms=entry.start_ms,
                end_ms=entry.end_ms,
                narration_text=narration,
                image_artifact_id=_persisted_id(image_artifact.id, "artifact"),
                image_sha256=entry.image_sha256,
                image_path=image_path,
                clip_artifact_id=_persisted_id(clip.id, "artifact"),
                clip_sha256=clip.sha256,
                clip_size_bytes=clip.size_bytes,
                clip_path=clip_path,
                motion=entry.motion,
                seed=entry.seed,
                frames=entry.frames,
                still_reason=None,
            )
        )
    return _Verified(
        manifest_artifact=manifest_artifact,
        manifest=manifest,
        scenes=tuple(scenes),
        round_number=round_number,
    )


async def _narrations_and_images(
    session: AsyncSession,
    *,
    run: Run,
    storage_root: Path,
) -> dict[tuple[str, str], tuple[Artifact, Path, str]]:
    """The A3 images of the run by (file name, SHA-256), with their narrated text."""
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
        raise SceneClipReviewGateError("A5a review requires the A3 visual bundle")
    artifacts = list(
        (
            await session.scalars(
                select(Artifact).where(Artifact.run_id == run.id, Artifact.step_run_id == step.id)
            )
        ).all()
    )
    manifests = [item for item in artifacts if item.artifact_type == "visual_manifest"]
    if len(manifests) != 1:
        raise SceneClipReviewGateError("A3 visual manifest is missing or ambiguous")
    _, encoded = _verified_file(
        storage_root, manifests[0], max_bytes=_MAX_MANIFEST_BYTES, read_content=True
    )
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise SceneClipReviewGateError("A3 manifest is not readable") from exc
    assets = payload.get("assets") if isinstance(payload, dict) else None
    if not isinstance(assets, list):
        raise SceneClipReviewGateError("A3 manifest does not list its images")
    texts: dict[str, str] = {}
    for asset in assets:
        if isinstance(asset, dict) and isinstance(asset.get("file_name"), str):
            narration = asset.get("narration_text")
            texts[asset["file_name"]] = narration.strip() if isinstance(narration, str) else ""
    found: dict[tuple[str, str], tuple[Artifact, Path, str]] = {}
    for artifact in artifacts:
        if artifact.artifact_type != "visual_image" or artifact.media_type != "image/png":
            continue
        name = Path(artifact.relative_path).name
        path, _ = _verified_file(
            storage_root, artifact, max_bytes=_MAX_IMAGE_BYTES, read_content=False
        )
        with path.open("rb") as stream:
            if stream.read(len(_PNG_SIGNATURE)) != _PNG_SIGNATURE:
                raise SceneClipReviewGateError("A3 source image is not a PNG")
        found[(name, artifact.sha256)] = (artifact, path, texts.get(name, ""))
    return found


def _verified_file(
    storage_root: Path,
    artifact: Artifact,
    *,
    max_bytes: int,
    read_content: bool,
) -> tuple[Path, bytes]:
    if artifact.size_bytes < 1 or artifact.size_bytes > max_bytes:
        raise SceneClipReviewGateError("review artifact size is invalid")
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
        raise SceneClipReviewGateError("review artifact is unavailable") from exc
    if actual_size != artifact.size_bytes:
        raise SceneClipReviewGateError("review artifact size does not match")
    if digest.hexdigest() != artifact.sha256:
        raise SceneClipReviewGateError("review artifact checksum does not match")
    return candidate, bytes(content or b"")


def _approval_input(*, run: Run, evidence: _Verified) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "run_id": _persisted_id(run.id, "run"),
        "round": evidence.round_number,
        "manifest_artifact_id": _persisted_id(evidence.manifest_artifact.id, "artifact"),
        "manifest_sha256": evidence.manifest_artifact.sha256,
        "clips": [
            [scene.clip_artifact_id, scene.clip_sha256]
            for scene in evidence.scenes
            if scene.is_clip
        ],
    }
    if evidence.manifest.usage_scope is not None:
        payload["usage_scope"] = evidence.manifest.usage_scope
    return payload


def _review_payload(evidence: _Verified) -> dict[str, str]:
    animated = sum(1 for scene in evidence.scenes if scene.is_clip)
    payload = {
        "clips": (
            f"{animated} clipes e {len(evidence.scenes) - animated} cenas com imagem · "
            f"manifesto #{evidence.manifest_artifact.id} · rodada {evidence.round_number}"
        ),
        "provenance": f"{evidence.manifest.model_id} · {evidence.manifest.license_id}"[:300],
        "manifest_sha256": evidence.manifest_artifact.sha256,
        "checklist": (
            "Conferir em cada clipe: pessoa nova, rosto deformado, texto legível, "
            "movimento estranho e se combina com o trecho narrado."
        ),
        "publication": "Nenhum upload, envio ou gasto é executado por esta aprovação.",
    }
    if evidence.manifest.usage_scope is not None:
        payload["usage"] = (
            "Teste de qualidade, não monetizável: os clipes herdam o escopo das imagens."
        )
    return payload


def _persisted_id(value: int | None, kind: str) -> int:
    if not isinstance(value, int):
        raise SceneClipReviewGateError(f"{kind} must be persisted")
    return value
