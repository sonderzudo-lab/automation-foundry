"""Auditable local diagnostic for Content Engine A4 caption alignment.

This module never contacts a network service, never transitions a run and never
creates, changes or deletes pipeline media. It reads the exact `narration_audio`
WAV and `caption_ass` file already registered for one run, verifies both against
their durable size and SHA-256, measures where the audio actually carries energy
and compares that with the published caption intervals. The result is one
canonical JSON report registered as an artifact plus allowlisted metric points.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sys
import wave
from array import array
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import Settings, settings
from src.operations.retention_policy import resolve_retention_days
from src.platform.alert_service import record_alert_occurrence
from src.platform.artifact_service import register_local_artifact
from src.platform.metric_service import record_metric_point
from src.platform.models import (
    AlertSeverity,
    Artifact,
    ArtifactSensitivity,
    Automation,
    MetricKind,
    Run,
)

# The diagnostic only accepts runs of the Content Engine automation. The slug is
# duplicated instead of imported to keep this module free of the A1 import chain.
_AUTOMATION_SLUG = "content-engine"
_SOURCE = "content-engine:a4-alignment"
_ALGORITHM = "rms-window-energy-vs-ass-events-v1"
_REPORT_SCHEMA_VERSION = 1
_RETENTION_DAYS = resolve_retention_days("caption_alignment_report")
_CAPTION_ARTIFACT_TYPE = "caption_ass"
_AUDIO_ARTIFACT_TYPE = "narration_audio"
_MAX_CAPTION_BYTES = 5 * 1024 * 1024
_MAX_AUDIO_BYTES = 64 * 1024 * 1024
_MAX_EVENTS = 100_000
_SILENT_DBFS = Decimal("-120")
_FULL_SCALE = 32_768.0
_SECOND_QUANTUM = Decimal("0.000001")
_RATIO_QUANTUM = Decimal("0.0000000001")
_DB_QUANTUM = Decimal("0.01")
_ASS_TIME = re.compile(r"^(\d{1,2}):([0-5]\d):([0-5]\d)\.(\d{2})$")

STATUS_PASS = "pass"
STATUS_WARNING = "warning"

REASON_LOW_SPEECH_COVERAGE = "LOW_SPEECH_COVERAGE"
REASON_CAPTIONS_OVER_SILENCE = "CAPTIONS_OVER_SILENCE"
REASON_ONSET_OFFSET = "ONSET_OFFSET"
REASON_END_OFFSET = "END_OFFSET"
REASON_BLOCK_ONSET_OFFSET = "BLOCK_ONSET_OFFSET"
REASON_EVENTS_BEYOND_AUDIO = "EVENTS_BEYOND_AUDIO"

_KNOWN_REASONS = frozenset(
    {
        REASON_LOW_SPEECH_COVERAGE,
        REASON_CAPTIONS_OVER_SILENCE,
        REASON_ONSET_OFFSET,
        REASON_END_OFFSET,
        REASON_BLOCK_ONSET_OFFSET,
        REASON_EVENTS_BEYOND_AUDIO,
    }
)
_MAX_REPORT_BYTES = 1024 * 1024


class CaptionAlignmentError(ValueError):
    """Raised when the diagnostic cannot produce trustworthy local evidence."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class CaptionAlignmentParameters:
    """Deterministic detection thresholds recorded inside every report."""

    min_speech_coverage: Decimal = Decimal("0.90")
    max_outside_speech: Decimal = Decimal("0.25")
    boundary_tolerance_seconds: Decimal = Decimal("0.50")
    window_milliseconds: int = 20
    relative_drop_db: Decimal = Decimal("30")
    absolute_floor_dbfs: Decimal = Decimal("-55")
    merge_gap_seconds: Decimal = Decimal("0.30")
    min_speech_seconds: Decimal = Decimal("0.12")

    def __post_init__(self) -> None:
        if not Decimal("0") < self.min_speech_coverage <= Decimal("1"):
            raise ValueError("min_speech_coverage must be in (0, 1]")
        if not Decimal("0") <= self.max_outside_speech <= Decimal("1"):
            raise ValueError("max_outside_speech must be in [0, 1]")
        if not Decimal("0") < self.boundary_tolerance_seconds <= Decimal("30"):
            raise ValueError("boundary_tolerance_seconds must be in (0, 30]")
        if not 5 <= self.window_milliseconds <= 200:
            raise ValueError("window_milliseconds must be between 5 and 200")
        if not Decimal("6") <= self.relative_drop_db <= Decimal("90"):
            raise ValueError("relative_drop_db must be between 6 and 90")
        if not Decimal("-90") <= self.absolute_floor_dbfs <= Decimal("-10"):
            raise ValueError("absolute_floor_dbfs must be between -90 and -10")
        if not Decimal("0") <= self.merge_gap_seconds <= Decimal("5"):
            raise ValueError("merge_gap_seconds must be in [0, 5]")
        if not Decimal("0") <= self.min_speech_seconds <= Decimal("5"):
            raise ValueError("min_speech_seconds must be in [0, 5]")

    def as_payload(self) -> dict[str, object]:
        """Return the canonical, JSON-safe projection stored in the report."""
        return {
            "min_speech_coverage": str(self.min_speech_coverage),
            "max_outside_speech": str(self.max_outside_speech),
            "boundary_tolerance_seconds": str(self.boundary_tolerance_seconds),
            "window_milliseconds": self.window_milliseconds,
            "relative_drop_db": str(self.relative_drop_db),
            "absolute_floor_dbfs": str(self.absolute_floor_dbfs),
            "merge_gap_seconds": str(self.merge_gap_seconds),
            "min_speech_seconds": str(self.min_speech_seconds),
        }


@dataclass(frozen=True, slots=True)
class CaptionAlignmentMeasurement:
    """Deterministic comparison between measured speech and caption events."""

    audio_duration_seconds: Decimal
    speech_seconds: Decimal
    caption_seconds: Decimal
    speech_covered_seconds: Decimal
    caption_outside_speech_seconds: Decimal
    speech_coverage_ratio: Decimal
    caption_outside_speech_ratio: Decimal
    speech_segment_count: int
    caption_block_count: int
    caption_event_count: int
    events_beyond_audio: int
    onset_offset_seconds: Decimal
    end_offset_seconds: Decimal
    max_block_onset_offset_seconds: Decimal | None
    peak_dbfs: Decimal
    threshold_dbfs: Decimal
    status: str
    reasons: tuple[str, ...]

    def as_payload(self) -> dict[str, object]:
        """Return the canonical, JSON-safe projection stored in the report."""
        return {
            "audio_duration_seconds": str(self.audio_duration_seconds),
            "speech_seconds": str(self.speech_seconds),
            "caption_seconds": str(self.caption_seconds),
            "speech_covered_seconds": str(self.speech_covered_seconds),
            "caption_outside_speech_seconds": str(self.caption_outside_speech_seconds),
            "speech_coverage_ratio": str(self.speech_coverage_ratio),
            "caption_outside_speech_ratio": str(self.caption_outside_speech_ratio),
            "speech_segment_count": self.speech_segment_count,
            "caption_block_count": self.caption_block_count,
            "caption_event_count": self.caption_event_count,
            "events_beyond_audio": self.events_beyond_audio,
            "onset_offset_seconds": str(self.onset_offset_seconds),
            "end_offset_seconds": str(self.end_offset_seconds),
            "max_block_onset_offset_seconds": (
                None
                if self.max_block_onset_offset_seconds is None
                else str(self.max_block_onset_offset_seconds)
            ),
            "peak_dbfs": str(self.peak_dbfs),
            "threshold_dbfs": str(self.threshold_dbfs),
            "status": self.status,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class CaptionAlignmentReportView:
    """Allowlisted projection of one report re-verified before being rendered."""

    run_id: int
    report_artifact_id: int
    report_sha256: str
    caption_artifact_id: int
    audio_artifact_id: int
    parameters_digest: str
    status: str
    reasons: tuple[str, ...]
    audio_duration_seconds: Decimal
    speech_seconds: Decimal
    caption_seconds: Decimal
    speech_coverage_ratio: Decimal
    caption_outside_speech_ratio: Decimal
    onset_offset_seconds: Decimal
    end_offset_seconds: Decimal
    speech_segment_count: int
    caption_block_count: int
    caption_event_count: int
    events_beyond_audio: int


@dataclass(frozen=True, slots=True)
class CaptionAlignmentResult:
    """Observable identifiers produced by one diagnostic execution or replay."""

    run_id: int
    caption_artifact_id: int
    audio_artifact_id: int
    report_artifact_id: int
    report_sha256: str
    parameters_digest: str
    measurement: CaptionAlignmentMeasurement
    alert_id: int | None
    replayed: bool


def build_alignment_parameters(configuration: Settings) -> CaptionAlignmentParameters:
    """Translate validated settings into immutable diagnostic parameters."""
    return CaptionAlignmentParameters(
        min_speech_coverage=Decimal(
            str(configuration.content_caption_alignment_min_speech_coverage)
        ),
        max_outside_speech=Decimal(
            str(configuration.content_caption_alignment_max_outside_speech)
        ),
        boundary_tolerance_seconds=Decimal(
            str(configuration.content_caption_alignment_tolerance_seconds)
        ),
    )


def measure_caption_alignment(
    *,
    audio_path: Path,
    caption_text: str,
    parameters: CaptionAlignmentParameters,
) -> CaptionAlignmentMeasurement:
    """Measure caption coverage against the energy actually present in the WAV."""
    profile = _speech_profile(audio_path, parameters)
    duration = profile.duration_seconds
    speech = profile.intervals
    events = _parse_dialogue_events(caption_text)
    captions = _merge_intervals(events, gap=Decimal("0"))
    blocks = _merge_intervals(events, gap=parameters.merge_gap_seconds)
    speech_total = _total(speech)
    caption_total = _total(captions)
    covered = _intersection_total(speech, captions)
    outside = caption_total - covered
    coverage_ratio = _ratio(covered, speech_total)
    outside_ratio = _ratio(outside, caption_total)
    events_beyond_audio = sum(1 for _start, end in events if end > duration)
    onset_offset = (
        captions[0][0] - speech[0][0] if speech and captions else Decimal("0")
    )
    end_offset = (
        captions[-1][1] - speech[-1][1] if speech and captions else Decimal("0")
    )
    block_offset: Decimal | None = None
    if speech and len(blocks) == len(speech):
        block_offset = max(
            (block[0] - segment[0]).copy_abs()
            for block, segment in zip(blocks, speech, strict=True)
        )
    tolerance = parameters.boundary_tolerance_seconds
    reasons: list[str] = []
    if speech and coverage_ratio < parameters.min_speech_coverage:
        reasons.append(REASON_LOW_SPEECH_COVERAGE)
    if captions and outside_ratio > parameters.max_outside_speech:
        reasons.append(REASON_CAPTIONS_OVER_SILENCE)
    if onset_offset.copy_abs() > tolerance:
        reasons.append(REASON_ONSET_OFFSET)
    if end_offset.copy_abs() > tolerance:
        reasons.append(REASON_END_OFFSET)
    if block_offset is not None and block_offset > tolerance:
        reasons.append(REASON_BLOCK_ONSET_OFFSET)
    if events_beyond_audio:
        reasons.append(REASON_EVENTS_BEYOND_AUDIO)
    return CaptionAlignmentMeasurement(
        audio_duration_seconds=_seconds(duration),
        speech_seconds=_seconds(speech_total),
        caption_seconds=_seconds(caption_total),
        speech_covered_seconds=_seconds(covered),
        caption_outside_speech_seconds=_seconds(outside),
        speech_coverage_ratio=coverage_ratio,
        caption_outside_speech_ratio=outside_ratio,
        speech_segment_count=len(speech),
        caption_block_count=len(blocks),
        caption_event_count=len(events),
        events_beyond_audio=events_beyond_audio,
        onset_offset_seconds=_seconds(onset_offset),
        end_offset_seconds=_seconds(end_offset),
        max_block_onset_offset_seconds=(
            None if block_offset is None else _seconds(block_offset)
        ),
        peak_dbfs=profile.peak_dbfs,
        threshold_dbfs=profile.threshold_dbfs,
        status=STATUS_WARNING if reasons else STATUS_PASS,
        reasons=tuple(reasons),
    )


async def evaluate_caption_alignment(
    session: AsyncSession,
    *,
    run_id: int,
    storage_root: Path,
    parameters: CaptionAlignmentParameters,
) -> CaptionAlignmentResult:
    """Publish one auditable alignment report without changing the run state."""
    run = await session.get(Run, run_id)
    if run is None:
        raise CaptionAlignmentError("CAPTION_ALIGNMENT_RUN_NOT_FOUND")
    automation = await session.get(Automation, run.automation_id)
    if automation is None or automation.slug != _AUTOMATION_SLUG:
        raise CaptionAlignmentError("CAPTION_ALIGNMENT_RUN_NOT_ELIGIBLE")
    caption_artifact = await _required_artifact(
        session,
        run_id=run_id,
        artifact_type=_CAPTION_ARTIFACT_TYPE,
        code="CAPTION_ALIGNMENT_CAPTION_ARTIFACT_MISSING",
    )
    audio_artifact = await _required_artifact(
        session,
        run_id=run_id,
        artifact_type=_AUDIO_ARTIFACT_TYPE,
        code="CAPTION_ALIGNMENT_AUDIO_ARTIFACT_MISSING",
    )
    _caption_path, caption_bytes = _verified_file(
        storage_root,
        caption_artifact,
        load=True,
        maximum_bytes=_MAX_CAPTION_BYTES,
    )
    audio_path, _audio_bytes = _verified_file(
        storage_root,
        audio_artifact,
        load=False,
        maximum_bytes=_MAX_AUDIO_BYTES,
    )
    try:
        caption_text = caption_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CaptionAlignmentError("CAPTION_ALIGNMENT_CAPTION_INVALID") from exc
    measurement = measure_caption_alignment(
        audio_path=audio_path,
        caption_text=caption_text,
        parameters=parameters,
    )
    digest = _parameters_digest(
        parameters,
        caption_sha256=caption_artifact.sha256,
        audio_sha256=audio_artifact.sha256,
    )
    report_payload = _report_payload(
        run_id=run_id,
        caption_artifact=caption_artifact,
        audio_artifact=audio_artifact,
        parameters=parameters,
        parameters_digest=digest,
        measurement=measurement,
    )
    report_path = _report_path(storage_root, run_id=run_id, digest=digest)
    _write_json_atomic(report_path, report_payload)
    idempotency_key = f"run:{run_id}:caption-alignment:{digest}"
    try:
        async with session.begin_nested():
            registration = await register_local_artifact(
                session,
                run=run,
                storage_root=storage_root,
                file_path=report_path,
                idempotency_key=f"{idempotency_key}:report",
                artifact_type="caption_alignment_report",
                media_type="application/json",
                origin=_SOURCE,
                sensitivity=ArtifactSensitivity.INTERNAL,
                retention_days=_RETENTION_DAYS,
                expected_sha256=_sha256_file(report_path),
            )
            dimensions: dict[str, object] = {
                "algorithm": _ALGORITHM,
                "scope": "run",
                "parameters_digest": digest,
            }
            coverage_metric = (
                await record_metric_point(
                    session,
                    automation=automation,
                    run=run,
                    idempotency_key=f"{idempotency_key}:speech-coverage",
                    name="content_caption_alignment_speech_coverage_ratio",
                    kind=MetricKind.RATIO,
                    value=measurement.speech_coverage_ratio,
                    unit="ratio",
                    source=_SOURCE,
                    confidence="0.5",
                    dimensions=dimensions,
                )
            ).metric_point
            await record_metric_point(
                session,
                automation=automation,
                run=run,
                idempotency_key=f"{idempotency_key}:outside-speech",
                name="content_caption_alignment_outside_speech_ratio",
                kind=MetricKind.RATIO,
                value=measurement.caption_outside_speech_ratio,
                unit="ratio",
                source=_SOURCE,
                confidence="0.5",
                dimensions=dimensions,
            )
            await record_metric_point(
                session,
                automation=automation,
                run=run,
                idempotency_key=f"{idempotency_key}:onset-offset",
                name="content_caption_alignment_onset_offset_seconds",
                kind=MetricKind.GAUGE,
                value=measurement.onset_offset_seconds,
                unit="seconds",
                source=_SOURCE,
                confidence="0.5",
                dimensions=dimensions,
            )
            await record_metric_point(
                session,
                automation=automation,
                run=run,
                idempotency_key=f"{idempotency_key}:end-offset",
                name="content_caption_alignment_end_offset_seconds",
                kind=MetricKind.GAUGE,
                value=measurement.end_offset_seconds,
                unit="seconds",
                source=_SOURCE,
                confidence="0.5",
                dimensions=dimensions,
            )
            await record_metric_point(
                session,
                automation=automation,
                run=run,
                idempotency_key=f"{idempotency_key}:speech-segments",
                name="content_caption_alignment_speech_segment_count",
                kind=MetricKind.COUNTER,
                value=measurement.speech_segment_count,
                unit="segments",
                source=_SOURCE,
                dimensions=dimensions,
            )
            alert_id: int | None = None
            if measurement.status == STATUS_WARNING:
                alert = (
                    await record_alert_occurrence(
                        session,
                        automation=automation,
                        run=run,
                        metric_point=coverage_metric,
                        deduplication_key=f"content-caption-alignment:run:{run_id}",
                        idempotency_key=f"{idempotency_key}:misaligned",
                        title="Legenda possivelmente desalinhada com o áudio",
                        summary=(
                            "O diagnóstico local comparou a energia do WAV aprovado "
                            "com os eventos da legenda e encontrou divergência acima "
                            "da tolerância configurada. Nada foi publicado."
                        ),
                        severity=AlertSeverity.WARNING,
                        source=_SOURCE,
                    )
                ).alert
                alert_id = _persisted_id(alert.id, "alert")
    except CaptionAlignmentError:
        raise
    except Exception as exc:
        raise CaptionAlignmentError(
            "CAPTION_ALIGNMENT_EVIDENCE_PERSIST_FAILED"
        ) from exc
    return CaptionAlignmentResult(
        run_id=run_id,
        caption_artifact_id=_persisted_id(caption_artifact.id, "artifact"),
        audio_artifact_id=_persisted_id(audio_artifact.id, "artifact"),
        report_artifact_id=_persisted_id(registration.artifact.id, "artifact"),
        report_sha256=registration.artifact.sha256,
        parameters_digest=digest,
        measurement=measurement,
        alert_id=alert_id,
        replayed=not registration.created,
    )


async def load_caption_alignment_report(
    session: AsyncSession,
    *,
    run_id: int,
    storage_root: Path | None = None,
) -> CaptionAlignmentReportView | None:
    """Rebuild the allowlisted projection of one verified report, or None."""
    root = Path(settings.storage_root) if storage_root is None else storage_root
    artifact = await session.scalar(
        select(Artifact)
        .where(
            Artifact.run_id == run_id,
            Artifact.artifact_type == "caption_alignment_report",
        )
        .order_by(Artifact.id.desc())
        .limit(1)
    )
    if artifact is None:
        return None
    _path, encoded = _verified_file(
        root,
        artifact,
        load=True,
        maximum_bytes=_MAX_REPORT_BYTES,
    )
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CaptionAlignmentError("CAPTION_ALIGNMENT_REPORT_UNVERIFIED") from exc
    if not isinstance(payload, dict):
        raise CaptionAlignmentError("CAPTION_ALIGNMENT_REPORT_UNVERIFIED")
    measurement = payload.get("measurement")
    if (
        payload.get("schema_version") != _REPORT_SCHEMA_VERSION
        or payload.get("algorithm") != _ALGORITHM
        or payload.get("run_id") != run_id
        or payload.get("published") is not False
        or payload.get("external_service_consulted") is not False
        or not isinstance(measurement, dict)
    ):
        raise CaptionAlignmentError("CAPTION_ALIGNMENT_REPORT_UNVERIFIED")
    status = measurement.get("status")
    if status not in {STATUS_PASS, STATUS_WARNING}:
        raise CaptionAlignmentError("CAPTION_ALIGNMENT_REPORT_UNVERIFIED")
    raw_reasons = measurement.get("reasons")
    if not isinstance(raw_reasons, list) or not set(raw_reasons) <= _KNOWN_REASONS:
        raise CaptionAlignmentError("CAPTION_ALIGNMENT_REPORT_UNVERIFIED")
    return CaptionAlignmentReportView(
        run_id=run_id,
        report_artifact_id=_persisted_id(artifact.id, "artifact"),
        report_sha256=artifact.sha256,
        caption_artifact_id=_reported_int(payload, "caption_artifact_id"),
        audio_artifact_id=_reported_int(payload, "audio_artifact_id"),
        parameters_digest=_reported_digest(payload),
        status=str(status),
        reasons=tuple(str(reason) for reason in raw_reasons),
        audio_duration_seconds=_reported_decimal(measurement, "audio_duration_seconds"),
        speech_seconds=_reported_decimal(measurement, "speech_seconds"),
        caption_seconds=_reported_decimal(measurement, "caption_seconds"),
        speech_coverage_ratio=_reported_decimal(measurement, "speech_coverage_ratio"),
        caption_outside_speech_ratio=_reported_decimal(
            measurement,
            "caption_outside_speech_ratio",
        ),
        onset_offset_seconds=_reported_decimal(measurement, "onset_offset_seconds"),
        end_offset_seconds=_reported_decimal(measurement, "end_offset_seconds"),
        speech_segment_count=_reported_int(measurement, "speech_segment_count"),
        caption_block_count=_reported_int(measurement, "caption_block_count"),
        caption_event_count=_reported_int(measurement, "caption_event_count"),
        events_beyond_audio=_reported_int(measurement, "events_beyond_audio"),
    )


def _reported_int(payload: dict[str, object], field: str) -> int:
    value = payload.get(field)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise CaptionAlignmentError("CAPTION_ALIGNMENT_REPORT_UNVERIFIED")
    return value


def _reported_decimal(payload: dict[str, object], field: str) -> Decimal:
    value = payload.get(field)
    if not isinstance(value, str):
        raise CaptionAlignmentError("CAPTION_ALIGNMENT_REPORT_UNVERIFIED")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise CaptionAlignmentError("CAPTION_ALIGNMENT_REPORT_UNVERIFIED") from exc
    if not parsed.is_finite():
        raise CaptionAlignmentError("CAPTION_ALIGNMENT_REPORT_UNVERIFIED")
    return parsed


def _reported_digest(payload: dict[str, object]) -> str:
    value = payload.get("parameters_digest")
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{16}", value):
        raise CaptionAlignmentError("CAPTION_ALIGNMENT_REPORT_UNVERIFIED")
    return value


async def _required_artifact(
    session: AsyncSession,
    *,
    run_id: int,
    artifact_type: str,
    code: str,
) -> Artifact:
    artifact = await session.scalar(
        select(Artifact)
        .where(Artifact.run_id == run_id, Artifact.artifact_type == artifact_type)
        .order_by(Artifact.id.desc())
        .limit(1)
    )
    if artifact is None:
        raise CaptionAlignmentError(code)
    return artifact


def _verified_file(
    storage_root: Path,
    artifact: Artifact,
    *,
    load: bool,
    maximum_bytes: int,
) -> tuple[Path, bytes]:
    try:
        root = storage_root.resolve(strict=True)
        candidate = (root / artifact.relative_path).resolve(strict=True)
        candidate.relative_to(root)
        if not candidate.is_file():
            raise OSError("not a regular file")
        if artifact.size_bytes > maximum_bytes:
            raise CaptionAlignmentError("CAPTION_ALIGNMENT_EVIDENCE_TOO_LARGE")
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        size = 0
        with candidate.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
                if load:
                    chunks.append(chunk)
    except CaptionAlignmentError:
        raise
    except (OSError, ValueError) as exc:
        raise CaptionAlignmentError("CAPTION_ALIGNMENT_EVIDENCE_UNVERIFIED") from exc
    if size != artifact.size_bytes or digest.hexdigest() != artifact.sha256:
        raise CaptionAlignmentError("CAPTION_ALIGNMENT_EVIDENCE_UNVERIFIED")
    return candidate, b"".join(chunks)


@dataclass(frozen=True, slots=True)
class _SpeechProfile:
    """Single-pass energy profile of one verified WAV file."""

    duration_seconds: Decimal
    peak_dbfs: Decimal
    threshold_dbfs: Decimal
    intervals: tuple[tuple[Decimal, Decimal], ...]


def _window_energies(
    audio_path: Path,
    parameters: CaptionAlignmentParameters,
) -> tuple[Decimal, Decimal, tuple[float, ...]]:
    try:
        with wave.open(str(audio_path), "rb") as audio:
            framerate = audio.getframerate()
            frame_count = audio.getnframes()
            if (
                audio.getnchannels() != 1
                or audio.getsampwidth() != 2
                or audio.getcomptype() != "NONE"
                or framerate < 8_000
                or frame_count < 1
            ):
                raise CaptionAlignmentError("CAPTION_ALIGNMENT_AUDIO_INVALID")
            window_frames = max(1, (framerate * parameters.window_milliseconds) // 1000)
            energies: list[float] = []
            while raw := audio.readframes(window_frames):
                samples = array("h")
                samples.frombytes(raw)
                if sys.byteorder != "little":
                    samples.byteswap()
                if not samples:
                    break
                total = sum(float(sample) * float(sample) for sample in samples)
                energies.append(math.sqrt(total / len(samples)))
    except CaptionAlignmentError:
        raise
    except (OSError, EOFError, ValueError, wave.Error) as exc:
        raise CaptionAlignmentError("CAPTION_ALIGNMENT_AUDIO_INVALID") from exc
    if not energies:
        raise CaptionAlignmentError("CAPTION_ALIGNMENT_AUDIO_INVALID")
    duration = Decimal(frame_count) / Decimal(framerate)
    window_seconds = Decimal(window_frames) / Decimal(framerate)
    return duration, window_seconds, tuple(energies)


def _dbfs(rms: float) -> Decimal:
    if rms <= 0:
        return _SILENT_DBFS
    value = Decimal(str(20.0 * math.log10(rms / _FULL_SCALE)))
    return max(_SILENT_DBFS, value).quantize(_DB_QUANTUM)


def _speech_profile(
    audio_path: Path,
    parameters: CaptionAlignmentParameters,
) -> _SpeechProfile:
    duration, window_seconds, energies = _window_energies(audio_path, parameters)
    peak = _dbfs(max(energies))
    threshold = max(peak - parameters.relative_drop_db, parameters.absolute_floor_dbfs)
    voiced: list[tuple[Decimal, Decimal]] = []
    for index, rms in enumerate(energies):
        if _dbfs(rms) < threshold:
            continue
        start = min(duration, window_seconds * Decimal(index))
        end = min(duration, start + window_seconds)
        if end > start:
            voiced.append((start, end))
    merged = _merge_intervals(tuple(voiced), gap=parameters.merge_gap_seconds)
    speech = tuple(
        interval
        for interval in merged
        if interval[1] - interval[0] >= parameters.min_speech_seconds
    )
    return _SpeechProfile(
        duration_seconds=duration,
        peak_dbfs=peak,
        threshold_dbfs=threshold,
        intervals=speech,
    )


def _parse_dialogue_events(caption_text: str) -> tuple[tuple[Decimal, Decimal], ...]:
    events: list[tuple[Decimal, Decimal]] = []
    in_events = False
    for line in caption_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_events = stripped.casefold() == "[events]"
            continue
        if not in_events or not stripped.startswith("Dialogue:"):
            continue
        fields = stripped[len("Dialogue:") :].split(",", 9)
        if len(fields) < 10:
            raise CaptionAlignmentError("CAPTION_ALIGNMENT_CAPTION_INVALID")
        start = _parse_ass_time(fields[1])
        end = _parse_ass_time(fields[2])
        if end <= start:
            raise CaptionAlignmentError("CAPTION_ALIGNMENT_CAPTION_INVALID")
        events.append((start, end))
        if len(events) > _MAX_EVENTS:
            raise CaptionAlignmentError("CAPTION_ALIGNMENT_CAPTION_INVALID")
    if not events:
        raise CaptionAlignmentError("CAPTION_ALIGNMENT_CAPTION_INVALID")
    return tuple(sorted(events))


def _parse_ass_time(value: str) -> Decimal:
    match = _ASS_TIME.match(value.strip())
    if match is None:
        raise CaptionAlignmentError("CAPTION_ALIGNMENT_CAPTION_INVALID")
    hours, minutes, seconds, centiseconds = (int(part) for part in match.groups())
    return (
        Decimal(hours) * Decimal(3600)
        + Decimal(minutes) * Decimal(60)
        + Decimal(seconds)
        + Decimal(centiseconds) / Decimal(100)
    )


def _merge_intervals(
    intervals: tuple[tuple[Decimal, Decimal], ...],
    *,
    gap: Decimal,
) -> tuple[tuple[Decimal, Decimal], ...]:
    if not intervals:
        return ()
    merged: list[tuple[Decimal, Decimal]] = []
    for start, end in sorted(intervals):
        if merged and start - merged[-1][1] <= gap:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
            continue
        merged.append((start, end))
    return tuple(merged)


def _total(intervals: tuple[tuple[Decimal, Decimal], ...]) -> Decimal:
    return sum((end - start for start, end in intervals), start=Decimal("0"))


def _intersection_total(
    left: tuple[tuple[Decimal, Decimal], ...],
    right: tuple[tuple[Decimal, Decimal], ...],
) -> Decimal:
    total = Decimal("0")
    index = 0
    for start, end in left:
        while index < len(right) and right[index][1] <= start:
            index += 1
        cursor = index
        while cursor < len(right) and right[cursor][0] < end:
            overlap = min(end, right[cursor][1]) - max(start, right[cursor][0])
            if overlap > 0:
                total += overlap
            cursor += 1
    return total


def _ratio(part: Decimal, whole: Decimal) -> Decimal:
    if whole <= 0:
        return Decimal("0").quantize(_RATIO_QUANTUM)
    value = part / whole
    bounded = min(Decimal("1"), max(Decimal("0"), value))
    return bounded.quantize(_RATIO_QUANTUM)


def _seconds(value: Decimal) -> Decimal:
    return value.quantize(_SECOND_QUANTUM)


def _parameters_digest(
    parameters: CaptionAlignmentParameters,
    *,
    caption_sha256: str,
    audio_sha256: str,
) -> str:
    canonical = json.dumps(
        {
            "algorithm": _ALGORITHM,
            "schema_version": _REPORT_SCHEMA_VERSION,
            "parameters": parameters.as_payload(),
            "caption_sha256": caption_sha256,
            "audio_sha256": audio_sha256,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _report_payload(
    *,
    run_id: int,
    caption_artifact: Artifact,
    audio_artifact: Artifact,
    parameters: CaptionAlignmentParameters,
    parameters_digest: str,
    measurement: CaptionAlignmentMeasurement,
) -> dict[str, object]:
    return {
        "schema_version": _REPORT_SCHEMA_VERSION,
        "algorithm": _ALGORITHM,
        "run_id": run_id,
        "caption_artifact_id": caption_artifact.id,
        "caption_sha256": caption_artifact.sha256,
        "audio_artifact_id": audio_artifact.id,
        "audio_sha256": audio_artifact.sha256,
        "parameters": parameters.as_payload(),
        "parameters_digest": parameters_digest,
        "measurement": measurement.as_payload(),
        "external_service_consulted": False,
        "published": False,
    }


def _report_path(storage_root: Path, *, run_id: int, digest: str) -> Path:
    root = storage_root.resolve()
    if root == Path(root.anchor):
        raise CaptionAlignmentError("CAPTION_ALIGNMENT_EVIDENCE_UNVERIFIED")
    return (
        root
        / "content-engine"
        / f"run-{run_id}"
        / "captions"
        / f"alignment-{digest}.json"
    )


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


def _persisted_id(value: int | None, kind: str) -> int:
    if not isinstance(value, int):
        raise CaptionAlignmentError(f"CAPTION_ALIGNMENT_{kind.upper()}_NOT_PERSISTED")
    return value
