"""Observable Content Engine A1 executor with no outbound side effects."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from openai import APIConnectionError, APITimeoutError
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.intelligence.similarity import execute_similarity_gate_step
from src.operations.retention_policy import resolve_retention_days
from src.pipeline.assembly import (
    AssemblyAdapter,
    FFmpegQualityTestConfig,
    execute_approved_assembly_step,
    get_configured_assembly_adapter,
)
from src.pipeline.caption_alignment import build_alignment_parameters
from src.pipeline.caption_alignment_gate import execute_caption_alignment_gate_step
from src.pipeline.captions import (
    FASTER_WHISPER_QUALITY_TEST_BACKEND,
    CaptionAdapter,
    FasterWhisperQualityTestConfig,
    execute_approved_caption_step,
    get_configured_caption_adapter,
)
from src.pipeline.final_review import (
    FINAL_REVIEW_APPROVAL_ACTION,
    FinalVideoReview,
    finalize_final_video_approval,
    load_final_video_review,
    request_final_video_approval,
)
from src.pipeline.local_export import LocalExportPackage, load_local_export_package
from src.pipeline.narration_review import (
    NARRATION_REVIEW_APPROVAL_ACTION,
    NarrationReview,
    NarrationVisualsNotReadyError,
    describe_visual_plan_problem,
    finalize_narration_approval,
    load_narration_review,
    request_narration_approval,
)
from src.pipeline.script_gen import (
    OPENING_STYLE_LABELS,
    OPENING_STYLES,
    STRUCTURE_LABELS,
    EmptyResponseError,
    OpeningStyle,
    ScriptLengthError,
    ScriptResult,
    SourcedFact,
    Structure,
    generate_script,
)
from src.pipeline.thumbnail_review import (
    THUMBNAIL_REVIEW_APPROVAL_ACTION,
    ThumbnailReview,
    finalize_thumbnail_approval,
    load_thumbnail_review,
    request_thumbnail_approval,
)
from src.pipeline.tts import (
    QWEN3_CLONE_QUALITY_TEST_BACKEND,
    Qwen3CloneQualityTestConfig,
    TTSSynthesizer,
    execute_approved_tts_step,
    get_configured_tts_synthesizer,
)
from src.pipeline.visual_generation import (
    FLUX2_KLEIN_QUALITY_TEST_BACKEND,
    Flux2KleinQualityTestConfig,
    build_flux2_klein_quality_test_adapter,
    ollama_scene_model,
    release_ollama_scene_model,
)
from src.pipeline.visual_prompts import DEFAULT_VISUAL_STYLE, VisualStyle
from src.pipeline.visual_review import (
    VISUAL_REVIEW_APPROVAL_ACTION,
    VisualReview,
    finalize_visual_approval,
    load_visual_review,
    request_visual_approval,
)
from src.pipeline.visuals import (
    LOCAL_ASSETS_QUALITY_TEST_BACKEND,
    VisualAdapter,
    VisualCountBounds,
    build_local_asset_count_resolver,
    execute_approved_visual_step,
    get_configured_visual_adapter,
    inspect_local_asset_manifest,
    plan_visual_slots,
)
from src.platform.approval_service import approval_payload_digest, request_approval
from src.platform.artifact_service import ArtifactStorageError, register_local_artifact
from src.platform.control_service import get_execution_control_state
from src.platform.ledger_service import record_ledger_entry
from src.platform.metric_service import record_metric_point
from src.platform.models import (
    Approval,
    ApprovalStatus,
    Artifact,
    ArtifactSensitivity,
    Automation,
    LedgerEntry,
    LedgerEntryType,
    MetricKind,
    MetricPoint,
    QueueClass,
    Run,
    RunStatus,
    StepRun,
)
from src.platform.run_service import (
    RunCreationResult,
    get_or_create_automation,
    get_or_create_run,
    transition_run,
)
from src.platform.task_runner import (
    PermanentTaskError,
    RetryableTaskError,
    RetryPolicy,
    TaskAttemptContext,
    TaskStepSpec,
    execute_task_step,
)

_AUTOMATION_SLUG = "content-engine"
_STEP_NAME = "generate-script-a1"
_ARTIFACT_RETENTION_DAYS = resolve_retention_days("script_bundle")
_SOURCE = "content-engine:a1"
CONTENT_SCRIPT_APPROVAL_ACTION = "review_content_script_a1"
_MAX_BUNDLE_BYTES = 5 * 1024 * 1024

# Chamado como (topic, persona, format, recent_openings) e, quando a pauta traz
# fatos, também com o argumento nomeado `facts`.
ScriptGenerator = Callable[..., ScriptResult]


class ContentScriptInput(BaseModel):
    """Validated boundary for one manually requested A1 script."""

    topic: str = Field(min_length=3, max_length=500)
    persona: str = Field(min_length=3, max_length=1_000)
    format: Literal["short", "long"]
    recent_openings: list[str] = Field(default_factory=list, max_length=10)
    facts: list[SourcedFact] = Field(default_factory=list, max_length=10)
    # "auto" rotates the opening style by run; None asks for no particular style.
    opening_style: Literal["auto"] | OpeningStyle | None = None
    structure: Structure | None = None
    # Look of the generated visuals; used only by a generating visual backend.
    visual_style: VisualStyle | None = None

    def as_payload(self) -> dict[str, object]:
        """Return the JSON payload persisted on the run and on its step.

        Fields added after the first runs are left out when unset, so runs
        created before they existed keep the exact payload their idempotency
        keys were recorded with.
        """
        payload = self.model_dump(mode="json")
        if not self.facts:
            del payload["facts"]
        for optional in ("opening_style", "structure", "visual_style"):
            if payload[optional] is None:
                del payload[optional]
        return payload

    @field_validator("topic", "persona")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be empty")
        return normalized

    @field_validator("recent_openings")
    @classmethod
    def normalize_recent_openings(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values if value.strip()]
        if len(normalized) > 10:
            raise ValueError("recent_openings supports at most 10 entries")
        if any(len(value) > 500 for value in normalized):
            raise ValueError("recent_openings entries must not exceed 500 characters")
        return normalized


@dataclass(frozen=True, slots=True)
class ContentScriptRunResult:
    """Observable identifiers produced by one A1 run or replay."""

    automation_id: int
    run_id: int
    step_run_id: int | None
    artifact_id: int | None
    approval_id: int | None
    run_status: str
    step_status: str | None
    created: bool
    replayed: bool


class ContentScriptRunNotRunnableError(ValueError):
    """Raised when an A1 run cannot be resumed without an explicit retry."""


@dataclass(frozen=True, slots=True)
class ContentScriptReview:
    """Verified editorial projection of one immutable A1 script bundle."""

    approval_id: int
    run_id: int
    artifact_id: int
    sha256: str
    topic: str
    format: str
    angle: str
    outline: str
    hook: str
    full_script: str
    narration: str
    # Facts the operator supplied with the topic and the narration sentences the
    # deterministic check could not tie to the topic or to one of those facts.
    # None means the bundle predates the check, which is not the same as no finding.
    facts: tuple[SourcedFact, ...] = ()
    unsourced_claims: tuple[str, ...] | None = None
    # Operator-facing labels of what was asked for this script; None when nothing was.
    opening_style: str | None = None
    opening_style_met: bool | None = None
    structure: str | None = None


def parse_content_script_form(values: dict[str, str]) -> dict[str, object]:
    """Normalize dashboard fields through the same typed A1 input boundary."""
    expected = {
        "topic",
        "persona",
        "format",
        "recent_openings",
        "facts",
        "opening_style",
        "structure",
        "visual_style",
    }
    if set(values) - expected:
        raise ValueError("unsupported content script input field")
    openings = values.get("recent_openings", "")
    facts: list[dict[str, str]] = []
    for line in values.get("facts", "").splitlines():
        if not line.strip():
            continue
        # The source is whatever follows the last separator, so a claim may contain "|".
        claim, separator, source = line.rpartition("|")
        if not separator:
            raise ValueError("each fact needs its source: 'fato | fonte'")
        facts.append({"claim": claim.strip(), "source": source.strip()})
    payload = ContentScriptInput.model_validate(
        {
            "topic": values.get("topic"),
            "persona": values.get("persona"),
            "format": values.get("format"),
            "recent_openings": openings.splitlines(),
            "facts": facts,
            "opening_style": values.get("opening_style") or None,
            "structure": values.get("structure") or None,
            "visual_style": values.get("visual_style") or None,
        }
    )
    return payload.as_payload()


async def prepare_content_script_run(
    session: AsyncSession,
    *,
    idempotency_key: str,
    retry_of_run_id: int | None = None,
    retry_requested_by: str | None = None,
    retry_reason: str | None = None,
    experiment_id: int | None = None,
    trigger: str = "manual",
    input_payload: dict[str, object] | None = None,
) -> RunCreationResult:
    """Persist a validated A1 run without contacting Ollama or the broker."""
    input_data = ContentScriptInput.model_validate(input_payload or {})
    automation = (
        await get_or_create_automation(
            session,
            slug=_AUTOMATION_SLUG,
            name="Content Engine",
            owner="local-operator",
        )
    ).automation
    return await get_or_create_run(
        session,
        automation=automation,
        idempotency_key=idempotency_key,
        trigger="retry" if retry_of_run_id is not None else trigger,
        input_payload=input_data.as_payload(),
        retry_of_run_id=retry_of_run_id,
        retry_requested_by=retry_requested_by,
        retry_reason=retry_reason,
        experiment_id=experiment_id,
    )


async def execute_content_script_run(
    session: AsyncSession,
    *,
    idempotency_key: str,
    retry_of_run_id: int | None = None,
    retry_requested_by: str | None = None,
    retry_reason: str | None = None,
    experiment_id: int | None = None,
    trigger: str = "manual",
    input_payload: dict[str, object] | None = None,
    script_generator: ScriptGenerator | None = None,
    tts_synthesizer: TTSSynthesizer | None = None,
    visual_adapter: VisualAdapter | None = None,
    caption_adapter: CaptionAdapter | None = None,
    assembly_adapter: AssemblyAdapter | None = None,
    storage_root: Path | None = None,
) -> ContentScriptRunResult:
    """Execute A1 as one GPU step and persist its reviewable local evidence."""
    input_data = ContentScriptInput.model_validate(input_payload or {})
    creation = await prepare_content_script_run(
        session,
        idempotency_key=idempotency_key,
        retry_of_run_id=retry_of_run_id,
        retry_requested_by=retry_requested_by,
        retry_reason=retry_reason,
        experiment_id=experiment_id,
        trigger=trigger,
        input_payload=input_data.as_payload(),
    )
    run = creation.run
    root = Path(settings.storage_root) if storage_root is None else storage_root
    _validate_pipeline_backend_dependencies()
    step_key = f"{idempotency_key}:{_STEP_NAME}"
    current = RunStatus(run.status)
    if current in {RunStatus.AWAITING_APPROVAL, RunStatus.SUCCEEDED}:
        completed_step, artifact, approval = await _load_completed_evidence(
            session,
            run=run,
            idempotency_key=idempotency_key,
            storage_root=root,
        )
        return _result(
            run,
            completed_step,
            _persisted_id(artifact.id, "artifact"),
            approval_id=_persisted_id(approval.id, "approval"),
            created=False,
            replayed=True,
        )
    if current is RunStatus.CANCELLED:
        return _result(
            run,
            None,
            None,
            approval_id=None,
            created=creation.created,
            replayed=True,
        )
    if current is RunStatus.FAILED:
        raise ContentScriptRunNotRunnableError(
            "failed A1 run requires an explicit successor retry"
        )

    if current is RunStatus.QUEUED:
        control = await get_execution_control_state(session, run_id=_persisted_id(run.id, "run"))
        if control.blocked:
            await transition_run(
                session,
                run,
                RunStatus.CANCELLED,
                note=f"A1 start blocked by {control.code}",
            )
            return _result(
                run,
                None,
                None,
                approval_id=None,
                created=creation.created,
                replayed=False,
            )
        await transition_run(session, run, RunStatus.RUNNING, note="Content Engine A1 started")

    generator: ScriptGenerator = script_generator or generate_script
    artifact_path = _artifact_path(root, run_id=_persisted_id(run.id, "run"))

    async def generate_operation(_context: TaskAttemptContext) -> dict[str, object]:
        # Só vai ao gerador o que a pauta traz: geradores injetados que não conhecem
        # esses argumentos continuam válidos para as demais runs.
        keyword_arguments: dict[str, object] = {}
        if input_data.facts:
            keyword_arguments["facts"] = tuple(input_data.facts)
        if input_data.opening_style is not None:
            keyword_arguments["opening_style"] = _resolve_opening_style(
                input_data.opening_style,
                run_id=_persisted_id(run.id, "run"),
            )
        if input_data.structure is not None:
            keyword_arguments["structure"] = input_data.structure
        try:
            generated = await asyncio.to_thread(
                generator,
                input_data.topic,
                input_data.persona,
                input_data.format,
                input_data.recent_openings or None,
                **keyword_arguments,
            )
        except (APITimeoutError, APIConnectionError, EmptyResponseError) as exc:
            raise RetryableTaskError("CONTENT_LLM_UNAVAILABLE") from exc
        except ScriptLengthError as exc:
            # Outra geração pode acertar o tamanho, então um retry explícito é permitido.
            raise RetryableTaskError("CONTENT_SCRIPT_LENGTH_OUT_OF_RANGE") from exc
        _validate_script_result(generated)
        try:
            _write_script_bundle(
                artifact_path,
                input_data=input_data,
                result=generated,
                model=settings.ollama_model,
            )
        except OSError as exc:
            raise RetryableTaskError("CONTENT_STORAGE_WRITE_FAILED") from exc
        return {
            "artifact_relative_path": artifact_path.relative_to(root.resolve()).as_posix(),
            "format": input_data.format,
            "model": settings.ollama_model,
            "full_script_characters": len(generated.full_script),
            "narration_characters": len(generated.narration),
            "unsourced_claims": len(generated.unsourced_claims or []),
            "opening_style": generated.opening_style,
            "opening_style_met": generated.opening_style_met,
            "structure": generated.structure,
        }

    task_result = await execute_task_step(
        session,
        run=run,
        spec=TaskStepSpec(
            name=_STEP_NAME,
            queue=QueueClass.GPU,
            ordinal=1,
            idempotency_key=step_key,
            input_payload=input_data.as_payload(),
        ),
        policy=RetryPolicy(
            max_attempts=1,
            timeout_seconds=max(1.0, float(settings.celery_soft_time_limit_seconds - 15)),
        ),
        operation=generate_operation,
    )
    step_run = await session.get(StepRun, task_result.step_run_ids[-1])
    if step_run is None:
        raise ContentScriptRunNotRunnableError("A1 step result was not persisted")

    if task_result.status is RunStatus.CANCELLED:
        return _result(
            run,
            step_run,
            None,
            approval_id=None,
            created=creation.created,
            replayed=task_result.replayed,
        )
    if task_result.status is RunStatus.FAILED:
        if task_result.error is None:
            raise ContentScriptRunNotRunnableError("failed A1 step is missing structured error")
        if RunStatus(run.status) is RunStatus.RUNNING:
            await transition_run(
                session,
                run,
                RunStatus.FAILED,
                error=task_result.error,
                note="Content Engine A1 failed",
            )
        return _result(
            run,
            step_run,
            None,
            approval_id=None,
            created=creation.created,
            replayed=task_result.replayed,
        )

    try:
        async with session.begin_nested():
            automation = await session.get(Automation, run.automation_id)
            if automation is None:
                raise ContentScriptRunNotRunnableError(
                    "A1 automation was not persisted"
                )
            artifact = (
                await register_local_artifact(
                    session,
                    run=run,
                    step_run=step_run,
                    storage_root=root,
                    file_path=artifact_path,
                    idempotency_key=f"{idempotency_key}:script-bundle",
                    artifact_type="script_bundle",
                    media_type="application/json",
                    origin=_SOURCE,
                    sensitivity=ArtifactSensitivity.INTERNAL,
                    retention_days=_ARTIFACT_RETENTION_DAYS,
                )
            ).artifact
            output = task_result.output_payload or {}
            await record_metric_point(
                session,
                automation=automation,
                run=run,
                step_run=step_run,
                idempotency_key=f"{idempotency_key}:narration-characters",
                name="content_script_narration_characters",
                kind=MetricKind.COUNTER,
                value=int(output.get("narration_characters", 0)),
                unit="characters",
                source=_SOURCE,
                dimensions={
                    "format": input_data.format,
                    "model": settings.ollama_model,
                },
            )
            await record_ledger_entry(
                session,
                automation=automation,
                run=run,
                step_run=step_run,
                idempotency_key=f"{idempotency_key}:external-api-cost",
                entry_type=LedgerEntryType.COST,
                category="external_api",
                amount=0,
                currency="BRL",
                source=_SOURCE,
                confidence=1,
            )
            bundle = _load_verified_script_bundle(
                root,
                artifact=artifact,
                input_data=input_data,
            )
            approval = (
                await request_approval(
                    session,
                    run=run,
                    idempotency_key=f"{idempotency_key}:script-review",
                    action=CONTENT_SCRIPT_APPROVAL_ACTION,
                    summary="Revisar o roteiro A1 antes de qualquer etapa audiovisual",
                    input_payload=_approval_input(
                        run=run,
                        step_run=step_run,
                        artifact=artifact,
                        narration=bundle.narration,
                    ),
                    review_payload=_approval_review_payload(
                        input_data=input_data,
                        result=bundle,
                        artifact=artifact,
                    ),
                )
            ).approval
    except Exception as exc:
        evidence_error = {
            "code": "CONTENT_EVIDENCE_PERSIST_FAILED",
            "exception_type": type(exc).__name__,
            "retryable": True,
            "timed_out": False,
        }
        if RunStatus(run.status) is RunStatus.RUNNING:
            await transition_run(
                session,
                run,
                RunStatus.FAILED,
                error=evidence_error,
                note="Content Engine A1 evidence persistence failed",
            )
        return _result(
            run,
            step_run,
            None,
            approval_id=None,
            created=creation.created,
            replayed=task_result.replayed,
        )
    if ApprovalStatus(approval.status) is ApprovalStatus.APPROVED and _tts_enabled():
        approval_input = _approval_input(
            run=run,
            step_run=step_run,
            artifact=artifact,
            narration=bundle.narration,
        )
        tts_result = await execute_approved_tts_step(
            session,
            run=run,
            approval=approval,
            approval_input_payload=approval_input,
            source_artifact=artifact,
            narration=bundle.narration,
            idempotency_key=idempotency_key,
            storage_root=root,
            synthesizer=(
                tts_synthesizer
                or get_configured_tts_synthesizer(
                    _tts_backend(),
                    qwen3_clone_config=_qwen3_clone_quality_test_config(),
                )
            ),
            voice_id=_tts_voice_id(),
            language_code=_tts_language_code(),
            gpu_power_watts=int(getattr(settings, "gpu_power_watts", 400)),
            timeout_seconds=max(
                1.0,
                float(settings.celery_soft_time_limit_seconds - 15),
            ),
            finalize_run=not (
                _narration_review_enabled()
                or _visuals_enabled()
                or _captions_enabled()
                or _assembly_enabled()
            ),
        )
        visual_replayed = True
        caption_replayed = True
        assembly_replayed = True
        similarity_replayed = True
        alignment_gate_replayed = True
        audio_artifact: Artifact | None = None
        if (
            (
                _narration_review_enabled()
                or _visuals_enabled()
                or _captions_enabled()
            )
            and tts_result.artifact_id is not None
            and RunStatus(run.status) is RunStatus.RUNNING
        ):
            audio_artifact = await session.get(Artifact, tts_result.artifact_id)
            if audio_artifact is None:
                raise ContentScriptRunNotRunnableError(
                    "completed A2 step is missing its audio artifact"
                )
        if (
            _narration_review_enabled()
            and audio_artifact is not None
            and RunStatus(run.status) is RunStatus.RUNNING
        ):
            await request_narration_approval(
                session,
                run=run,
                script_approval=approval,
                script_approval_input_payload=approval_input,
                script_artifact=artifact,
                audio_artifact=audio_artifact,
                idempotency_key=idempotency_key,
                storage_root=root,
            )
        visual_result = None
        if (
            _visuals_enabled()
            and audio_artifact is not None
            and RunStatus(run.status) is RunStatus.RUNNING
        ):
            visual_result = await execute_approved_visual_step(
                session,
                run=run,
                approval=approval,
                approval_input_payload=approval_input,
                script_artifact=artifact,
                audio_artifact=audio_artifact,
                idempotency_key=idempotency_key,
                storage_root=root,
                adapter=visual_adapter or _configured_visual_adapter(input_data),
                allow_quality_test_assets=(
                    _visuals_backend() == FLUX2_KLEIN_QUALITY_TEST_BACKEND
                ),
                queue=_visuals_queue(),
                gpu_power_watts=int(getattr(settings, "gpu_power_watts", 400)),
                timeout_seconds=max(
                    1.0,
                    float(settings.celery_soft_time_limit_seconds - 15),
                ),
                finalize_run=not (
                    _visual_review_enabled()
                    or _captions_enabled()
                    or _assembly_enabled()
                ),
                count_resolver=(
                    build_local_asset_count_resolver(
                        import_root=_visual_import_root(),
                        manifest_path=_visual_manifest_path(),
                    )
                    if visual_adapter is None
                    and _visuals_backend() == LOCAL_ASSETS_QUALITY_TEST_BACKEND
                    else None
                ),
            )
            visual_replayed = visual_result.replayed
            if (
                _visual_review_enabled()
                and visual_result.manifest_artifact_id is not None
                and RunStatus(run.status) is RunStatus.RUNNING
            ):
                # Pauses the run until a person has looked at every image.
                await request_visual_approval(
                    session,
                    run=run,
                    idempotency_key=idempotency_key,
                    storage_root=root,
                )
        caption_result = None
        if (
            _captions_enabled()
            and audio_artifact is not None
            and RunStatus(run.status) is RunStatus.RUNNING
        ):
            caption_result = await execute_approved_caption_step(
                session,
                run=run,
                approval=approval,
                approval_input_payload=approval_input,
                audio_artifact=audio_artifact,
                narration=bundle.narration,
                idempotency_key=idempotency_key,
                storage_root=root,
                adapter=(
                    caption_adapter
                    or get_configured_caption_adapter(
                        _captions_backend(),
                        narration=bundle.narration,
                        faster_whisper_config=_faster_whisper_quality_test_config(),
                    )
                ),
                language_code="pt",
                gpu_power_watts=int(getattr(settings, "gpu_power_watts", 400)),
                timeout_seconds=max(
                    1.0,
                    float(settings.celery_soft_time_limit_seconds - 15),
                ),
                queue=_captions_queue(),
                visual_manifest_artifact_id=(
                    visual_result.manifest_artifact_id
                    if visual_result is not None
                    else None
                ),
                visual_artifact_ids=(
                    visual_result.visual_artifact_ids
                    if visual_result is not None
                    else ()
                ),
                finalize_run=not _assembly_enabled(),
            )
            caption_replayed = caption_result.replayed
        if (
            _assembly_enabled()
            and audio_artifact is not None
            and visual_result is not None
            and caption_result is not None
            and RunStatus(run.status) is RunStatus.RUNNING
        ):
            visual_manifest = await session.get(
                Artifact,
                visual_result.manifest_artifact_id,
            )
            caption_artifact = await session.get(Artifact, caption_result.artifact_id)
            loaded_visuals: list[Artifact] = []
            for artifact_id in visual_result.visual_artifact_ids:
                visual_artifact = await session.get(Artifact, artifact_id)
                if visual_artifact is None:
                    raise ContentScriptRunNotRunnableError(
                        "completed A3 step is missing an assembly source image"
                    )
                loaded_visuals.append(visual_artifact)
            if visual_manifest is None or caption_artifact is None:
                raise ContentScriptRunNotRunnableError(
                    "completed A3/A4 steps are missing assembly source artifacts"
                )
            assembly_result = await execute_approved_assembly_step(
                session,
                run=run,
                approval=approval,
                approval_input_payload=approval_input,
                audio_artifact=audio_artifact,
                visual_manifest_artifact=visual_manifest,
                visual_artifacts=tuple(loaded_visuals),
                caption_artifact=caption_artifact,
                idempotency_key=idempotency_key,
                storage_root=root,
                adapter=(
                    assembly_adapter
                    or get_configured_assembly_adapter(
                        _assembly_backend(),
                        quality_test_config=_ffmpeg_quality_test_config(),
                    )
                ),
                cpu_power_watts=int(getattr(settings, "cpu_power_watts", 150)),
                timeout_seconds=max(
                    1.0,
                    float(settings.celery_soft_time_limit_seconds - 15),
                ),
                finalize_run=False,
            )
            assembly_replayed = assembly_result.replayed
            if (
                assembly_result.artifact_id is not None
                and RunStatus(run.status) is RunStatus.RUNNING
            ):
                final_video = await session.get(Artifact, assembly_result.artifact_id)
                if final_video is None:
                    raise ContentScriptRunNotRunnableError(
                        "completed A5 step is missing its final video artifact"
                    )
                similarity_result = await execute_similarity_gate_step(
                    session,
                    run=run,
                    script_approval=approval,
                    approval_input_payload=approval_input,
                    script_artifact=artifact,
                    final_video_artifact=final_video,
                    idempotency_key=idempotency_key,
                    storage_root=root,
                    threshold=Decimal(
                        str(getattr(settings, "content_similarity_threshold", 0.85))
                    ),
                    reference_limit=int(
                        getattr(settings, "content_similarity_window", 20)
                    ),
                    cpu_power_watts=int(getattr(settings, "cpu_power_watts", 150)),
                    timeout_seconds=max(
                        1.0,
                        float(settings.celery_soft_time_limit_seconds - 15),
                    ),
                    finalize_run=not _final_review_enabled(),
                )
                similarity_replayed = similarity_result.replayed
                if (
                    _final_review_enabled()
                    and not similarity_result.blocked
                    and similarity_result.report_artifact_id is not None
                    and RunStatus(run.status) is RunStatus.RUNNING
                ):
                    report_artifact = await session.get(
                        Artifact,
                        similarity_result.report_artifact_id,
                    )
                    if report_artifact is None:
                        raise ContentScriptRunNotRunnableError(
                            "completed A6 step is missing its originality report"
                        )
                    if _caption_alignment_gate_enabled():
                        alignment_gate = await execute_caption_alignment_gate_step(
                            session,
                            run=run,
                            idempotency_key=idempotency_key,
                            storage_root=root,
                            parameters=build_alignment_parameters(settings),
                            timeout_seconds=max(
                                1.0,
                                float(settings.celery_soft_time_limit_seconds - 15),
                            ),
                        )
                        alignment_gate_replayed = alignment_gate.replayed
                    if RunStatus(run.status) is RunStatus.RUNNING:
                        await request_final_video_approval(
                            session,
                            run=run,
                            script_approval=approval,
                            approval_input_payload=approval_input,
                            final_video_artifact=final_video,
                            originality_report_artifact=report_artifact,
                            idempotency_key=idempotency_key,
                            storage_root=root,
                        )
        return _result(
            run,
            step_run,
            _persisted_id(artifact.id, "artifact"),
            approval_id=_persisted_id(approval.id, "approval"),
            created=creation.created,
            replayed=(
                task_result.replayed
                and tts_result.replayed
                and visual_replayed
                and caption_replayed
                and assembly_replayed
                and similarity_replayed
                and alignment_gate_replayed
            ),
        )
    return _result(
        run,
        step_run,
        _persisted_id(artifact.id, "artifact"),
        approval_id=_persisted_id(approval.id, "approval"),
        created=creation.created,
        replayed=task_result.replayed,
    )


async def load_content_approval_review(
    session: AsyncSession,
    *,
    approval: Approval,
    storage_root: Path | None = None,
) -> (
    ContentScriptReview
    | NarrationReview
    | VisualReview
    | FinalVideoReview
    | ThumbnailReview
):
    """Route one Content Engine approval to its own verified review projection."""
    root = Path(settings.storage_root) if storage_root is None else storage_root
    if approval.action == VISUAL_REVIEW_APPROVAL_ACTION:
        return await load_visual_review(
            session,
            approval=approval,
            storage_root=root,
        )
    if approval.action == NARRATION_REVIEW_APPROVAL_ACTION:
        return _with_visual_plan_status(
            await load_narration_review(
                session,
                approval=approval,
                storage_root=root,
            )
        )
    if approval.action == THUMBNAIL_REVIEW_APPROVAL_ACTION:
        return await load_thumbnail_review(
            session,
            approval=approval,
            storage_root=root,
        )
    if approval.action == FINAL_REVIEW_APPROVAL_ACTION:
        return await load_final_video_review(
            session,
            approval=approval,
            storage_root=root,
        )
    return await load_content_script_review(
        session,
        approval=approval,
        storage_root=root,
    )


async def load_content_local_export(
    session: AsyncSession,
    *,
    run: Run,
    storage_root: Path | None = None,
) -> LocalExportPackage:
    """Load a checksum-verified, local-only export for one completed run."""
    root = Path(settings.storage_root) if storage_root is None else storage_root
    return await load_local_export_package(
        session,
        run=run,
        storage_root=root,
    )


async def load_content_script_review(
    session: AsyncSession,
    *,
    approval: Approval,
    storage_root: Path | None = None,
) -> ContentScriptReview:
    """Load the complete, checksum-verified A1 output without private prompt context."""
    if approval.action != CONTENT_SCRIPT_APPROVAL_ACTION:
        raise ContentScriptRunNotRunnableError("approval is not an A1 script review")
    run = await session.get(Run, approval.run_id)
    if run is None:
        raise ContentScriptRunNotRunnableError("approval run does not exist")
    root = Path(settings.storage_root) if storage_root is None else storage_root
    step_run, artifact = await _load_script_evidence(session, run=run)
    input_data = ContentScriptInput.model_validate(run.input_payload or {})
    result = _load_verified_script_bundle(
        root,
        artifact=artifact,
        input_data=input_data,
    )
    expected = _approval_input(
        run=run,
        step_run=step_run,
        artifact=artifact,
        narration=result.narration,
    )
    if approval.payload_digest != approval_payload_digest(expected):
        raise ContentScriptRunNotRunnableError("approval does not match the A1 artifact")
    return ContentScriptReview(
        approval_id=_persisted_id(approval.id, "approval"),
        run_id=_persisted_id(run.id, "run"),
        artifact_id=_persisted_id(artifact.id, "artifact"),
        sha256=artifact.sha256,
        topic=input_data.topic,
        format=input_data.format,
        angle=result.angle,
        outline=result.outline,
        hook=result.hook,
        full_script=result.full_script,
        narration=result.narration,
        facts=tuple(input_data.facts),
        unsourced_claims=(
            None if result.unsourced_claims is None else tuple(result.unsourced_claims)
        ),
        opening_style=_known_label(OPENING_STYLE_LABELS, result.opening_style),
        opening_style_met=result.opening_style_met,
        structure=_known_label(STRUCTURE_LABELS, result.structure),
    )


async def finalize_content_script_approval(
    session: AsyncSession,
    *,
    approval: Approval,
    storage_root: Path | None = None,
) -> bool:
    """Verify approval and report whether the durable dispatch must continue."""
    root = Path(settings.storage_root) if storage_root is None else storage_root
    if approval.action == NARRATION_REVIEW_APPROVAL_ACTION:
        continuation_required = await finalize_narration_approval(
            session,
            approval=approval,
            storage_root=root,
        )
        if continuation_required:
            # Refusing here rolls the decision back, so the gate stays open
            # while the operator finishes the manifest.
            narration_review = _with_visual_plan_status(
                await load_narration_review(
                    session,
                    approval=approval,
                    storage_root=root,
                )
            )
            if narration_review.visual_plan_problem_code is not None:
                raise NarrationVisualsNotReadyError(
                    problem_code=narration_review.visual_plan_problem_code,
                    minimum_count=narration_review.minimum_visual_count,
                    maximum_count=narration_review.maximum_visual_count,
                    declared_count=narration_review.declared_visual_count,
                )
        return continuation_required
    if approval.action == VISUAL_REVIEW_APPROVAL_ACTION:
        return await finalize_visual_approval(
            session,
            approval=approval,
            storage_root=root,
        )
    if approval.action == THUMBNAIL_REVIEW_APPROVAL_ACTION:
        return await finalize_thumbnail_approval(
            session,
            approval=approval,
            storage_root=root,
        )
    if approval.action == FINAL_REVIEW_APPROVAL_ACTION:
        if _thumbnail_review_enabled():
            await request_thumbnail_approval(
                session,
                final_review_approval=approval,
                storage_root=root,
            )
            return False
        return await finalize_final_video_approval(
            session,
            approval=approval,
            storage_root=root,
        )
    if approval.action != CONTENT_SCRIPT_APPROVAL_ACTION:
        return False
    if ApprovalStatus(approval.status) is not ApprovalStatus.APPROVED:
        raise ContentScriptRunNotRunnableError("A1 script approval is not approved")
    review = await load_content_script_review(
        session,
        approval=approval,
        storage_root=storage_root,
    )
    run = await session.get(Run, approval.run_id)
    if run is None:
        raise ContentScriptRunNotRunnableError("approval run does not exist")
    step_run = await _required_script_step(session, run=run)
    metric = await session.scalar(
        select(MetricPoint).where(
            MetricPoint.run_id == run.id,
            MetricPoint.idempotency_key
            == f"{run.idempotency_key}:narration-characters",
        )
    )
    ledger = await session.scalar(
        select(LedgerEntry).where(
            LedgerEntry.run_id == run.id,
            LedgerEntry.idempotency_key
            == f"{run.idempotency_key}:external-api-cost",
        )
    )
    if metric is None or ledger is None:
        raise ContentScriptRunNotRunnableError(
            "approved A1 run is missing metric or ledger evidence"
        )
    output_payload = {
        "step_run_id": step_run.id,
        "artifact_id": review.artifact_id,
        "approval_id": review.approval_id,
        "metric_point_id": metric.id,
        "ledger_entry_id": ledger.id,
    }
    current = RunStatus(run.status)
    if current is RunStatus.SUCCEEDED:
        completed_output = run.output_payload or {}
        if "tts_artifact_id" in completed_output:
            if (
                completed_output.get("script_artifact_id") != review.artifact_id
                or completed_output.get("script_approval_id") != review.approval_id
            ):
                raise ContentScriptRunNotRunnableError(
                    "completed A2 run has conflicting approval evidence"
                )
            return False
        if completed_output != output_payload:
            raise ContentScriptRunNotRunnableError(
                "completed A1 run has conflicting approval evidence"
            )
        return False
    if current is not RunStatus.RUNNING:
        raise ContentScriptRunNotRunnableError(
            "approved A1 run must be running before finalization"
        )
    if _tts_enabled():
        return True
    await transition_run(
        session,
        run,
        RunStatus.SUCCEEDED,
        output_payload=output_payload,
        note="Content Engine A1 editorially approved; no external effect executed",
    )
    return False


def _tts_backend() -> str:
    value = getattr(settings, "content_tts_backend", "disabled")
    return value.strip().casefold() if isinstance(value, str) else "disabled"


def _tts_enabled() -> bool:
    return _tts_backend() != "disabled"


def _qwen3_clone_quality_test_config() -> Qwen3CloneQualityTestConfig | None:
    if _tts_backend() != QWEN3_CLONE_QUALITY_TEST_BACKEND:
        return None
    paths = [
        getattr(settings, field, None)
        for field in (
            "content_tts_qwen3_python_path",
            "content_tts_qwen3_model_dir",
            "content_tts_reference_audio_path",
            "content_tts_reference_text_path",
        )
    ]
    if not all(isinstance(path, str) and path.strip() for path in paths):
        raise ContentScriptRunNotRunnableError(
            "Qwen3-TTS clone quality test requires its local paths"
        )
    python_path, model_dir, reference_audio, reference_text = (str(path) for path in paths)
    return Qwen3CloneQualityTestConfig(
        python_path=python_path,
        model_dir=model_dir,
        reference_audio_path=reference_audio,
        reference_text_path=reference_text,
        # Leave room inside the step timeout for hashing the weights and validating the WAV.
        timeout_seconds=max(1.0, float(settings.celery_soft_time_limit_seconds - 45)),
    )


def _narration_review_enabled() -> bool:
    return getattr(settings, "content_narration_review_enabled", False) is True


def _tts_voice_id() -> str:
    value = getattr(settings, "content_tts_voice_id", "pf_dora")
    return value if isinstance(value, str) else "pf_dora"


def _tts_language_code() -> str:
    value = getattr(settings, "content_tts_language_code", "p")
    return value if isinstance(value, str) else "p"


def _visuals_backend() -> str:
    value = getattr(settings, "content_visual_backend", "disabled")
    return value.strip().casefold() if isinstance(value, str) else "disabled"


def _visuals_enabled() -> bool:
    return _visuals_backend() != "disabled"


def _configured_visual_adapter(input_data: ContentScriptInput) -> VisualAdapter:
    """Build the visual adapter the settings name, with this run's topic and style."""
    if _visuals_backend() != FLUX2_KLEIN_QUALITY_TEST_BACKEND:
        return get_configured_visual_adapter(
            _visuals_backend(),
            import_root=_visual_import_root(),
            manifest_path=_visual_manifest_path(),
        )
    paths = [
        getattr(settings, field, None)
        for field in (
            "content_visual_flux2_python_path",
            "content_visual_flux2_model_dir",
        )
    ]
    if not all(isinstance(path, str) and path.strip() for path in paths):
        raise ContentScriptRunNotRunnableError(
            "FLUX.2 klein quality test requires its local paths"
        )
    python_path, model_dir = (str(path) for path in paths)
    return build_flux2_klein_quality_test_adapter(
        Flux2KleinQualityTestConfig(
            python_path=python_path,
            model_dir=model_dir,
            # The same step also writes the scene descriptions and hashes the weights.
            timeout_seconds=max(1.0, float(settings.celery_soft_time_limit_seconds - 90)),
        ),
        scene_model=ollama_scene_model(),
        topic=input_data.topic,
        style=input_data.visual_style or DEFAULT_VISUAL_STYLE,
        release_scene_model=release_ollama_scene_model,
    )


def _visual_import_root() -> Path:
    return Path(
        str(getattr(settings, "content_visual_import_root", "./imports/content-visuals"))
    )


def _visual_manifest_path() -> Path:
    return Path(str(getattr(settings, "content_visual_manifest_path", "manifest.json")))


def _with_visual_plan_status(review: NarrationReview) -> NarrationReview:
    """Attach the manifest readiness when the visual backend is operator-supplied."""
    if _visuals_backend() != LOCAL_ASSETS_QUALITY_TEST_BACKEND:
        return review
    status = inspect_local_asset_manifest(
        import_root=_visual_import_root(),
        manifest_path=_visual_manifest_path(),
        bounds=VisualCountBounds(
            minimum=review.minimum_visual_count,
            default=review.required_visual_count,
            maximum=review.maximum_visual_count,
        ),
    )
    visual_plan = review.visual_plan
    if status.ready and status.declared_count is not None:
        # Re-plan for the count the manifest declares and name each image.
        visual_plan = tuple(
            replace(slot, file_name=file_name)
            for slot, file_name in zip(
                plan_visual_slots(
                    review.narration,
                    duration_seconds=Decimal(review.duration_seconds),
                    count=status.declared_count,
                ),
                status.relative_paths,
                strict=False,
            )
        )
    return replace(
        review,
        visual_plan=visual_plan,
        visual_plan_checked=True,
        visual_plan_problem_code=status.problem_code,
        visual_plan_problem=(
            None
            if status.problem_code is None
            else describe_visual_plan_problem(
                status.problem_code,
                minimum_count=status.bounds.minimum,
                maximum_count=status.bounds.maximum,
                declared_count=status.declared_count,
            )
        ),
        declared_visual_count=status.declared_count,
    )


def _visuals_queue() -> QueueClass:
    if _visuals_backend() == "local_assets_quality_test":
        return QueueClass.IO
    return QueueClass.GPU


def _captions_backend() -> str:
    value = getattr(settings, "content_caption_backend", "disabled")
    return value.strip().casefold() if isinstance(value, str) else "disabled"


def _captions_enabled() -> bool:
    return _captions_backend() != "disabled"


def _captions_queue() -> QueueClass:
    if _captions_backend() == "approved_text_timing_quality_test":
        return QueueClass.CPU
    return QueueClass.GPU


def _faster_whisper_quality_test_config() -> FasterWhisperQualityTestConfig | None:
    if _captions_backend() != FASTER_WHISPER_QUALITY_TEST_BACKEND:
        return None
    model_path = getattr(settings, "content_caption_model_path", None)
    if not isinstance(model_path, str) or not model_path.strip():
        raise ContentScriptRunNotRunnableError(
            "faster-whisper quality test requires a local model path"
        )
    return FasterWhisperQualityTestConfig(model_path=Path(model_path))


def _assembly_backend() -> str:
    value = getattr(settings, "content_assembly_backend", "disabled")
    return value.strip().casefold() if isinstance(value, str) else "disabled"


def _assembly_enabled() -> bool:
    return _assembly_backend() != "disabled"


def _final_review_enabled() -> bool:
    return getattr(settings, "content_final_review_enabled", False) is True


def _caption_alignment_gate_enabled() -> bool:
    return getattr(settings, "content_caption_alignment_gate_enabled", False) is True


def _visual_review_enabled() -> bool:
    return getattr(settings, "content_visual_review_enabled", False) is True


def _thumbnail_review_enabled() -> bool:
    return getattr(settings, "content_thumbnail_review_enabled", False) is True


def _ffmpeg_quality_test_config() -> FFmpegQualityTestConfig | None:
    if _assembly_backend() != "ffmpeg_quality_test":
        return None
    ffmpeg_hash = getattr(settings, "content_ffmpeg_expected_sha256", None)
    ffprobe_hash = getattr(settings, "content_ffprobe_expected_sha256", None)
    if not isinstance(ffmpeg_hash, str) or not isinstance(ffprobe_hash, str):
        raise ContentScriptRunNotRunnableError(
            "FFmpeg quality test requires pinned binary hashes"
        )
    return FFmpegQualityTestConfig(
        ffmpeg_path=str(getattr(settings, "content_ffmpeg_path", "ffmpeg")),
        ffprobe_path=str(getattr(settings, "content_ffprobe_path", "ffprobe")),
        ffmpeg_sha256=ffmpeg_hash,
        ffprobe_sha256=ffprobe_hash,
        timeout_seconds=max(
            1.0,
            float(settings.celery_soft_time_limit_seconds - 15),
        ),
        edit="motion" if getattr(settings, "content_assembly_edit", "static") == "motion" else "static",
    )


def _validate_pipeline_backend_dependencies() -> None:
    if _thumbnail_review_enabled() and not _final_review_enabled():
        raise ContentScriptRunNotRunnableError(
            "A8 thumbnail review requires A7 final review"
        )
    if (
        _narration_review_enabled()
        or _visuals_enabled()
        or _captions_enabled()
        or _assembly_enabled()
    ) and not _tts_enabled():
        raise ContentScriptRunNotRunnableError(
            "audiovisual continuation requires A2 TTS"
        )
    if _caption_alignment_gate_enabled() and not _final_review_enabled():
        raise ContentScriptRunNotRunnableError(
            "caption alignment gate requires A7 final review"
        )
    if _caption_alignment_gate_enabled() and not _captions_enabled():
        raise ContentScriptRunNotRunnableError(
            "caption alignment gate requires A4 captions"
        )
    if _caption_alignment_gate_enabled() and not _assembly_enabled():
        raise ContentScriptRunNotRunnableError(
            "caption alignment gate requires A5 assembly"
        )
    if _narration_review_enabled() and not _visuals_enabled():
        raise ContentScriptRunNotRunnableError(
            "A2 narration review requires A3 visual continuation"
        )
    if _visual_review_enabled() and not (
        _visuals_enabled() and _captions_enabled() and _assembly_enabled()
    ):
        raise ContentScriptRunNotRunnableError(
            "A3 visual review requires A3 visuals, A4 captions and A5 assembly"
        )
    if (
        _visuals_backend() == FLUX2_KLEIN_QUALITY_TEST_BACKEND
        and not _visual_review_enabled()
    ):
        raise ContentScriptRunNotRunnableError(
            "generated visuals require the A3 visual review gate"
        )
    if _assembly_enabled() and not (_visuals_enabled() and _captions_enabled()):
        raise ContentScriptRunNotRunnableError(
            "A5 assembly requires both A3 visuals and A4 captions"
        )


async def _load_completed_evidence(
    session: AsyncSession,
    *,
    run: Run,
    idempotency_key: str,
    storage_root: Path,
) -> tuple[StepRun, Artifact, Approval]:
    step_run, artifact = await _load_script_evidence(session, run=run)
    approval = await session.scalar(
        select(Approval).where(
            Approval.run_id == run.id,
            Approval.idempotency_key == f"{idempotency_key}:script-review",
        )
    )
    if approval is None:
        raise ContentScriptRunNotRunnableError(
            "completed A1 generation is missing its editorial approval"
        )
    await load_content_script_review(
        session,
        approval=approval,
        storage_root=storage_root,
    )
    current = RunStatus(run.status)
    approval_status = ApprovalStatus(approval.status)
    if current is RunStatus.AWAITING_APPROVAL and approval_status is not ApprovalStatus.PENDING:
        waiting_on_later_review = (
            approval_status is ApprovalStatus.APPROVED
            and (
                await _has_pending_gate(
                    session,
                    run=run,
                    action=NARRATION_REVIEW_APPROVAL_ACTION,
                )
                or await _has_pending_gate(
                    session,
                    run=run,
                    action=VISUAL_REVIEW_APPROVAL_ACTION,
                )
                or await _has_pending_gate(
                    session,
                    run=run,
                    action=FINAL_REVIEW_APPROVAL_ACTION,
                )
                or await _has_pending_gate(
                    session,
                    run=run,
                    action=THUMBNAIL_REVIEW_APPROVAL_ACTION,
                )
            )
        )
        if not waiting_on_later_review:
            raise ContentScriptRunNotRunnableError(
                "waiting A1 run has an invalid approval state"
            )
    if current is RunStatus.SUCCEEDED and approval_status is not ApprovalStatus.APPROVED:
        raise ContentScriptRunNotRunnableError("completed A1 run was not approved")
    return step_run, artifact, approval


async def _has_pending_gate(
    session: AsyncSession,
    *,
    run: Run,
    action: str,
) -> bool:
    pending = await session.scalar(
        select(Approval.id).where(
            Approval.run_id == run.id,
            Approval.action == action,
            Approval.status == ApprovalStatus.PENDING.value,
        )
    )
    return pending is not None


async def _load_script_evidence(
    session: AsyncSession,
    *,
    run: Run,
) -> tuple[StepRun, Artifact]:
    step_run = await _required_script_step(session, run=run)
    artifact = await session.scalar(
        select(Artifact).where(
            Artifact.run_id == run.id,
            Artifact.idempotency_key == f"{run.idempotency_key}:script-bundle",
        )
    )
    if artifact is None or artifact.step_run_id != step_run.id:
        raise ContentScriptRunNotRunnableError(
            "A1 run is missing its durable script artifact"
        )
    return step_run, artifact


async def _required_script_step(session: AsyncSession, *, run: Run) -> StepRun:
    step_run = await session.scalar(
        select(StepRun).where(
            StepRun.run_id == run.id,
            StepRun.idempotency_key == f"{run.idempotency_key}:{_STEP_NAME}",
            StepRun.status == RunStatus.SUCCEEDED.value,
        )
    )
    if step_run is None:
        raise ContentScriptRunNotRunnableError(
            "A1 run is missing its successful generation step"
        )
    return step_run


def _approval_input(
    *,
    run: Run,
    step_run: StepRun,
    artifact: Artifact,
    narration: str,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "run_id": _persisted_id(run.id, "run"),
        "step_run_id": _persisted_id(step_run.id, "step"),
        "artifact_id": _persisted_id(artifact.id, "artifact"),
        "artifact_sha256": artifact.sha256,
        "narration_sha256": hashlib.sha256(narration.encode("utf-8")).hexdigest(),
    }


def _approval_review_payload(
    *,
    input_data: ContentScriptInput,
    result: ScriptResult,
    artifact: Artifact,
) -> dict[str, str]:
    return {
        "topic": _preview(input_data.topic),
        "format": "Short" if input_data.format == "short" else "Longo",
        "angle": _preview(result.angle),
        "hook": _preview(result.hook),
        "script_preview": _preview(result.full_script),
        "narration_preview": _preview(result.narration),
        "artifact": f"Script bundle #{_persisted_id(artifact.id, 'artifact')}",
        "sha256": artifact.sha256,
    }


def _preview(value: str) -> str:
    normalized = value.strip()
    return normalized if len(normalized) <= 300 else f"{normalized[:297]}..."


def _load_verified_script_bundle(
    storage_root: Path,
    *,
    artifact: Artifact,
    input_data: ContentScriptInput,
) -> ScriptResult:
    if artifact.size_bytes > _MAX_BUNDLE_BYTES:
        raise ContentScriptRunNotRunnableError("A1 script bundle exceeds the review limit")
    try:
        root = storage_root.resolve(strict=True)
        candidate = (root / artifact.relative_path).resolve(strict=True)
        candidate.relative_to(root)
        if not candidate.is_file():
            raise OSError("not a regular file")
        encoded = candidate.read_bytes()
    except (OSError, ValueError) as exc:
        raise ArtifactStorageError("A1 script bundle is unavailable") from exc
    if len(encoded) != artifact.size_bytes or len(encoded) > _MAX_BUNDLE_BYTES:
        raise ArtifactStorageError("A1 script bundle size does not match durable metadata")
    if hashlib.sha256(encoded).hexdigest() != artifact.sha256:
        raise ArtifactStorageError("A1 script bundle checksum does not match durable metadata")
    try:
        payload = json.loads(encoded.decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise ValueError("unsupported schema")
        if payload.get("topic") != input_data.topic:
            raise ValueError("topic mismatch")
        if payload.get("persona") != input_data.persona:
            raise ValueError("persona mismatch")
        if payload.get("format") != input_data.format:
            raise ValueError("format mismatch")
        # Bundles written before the field existed carry no facts at all.
        if payload.get("facts", []) != [
            fact.model_dump(mode="json") for fact in input_data.facts
        ]:
            raise ValueError("facts mismatch")
        result = ScriptResult.model_validate(payload.get("result"))
        if any(
            not getattr(result, field).strip()
            for field in ("angle", "outline", "full_script", "hook", "narration")
        ):
            raise ValueError("empty script field")
    except (UnicodeError, ValueError, TypeError) as exc:
        raise ContentScriptRunNotRunnableError("A1 script bundle is invalid") from exc
    return result


def _known_label(labels: Mapping[Any, str], value: str | None) -> str | None:
    """Operator-facing label of a known value; other text from the bundle renders nothing."""
    return None if value is None else labels.get(value)


def _resolve_opening_style(
    requested: Literal["auto"] | OpeningStyle,
    *,
    run_id: int,
) -> OpeningStyle:
    """Turn "auto" into one concrete style, stable for the run and rotating across runs."""
    if requested != "auto":
        return requested
    return OPENING_STYLES[run_id % len(OPENING_STYLES)]


def _validate_script_result(result: ScriptResult) -> None:
    for field in ("angle", "outline", "full_script", "hook", "narration"):
        if not getattr(result, field).strip():
            raise PermanentTaskError("CONTENT_SCRIPT_INVALID_OUTPUT")


def _artifact_path(storage_root: Path, *, run_id: int) -> Path:
    root = storage_root.resolve()
    if root == Path(root.anchor):
        raise ValueError("storage root must not be a filesystem root")
    return root / _AUTOMATION_SLUG / f"run-{run_id}" / "script" / "script.json"


def _write_script_bundle(
    destination: Path,
    *,
    input_data: ContentScriptInput,
    result: ScriptResult,
    model: str,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    payload = {
        "schema_version": 1,
        "topic": input_data.topic,
        "persona": input_data.persona,
        "format": input_data.format,
        "model": model,
        "facts": [fact.model_dump(mode="json") for fact in input_data.facts],
        "result": result.model_dump(mode="json"),
    }
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _persisted_id(value: int | None, kind: str) -> int:
    if not isinstance(value, int):
        raise ValueError(f"{kind} must be persisted")
    return value


def _result(
    run: Run,
    step_run: StepRun | None,
    artifact_id: int | None,
    *,
    approval_id: int | None,
    created: bool,
    replayed: bool,
) -> ContentScriptRunResult:
    return ContentScriptRunResult(
        automation_id=_persisted_id(run.automation_id, "automation"),
        run_id=_persisted_id(run.id, "run"),
        step_run_id=None if step_run is None else _persisted_id(step_run.id, "step"),
        artifact_id=artifact_id,
        approval_id=approval_id,
        run_status=run.status,
        step_status=None if step_run is None else step_run.status,
        created=created,
        replayed=replayed,
    )
