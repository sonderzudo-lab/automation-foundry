"""Deterministic Content Engine originality gate with no external model."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from src.operations.retention_policy import resolve_retention_days
from src.platform.alert_service import record_alert_occurrence
from src.platform.approval_service import approval_payload_digest
from src.platform.artifact_service import register_local_artifact
from src.platform.ledger_service import record_ledger_entry
from src.platform.metric_service import record_metric_point
from src.platform.models import (
    AlertSeverity,
    Approval,
    ApprovalStatus,
    Artifact,
    ArtifactSensitivity,
    Automation,
    LedgerEntryType,
    MetricKind,
    QueueClass,
    Run,
    RunStatus,
    StepRun,
)
from src.platform.run_service import transition_run
from src.platform.task_runner import (
    PermanentTaskError,
    RetryPolicy,
    Sleeper,
    TaskAttemptContext,
    TaskStepSpec,
    execute_task_step,
)

SIMILARITY_STEP_NAME = "check-originality-a6"
_SCRIPT_APPROVAL_ACTION = "review_content_script_a1"
_SOURCE = "content-engine:a6"
_ALGORITHM = "lexical-cosine-unigram+jaccard-trigram-v1"
_REPORT_SCHEMA_VERSION = 1
_MAX_SCRIPT_BYTES = 5 * 1024 * 1024
_RETENTION_DAYS = resolve_retention_days("originality_report")
_QUANTUM = Decimal("0.0000000001")
_TOKEN_PATTERN = re.compile(r"[^\W_]+", re.UNICODE)


@dataclass(frozen=True, slots=True)
class SimilarityScore:
    """Transparent lexical comparison between two scripts."""

    score: Decimal
    token_cosine: Decimal
    trigram_jaccard: Decimal


@dataclass(frozen=True, slots=True)
class SimilarityExecutionResult:
    """Observable identifiers and policy outcome produced by A6."""

    run_id: int
    step_run_id: int | None
    report_artifact_id: int | None
    run_status: str
    step_status: str | None
    max_similarity: Decimal | None
    reference_count: int
    blocked: bool
    replayed: bool
    output_payload: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class _Reference:
    run_id: int
    artifact: Artifact


@dataclass(frozen=True, slots=True)
class _Evaluation:
    max_similarity: Decimal
    mean_similarity: Decimal
    blocked: bool
    comparison_seconds: Decimal
    comparisons: tuple[dict[str, object], ...]


def calculate_script_similarity(current: str, reference: str) -> SimilarityScore:
    """Return a deterministic lexical score in the inclusive range 0..1."""
    current_tokens = _normalize_tokens(current)
    reference_tokens = _normalize_tokens(reference)
    if not current_tokens or not reference_tokens:
        raise ValueError("scripts must contain comparable text")
    token_cosine = _counter_cosine(Counter(current_tokens), Counter(reference_tokens))
    trigram_jaccard = _jaccard(
        _shingles(current_tokens, size=3),
        _shingles(reference_tokens, size=3),
    )
    score = max(token_cosine, trigram_jaccard)
    return SimilarityScore(
        score=_quantize(score),
        token_cosine=_quantize(token_cosine),
        trigram_jaccard=_quantize(trigram_jaccard),
    )


async def execute_similarity_gate_step(
    session: AsyncSession,
    *,
    run: Run,
    script_approval: Approval,
    approval_input_payload: dict[str, object],
    script_artifact: Artifact,
    final_video_artifact: Artifact,
    idempotency_key: str,
    storage_root: Path,
    threshold: Decimal,
    reference_limit: int,
    cpu_power_watts: int,
    timeout_seconds: float,
    finalize_run: bool = True,
    sleep: Sleeper = asyncio.sleep,
) -> SimilarityExecutionResult:
    """Evaluate A6 and fail the run closed when similarity reaches the threshold."""
    threshold = _validate_threshold(threshold)
    if not 1 <= reference_limit <= 100:
        raise ValueError("reference_limit must be between 1 and 100")
    if cpu_power_watts < 1:
        raise ValueError("cpu_power_watts must be positive")
    _validate_gate_inputs(
        run=run,
        script_approval=script_approval,
        approval_input_payload=approval_input_payload,
        script_artifact=script_artifact,
        final_video_artifact=final_video_artifact,
    )
    current_script = _read_script_bundle(storage_root, script_artifact)
    _verify_artifact_file(storage_root, final_video_artifact, load=False)
    references = await _load_references(
        session,
        run=run,
        limit=reference_limit,
    )
    output_path = _report_path(storage_root, run_id=_persisted_id(run.id, "run"))
    step_key = f"{idempotency_key}:{SIMILARITY_STEP_NAME}"

    async def evaluate_operation(_context: TaskAttemptContext) -> dict[str, object]:
        evaluation = await asyncio.to_thread(
            _evaluate_and_write_report,
            current_script,
            references,
            threshold,
            script_artifact,
            final_video_artifact,
            storage_root,
            output_path,
        )
        return {
            "artifact_relative_path": output_path.relative_to(
                storage_root.resolve()
            ).as_posix(),
            "sha256": _sha256_file(output_path),
            "algorithm": _ALGORITHM,
            "threshold": str(threshold),
            "max_similarity": str(evaluation.max_similarity),
            "mean_similarity": str(evaluation.mean_similarity),
            "reference_count": len(references),
            "blocked": evaluation.blocked,
            "comparison_seconds": str(evaluation.comparison_seconds),
        }

    task_result = await execute_task_step(
        session,
        run=run,
        spec=TaskStepSpec(
            name=SIMILARITY_STEP_NAME,
            queue=QueueClass.CPU,
            ordinal=6,
            idempotency_key=step_key,
            input_payload={
                "schema_version": 1,
                "script_artifact_id": script_artifact.id,
                "script_sha256": script_artifact.sha256,
                "final_video_artifact_id": final_video_artifact.id,
                "final_video_sha256": final_video_artifact.sha256,
                "algorithm": _ALGORITHM,
                "threshold": str(threshold),
                "reference_limit": reference_limit,
                "reference_run_ids": [reference.run_id for reference in references],
            },
        ),
        policy=RetryPolicy(max_attempts=1, timeout_seconds=timeout_seconds),
        operation=evaluate_operation,
        sleep=sleep,
    )
    step_run = await session.get(StepRun, task_result.step_run_ids[-1])
    if step_run is None:
        raise ValueError("similarity step result was not persisted")
    if task_result.status is RunStatus.CANCELLED:
        return _result(run, step_run, None, replayed=task_result.replayed)
    if task_result.status is RunStatus.FAILED:
        if task_result.error is None:
            raise ValueError("failed similarity step is missing structured error")
        if RunStatus(run.status) is RunStatus.RUNNING:
            await transition_run(
                session,
                run,
                RunStatus.FAILED,
                error=task_result.error,
                note="Content Engine A6 originality evaluation failed",
            )
        return _result(run, step_run, None, replayed=task_result.replayed)

    output = task_result.output_payload or {}
    blocked = _required_bool(output, "blocked")
    max_similarity = _decimal_output(output, "max_similarity")
    mean_similarity = _decimal_output(output, "mean_similarity")
    reference_count = _required_int(output, "reference_count", minimum=0)
    try:
        async with session.begin_nested():
            automation = await session.get(Automation, run.automation_id)
            if automation is None:
                raise ValueError("similarity automation does not exist")
            report = (
                await register_local_artifact(
                    session,
                    run=run,
                    step_run=step_run,
                    storage_root=storage_root,
                    file_path=_output_path(storage_root, output),
                    idempotency_key=f"{idempotency_key}:originality-report",
                    artifact_type="originality_report",
                    media_type="application/json",
                    origin=_SOURCE,
                    sensitivity=ArtifactSensitivity.INTERNAL,
                    retention_days=_RETENTION_DAYS,
                    expected_sha256=_required_text(output, "sha256", max_length=64),
                )
            ).artifact
            dimensions: dict[str, object] = {
                "algorithm": _ALGORITHM,
                "threshold": str(threshold),
                "scope": "automation",
            }
            max_metric = (
                await record_metric_point(
                    session,
                    automation=automation,
                    run=run,
                    step_run=step_run,
                    idempotency_key=f"{idempotency_key}:originality-max-similarity",
                    name="content_originality_max_similarity",
                    kind=MetricKind.RATIO,
                    value=max_similarity,
                    unit="ratio",
                    source=_SOURCE,
                    dimensions=dimensions,
                )
            ).metric_point
            mean_metric = (
                await record_metric_point(
                    session,
                    automation=automation,
                    run=run,
                    step_run=step_run,
                    idempotency_key=f"{idempotency_key}:originality-mean-similarity",
                    name="content_originality_mean_similarity",
                    kind=MetricKind.RATIO,
                    value=mean_similarity,
                    unit="ratio",
                    source=_SOURCE,
                    dimensions=dimensions,
                )
            ).metric_point
            count_metric = (
                await record_metric_point(
                    session,
                    automation=automation,
                    run=run,
                    step_run=step_run,
                    idempotency_key=f"{idempotency_key}:originality-reference-count",
                    name="content_originality_reference_count",
                    kind=MetricKind.COUNTER,
                    value=reference_count,
                    unit="scripts",
                    source=_SOURCE,
                    dimensions=dimensions,
                )
            ).metric_point
            comparison_seconds = _decimal_output(output, "comparison_seconds")
            duration_metric = (
                await record_metric_point(
                    session,
                    automation=automation,
                    run=run,
                    step_run=step_run,
                    idempotency_key=f"{idempotency_key}:originality-duration",
                    name="content_originality_evaluation_duration_seconds",
                    kind=MetricKind.DURATION,
                    value=comparison_seconds,
                    unit="seconds",
                    source=_SOURCE,
                    dimensions=dimensions,
                )
            ).metric_point
            energy = (comparison_seconds / Decimal("3600")) * (
                Decimal(cpu_power_watts) / Decimal("1000")
            )
            energy_metric = (
                await record_metric_point(
                    session,
                    automation=automation,
                    run=run,
                    step_run=step_run,
                    idempotency_key=f"{idempotency_key}:originality-energy-estimate",
                    name="content_originality_energy_estimate_kwh",
                    kind=MetricKind.GAUGE,
                    value=energy.quantize(_QUANTUM),
                    unit="kWh",
                    source=_SOURCE,
                    confidence="0.4",
                    dimensions=dimensions,
                )
            ).metric_point
            ledger = (
                await record_ledger_entry(
                    session,
                    automation=automation,
                    run=run,
                    step_run=step_run,
                    idempotency_key=f"{idempotency_key}:originality-external-api-cost",
                    entry_type=LedgerEntryType.COST,
                    category="external_api",
                    amount=0,
                    currency="BRL",
                    source=_SOURCE,
                    confidence=1,
                )
            ).ledger_entry
            alert_id: int | None = None
            if blocked:
                alert = (
                    await record_alert_occurrence(
                        session,
                        automation=automation,
                        run=run,
                        step_run=step_run,
                        metric_point=max_metric,
                        deduplication_key=f"content-originality:run:{run.id}",
                        idempotency_key=f"{idempotency_key}:originality-blocked",
                        title="Conteúdo bloqueado por similaridade alta",
                        summary=(
                            "O roteiro atingiu ou ultrapassou o limite local de "
                            "similaridade e não pode avançar automaticamente."
                        ),
                        severity=AlertSeverity.WARNING,
                        source=_SOURCE,
                    )
                ).alert
                alert_id = _persisted_id(alert.id, "alert")
    except Exception as exc:
        error = {
            "code": "CONTENT_ORIGINALITY_EVIDENCE_PERSIST_FAILED",
            "exception_type": type(exc).__name__,
            "retryable": True,
            "timed_out": False,
        }
        if RunStatus(run.status) is RunStatus.RUNNING:
            await transition_run(
                session,
                run,
                RunStatus.FAILED,
                error=error,
                note="Content Engine A6 evidence persistence failed",
            )
        return _result(run, step_run, None, replayed=task_result.replayed)

    output_payload: dict[str, object] = {
        "script_artifact_id": script_artifact.id,
        "script_approval_id": script_approval.id,
        "final_video_artifact_id": final_video_artifact.id,
        "similarity_step_run_id": step_run.id,
        "originality_report_artifact_id": report.id,
        "originality_max_metric_id": max_metric.id,
        "originality_mean_metric_id": mean_metric.id,
        "originality_reference_count_metric_id": count_metric.id,
        "originality_duration_metric_id": duration_metric.id,
        "originality_energy_metric_id": energy_metric.id,
        "originality_ledger_entry_id": ledger.id,
        "originality_alert_id": alert_id,
        "originality_blocked": blocked,
    }
    if RunStatus(run.status) is RunStatus.RUNNING:
        if blocked:
            await transition_run(
                session,
                run,
                RunStatus.FAILED,
                error={
                    "code": "CONTENT_ORIGINALITY_BLOCKED",
                    "retryable": False,
                    "timed_out": False,
                    "threshold": str(threshold),
                    "max_similarity": str(max_similarity),
                },
                note="Content Engine A6 blocked a highly similar script; nothing published",
            )
        elif finalize_run:
            await transition_run(
                session,
                run,
                RunStatus.SUCCEEDED,
                output_payload=output_payload,
                note="Content Engine A6 passed the local originality gate; nothing published",
            )
    return SimilarityExecutionResult(
        run_id=_persisted_id(run.id, "run"),
        step_run_id=_persisted_id(step_run.id, "step"),
        report_artifact_id=_persisted_id(report.id, "artifact"),
        run_status=run.status,
        step_status=step_run.status,
        max_similarity=max_similarity,
        reference_count=reference_count,
        blocked=blocked,
        replayed=task_result.replayed,
        output_payload=output_payload,
    )


async def _load_references(
    session: AsyncSession,
    *,
    run: Run,
    limit: int,
) -> tuple[_Reference, ...]:
    report_artifact = aliased(Artifact)
    rows = (
        await session.execute(
            select(Artifact, Run)
            .join(Run, Run.id == Artifact.run_id)
            .where(
                Run.automation_id == run.automation_id,
                Run.id != run.id,
                Run.status == RunStatus.SUCCEEDED.value,
                Artifact.artifact_type == "script_bundle",
                exists(
                    select(report_artifact.id).where(
                        report_artifact.run_id == Run.id,
                        report_artifact.artifact_type == "originality_report",
                    )
                ),
            )
            .order_by(Run.finished_at.desc(), Run.id.desc())
            .limit(limit)
        )
    ).all()
    references: list[_Reference] = []
    for artifact, historical_run in rows:
        references.append(
            _Reference(
                run_id=_persisted_id(historical_run.id, "historical run"),
                artifact=artifact,
            )
        )
    return tuple(references)


def _evaluate_and_write_report(
    current_script: str,
    references: tuple[_Reference, ...],
    threshold: Decimal,
    script_artifact: Artifact,
    final_video_artifact: Artifact,
    storage_root: Path,
    output_path: Path,
) -> _Evaluation:
    started = time.perf_counter()
    comparisons: list[dict[str, object]] = []
    scores: list[Decimal] = []
    for reference in references:
        try:
            reference_script = _read_script_bundle(storage_root, reference.artifact)
        except ValueError as exc:
            raise PermanentTaskError("CONTENT_ORIGINALITY_REFERENCE_INVALID") from exc
        result = calculate_script_similarity(current_script, reference_script)
        scores.append(result.score)
        comparisons.append(
            {
                "reference_run_id": reference.run_id,
                "reference_artifact_id": reference.artifact.id,
                "reference_sha256": reference.artifact.sha256,
                "score": str(result.score),
                "token_cosine": str(result.token_cosine),
                "trigram_jaccard": str(result.trigram_jaccard),
            }
        )
    max_similarity = max(scores, default=Decimal("0"))
    mean_similarity = (
        sum(scores, start=Decimal("0")) / Decimal(len(scores))
        if scores
        else Decimal("0")
    )
    elapsed = max(Decimal(str(time.perf_counter() - started)), _QUANTUM)
    evaluation = _Evaluation(
        max_similarity=_quantize(max_similarity),
        mean_similarity=_quantize(mean_similarity),
        blocked=max_similarity >= threshold,
        comparison_seconds=_quantize(elapsed),
        comparisons=tuple(comparisons),
    )
    payload = {
        "schema_version": _REPORT_SCHEMA_VERSION,
        "algorithm": _ALGORITHM,
        "scope": "automation",
        "script_artifact_id": script_artifact.id,
        "script_sha256": script_artifact.sha256,
        "final_video_artifact_id": final_video_artifact.id,
        "final_video_sha256": final_video_artifact.sha256,
        "threshold": str(threshold),
        "reference_count": len(references),
        "max_similarity": str(evaluation.max_similarity),
        "mean_similarity": str(evaluation.mean_similarity),
        "blocked": evaluation.blocked,
        "comparisons": list(evaluation.comparisons),
    }
    _write_json_atomic(output_path, payload)
    return evaluation


def _validate_gate_inputs(
    *,
    run: Run,
    script_approval: Approval,
    approval_input_payload: dict[str, object],
    script_artifact: Artifact,
    final_video_artifact: Artifact,
) -> None:
    run_id = _persisted_id(run.id, "run")
    if RunStatus(run.status) is not RunStatus.RUNNING:
        raise ValueError("similarity gate requires a running run")
    if script_approval.run_id != run_id:
        raise ValueError("script approval belongs to a different run")
    if script_approval.action != _SCRIPT_APPROVAL_ACTION:
        raise ValueError("similarity gate requires the A1 editorial approval")
    if ApprovalStatus(script_approval.status) is not ApprovalStatus.APPROVED:
        raise ValueError("similarity gate requires an approved A1 script")
    if script_approval.payload_digest != approval_payload_digest(approval_input_payload):
        raise ValueError("script approval payload does not match the protected script")
    if script_artifact.run_id != run_id or script_artifact.artifact_type != "script_bundle":
        raise ValueError("similarity gate requires this run's script bundle")
    if approval_input_payload.get("artifact_id") != script_artifact.id:
        raise ValueError("approval input does not identify the script artifact")
    if approval_input_payload.get("artifact_sha256") != script_artifact.sha256:
        raise ValueError("approval input does not match the script checksum")
    if (
        final_video_artifact.run_id != run_id
        or final_video_artifact.artifact_type != "final_video"
        or final_video_artifact.step_run_id is None
    ):
        raise ValueError("similarity gate requires this run's assembled video")


def _read_script_bundle(storage_root: Path, artifact: Artifact) -> str:
    encoded = _verify_artifact_file(
        storage_root,
        artifact,
        load=True,
        maximum_bytes=_MAX_SCRIPT_BYTES,
    )
    assert encoded is not None
    try:
        payload = json.loads(encoded.decode("utf-8"))
        result = payload.get("result") if isinstance(payload, dict) else None
        script = result.get("full_script") if isinstance(result, dict) else None
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("script bundle is not valid UTF-8 JSON") from exc
    if payload.get("schema_version") != 1 or not isinstance(script, str) or not script.strip():
        raise ValueError("script bundle does not contain a supported full script")
    return script


def _verify_artifact_file(
    storage_root: Path,
    artifact: Artifact,
    *,
    load: bool,
    maximum_bytes: int | None = None,
) -> bytes | None:
    root = storage_root.resolve(strict=True)
    candidate = (root / artifact.relative_path).resolve(strict=True)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("artifact resolves outside storage root") from exc
    if not candidate.is_file():
        raise ValueError("artifact is not a regular file")
    if maximum_bytes is not None and artifact.size_bytes > maximum_bytes:
        raise ValueError("artifact exceeds the comparison size limit")
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    size = 0
    with candidate.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
            if load:
                chunks.append(chunk)
    if size != artifact.size_bytes or digest.hexdigest() != artifact.sha256:
        raise ValueError("artifact does not match durable size and checksum")
    return b"".join(chunks) if load else None


def _normalize_tokens(value: str) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return tuple(_TOKEN_PATTERN.findall(normalized))


def _counter_cosine(left: Counter[str], right: Counter[str]) -> Decimal:
    numerator = sum(left[token] * right.get(token, 0) for token in left)
    left_norm = math.sqrt(sum(count * count for count in left.values()))
    right_norm = math.sqrt(sum(count * count for count in right.values()))
    if left_norm == 0 or right_norm == 0:
        return Decimal("0")
    return Decimal(str(numerator / (left_norm * right_norm)))


def _shingles(tokens: tuple[str, ...], *, size: int) -> frozenset[tuple[str, ...]]:
    if len(tokens) < size:
        return frozenset({tokens}) if tokens else frozenset()
    return frozenset(tuple(tokens[index : index + size]) for index in range(len(tokens) - size + 1))


def _jaccard(left: frozenset[tuple[str, ...]], right: frozenset[tuple[str, ...]]) -> Decimal:
    union = left | right
    if not union:
        return Decimal("0")
    return Decimal(len(left & right)) / Decimal(len(union))


def _quantize(value: Decimal) -> Decimal:
    bounded = min(Decimal("1"), max(Decimal("0"), value))
    return bounded.quantize(_QUANTUM)


def _validate_threshold(value: Decimal) -> Decimal:
    threshold = Decimal(value).quantize(_QUANTUM)
    if not Decimal("0") < threshold <= Decimal("1"):
        raise ValueError("threshold must be greater than zero and at most one")
    return threshold


def _report_path(storage_root: Path, *, run_id: int) -> Path:
    root = storage_root.resolve()
    if root == Path(root.anchor):
        raise ValueError("storage root must not be a filesystem root")
    return root / "content-engine" / f"run-{run_id}" / "originality" / "report.json"


def _write_json_atomic(destination: Path, payload: dict[str, object]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _output_path(storage_root: Path, output: dict[str, object]) -> Path:
    relative = _required_text(output, "artifact_relative_path", max_length=1000)
    root = storage_root.resolve(strict=True)
    candidate = (root / relative).resolve(strict=True)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("similarity output resolves outside storage root") from exc
    return candidate


def _required_text(payload: dict[str, object], field: str, *, max_length: int) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise ValueError(f"{field} is missing or invalid")
    return value.strip()


def _required_int(payload: dict[str, object], field: str, *, minimum: int) -> int:
    value = payload.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{field} is missing or invalid")
    return value


def _required_bool(payload: dict[str, object], field: str) -> bool:
    value = payload.get(field)
    if not isinstance(value, bool):
        raise ValueError(f"{field} is missing or invalid")
    return value


def _decimal_output(payload: dict[str, object], field: str) -> Decimal:
    value = payload.get(field)
    try:
        parsed = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"{field} is missing or invalid") from exc
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{field} is missing or invalid")
    return parsed


def _persisted_id(value: int | None, kind: str) -> int:
    if not isinstance(value, int):
        raise ValueError(f"{kind} must be persisted")
    return value


def _result(
    run: Run,
    step_run: StepRun | None,
    report_artifact_id: int | None,
    *,
    replayed: bool,
) -> SimilarityExecutionResult:
    return SimilarityExecutionResult(
        run_id=_persisted_id(run.id, "run"),
        step_run_id=None if step_run is None else _persisted_id(step_run.id, "step"),
        report_artifact_id=report_artifact_id,
        run_status=run.status,
        step_status=None if step_run is None else step_run.status,
        max_similarity=None,
        reference_count=0,
        blocked=False,
        replayed=replayed,
    )
