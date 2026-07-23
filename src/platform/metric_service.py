"""Validated, append-only metric observations for the local control plane."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, localcontext

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.platform.models import Automation, MetricKind, MetricPoint, Run, StepRun
from src.platform.run_service import IdempotencyConflictError

_VALUE_QUANTUM = Decimal("0.0000000001")
_VALUE_LIMIT = Decimal("99999999999999999999.9999999999")
_CONFIDENCE_QUANTUM = Decimal("0.0001")


class MetricValidationError(ValueError):
    """Raised when a metric cannot be represented without ambiguity."""


@dataclass(frozen=True, slots=True)
class MetricPointCreationResult:
    """Result of recording or replaying one immutable metric point."""

    metric_point: MetricPoint
    created: bool


def _utcnow_aware() -> datetime:
    return datetime.now(UTC)


def _require_text(value: str, field: str, *, max_length: int) -> str:
    normalized = value.strip()
    if not normalized:
        raise MetricValidationError(f"{field} must not be empty")
    if len(normalized) > max_length:
        raise MetricValidationError(f"{field} exceeds {max_length} characters")
    return normalized


def _as_utc_naive(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise MetricValidationError(f"{field} must be timezone-aware")
    return value.astimezone(UTC).replace(tzinfo=None)


def _as_decimal(
    value: Decimal | int | str,
    field: str,
    *,
    quantum: Decimal,
    limit: Decimal,
) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, str)):
        raise MetricValidationError(f"{field} must be a decimal, integer, or string")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise MetricValidationError(f"{field} must be a valid decimal") from exc
    if not parsed.is_finite():
        raise MetricValidationError(f"{field} must be finite")
    if parsed.copy_abs() > limit:
        raise MetricValidationError(f"{field} exceeds the supported numeric range")
    with localcontext() as context:
        context.prec = 50
        normalized = parsed.quantize(quantum)
    if normalized != parsed:
        exponent = quantum.as_tuple().exponent
        if not isinstance(exponent, int):
            raise MetricValidationError(f"{field} has an invalid numeric scale")
        decimal_places = max(0, -exponent)
        raise MetricValidationError(
            f"{field} exceeds {decimal_places} decimal places"
        )
    return normalized


def _normalize_dimensions(value: dict[str, object] | None) -> dict[str, object]:
    dimensions = dict(value or {})
    if any(not isinstance(key, str) for key in dimensions):
        raise MetricValidationError("dimensions keys must be strings")
    try:
        canonical = json.dumps(
            dimensions,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        decoded = json.loads(canonical)
    except (TypeError, ValueError) as exc:
        raise MetricValidationError("dimensions must contain valid JSON values") from exc
    if not isinstance(decoded, dict):
        raise MetricValidationError("dimensions must be a JSON object")
    return decoded


def _validate_kind_value(kind: MetricKind, value: Decimal, unit: str) -> None:
    if kind in {MetricKind.COUNTER, MetricKind.DURATION} and value < 0:
        raise MetricValidationError(f"{kind.value} value must not be negative")
    if kind is MetricKind.COUNTER and value != value.to_integral_value():
        raise MetricValidationError("counter value must be an integer")
    if kind is MetricKind.RATIO:
        if value < 0 or value > 1:
            raise MetricValidationError("ratio value must be between 0 and 1")
        if unit != "ratio":
            raise MetricValidationError("ratio metrics must use the 'ratio' unit")
    if kind is MetricKind.CURRENCY and (
        len(unit) != 3 or not unit.isascii() or not unit.isalpha() or not unit.isupper()
    ):
        raise MetricValidationError(
            "currency metrics must use a three-letter uppercase currency unit"
        )


async def record_metric_point(
    session: AsyncSession,
    *,
    automation: Automation,
    idempotency_key: str,
    name: str,
    kind: MetricKind,
    value: Decimal | int | str,
    unit: str,
    source: str,
    run: Run | None = None,
    step_run: StepRun | None = None,
    confidence: Decimal | int | str | None = None,
    dimensions: dict[str, object] | None = None,
    observed_at: datetime | None = None,
    now: datetime | None = None,
) -> MetricPointCreationResult:
    """Record one validated point or return an exact idempotent replay."""
    if automation.id is None:
        raise MetricValidationError(
            "automation must be persisted before recording a metric"
        )
    if run is not None:
        if run.id is None:
            raise MetricValidationError("run must be persisted before recording a metric")
        if run.automation_id != automation.id:
            raise MetricValidationError("run does not belong to metric automation")
    if step_run is not None:
        if run is None:
            raise MetricValidationError("step metric requires its parent run")
        if step_run.id is None:
            raise MetricValidationError(
                "step run must be persisted before recording a metric"
            )
        if step_run.run_id != run.id:
            raise MetricValidationError("step run does not belong to metric run")

    idempotency_key = _require_text(
        idempotency_key,
        "idempotency_key",
        max_length=255,
    )
    name = _require_text(name, "name", max_length=200)
    unit = _require_text(unit, "unit", max_length=50)
    source = _require_text(source, "source", max_length=200)
    try:
        metric_kind = MetricKind(kind)
    except ValueError as exc:
        raise MetricValidationError("kind is not supported") from exc
    normalized_value = _as_decimal(
        value,
        "value",
        quantum=_VALUE_QUANTUM,
        limit=_VALUE_LIMIT,
    )
    _validate_kind_value(metric_kind, normalized_value, unit)
    normalized_confidence = (
        None
        if confidence is None
        else _as_decimal(
            confidence,
            "confidence",
            quantum=_CONFIDENCE_QUANTUM,
            limit=Decimal("1"),
        )
    )
    if normalized_confidence is not None and normalized_confidence < 0:
        raise MetricValidationError("confidence must be between 0 and 1")
    normalized_dimensions = _normalize_dimensions(dimensions)
    normalized_observed_at = (
        _as_utc_naive(observed_at, "observed_at")
        if observed_at is not None
        else None
    )

    existing = await session.scalar(
        select(MetricPoint).where(
            MetricPoint.automation_id == automation.id,
            MetricPoint.idempotency_key == idempotency_key,
        )
    )
    expected = (
        run.id if run is not None else None,
        step_run.id if step_run is not None else None,
        name,
        metric_kind.value,
        normalized_value,
        unit,
        source,
        normalized_confidence,
        normalized_dimensions,
    )
    if existing is not None:
        actual = (
            existing.run_id,
            existing.step_run_id,
            existing.name,
            existing.kind,
            existing.value,
            existing.unit,
            existing.source,
            existing.confidence,
            existing.dimensions,
        )
        if actual != expected or (
            normalized_observed_at is not None
            and existing.observed_at != normalized_observed_at
        ):
            raise IdempotencyConflictError(
                "metric idempotency key already exists with different inputs"
            )
        return MetricPointCreationResult(existing, created=False)

    recorded_at = _as_utc_naive(now or _utcnow_aware(), "now")
    metric_point = MetricPoint(
        automation_id=automation.id,
        run_id=run.id if run is not None else None,
        step_run_id=step_run.id if step_run is not None else None,
        idempotency_key=idempotency_key,
        name=name,
        kind=metric_kind.value,
        value=normalized_value,
        unit=unit,
        source=source,
        confidence=normalized_confidence,
        dimensions=normalized_dimensions,
        observed_at=normalized_observed_at or recorded_at,
        recorded_at=recorded_at,
    )
    session.add(metric_point)
    await session.flush()
    return MetricPointCreationResult(metric_point, created=True)
