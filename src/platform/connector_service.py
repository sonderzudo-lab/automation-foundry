"""Validated, append-only connector observations for the local control plane."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, localcontext

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.platform.models import (
    Automation,
    ConnectorObservation,
    ConnectorStatus,
    DataQualityStatus,
)
from src.platform.run_service import IdempotencyConflictError

_CONNECTOR_KEY = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_QUALITY_QUANTUM = Decimal("0.0001")
_MAX_SLO_SECONDS = 365 * 24 * 60 * 60


class ConnectorObservationValidationError(ValueError):
    """Raised when connector state cannot be stored without ambiguity."""


@dataclass(frozen=True, slots=True)
class ConnectorObservationCreationResult:
    """Result of recording or replaying one connector observation."""

    observation: ConnectorObservation
    created: bool


def _require_text(value: str, field: str, *, max_length: int) -> str:
    normalized = value.strip()
    if not normalized:
        raise ConnectorObservationValidationError(f"{field} must not be empty")
    if len(normalized) > max_length:
        raise ConnectorObservationValidationError(
            f"{field} exceeds {max_length} characters"
        )
    return normalized


def _as_utc_naive(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ConnectorObservationValidationError(
            f"{field} must be timezone-aware"
        )
    return value.astimezone(UTC).replace(tzinfo=None)


def _positive_slo(value: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConnectorObservationValidationError(f"{field} must be an integer")
    if value < 1 or value > _MAX_SLO_SECONDS:
        raise ConnectorObservationValidationError(
            f"{field} must be between 1 and {_MAX_SLO_SECONDS}"
        )
    return value


def _quality_score(value: Decimal | int | str | None) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, str)):
        raise ConnectorObservationValidationError(
            "quality_score must be a decimal, integer, string, or null"
        )
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ConnectorObservationValidationError(
            "quality_score must be a valid decimal"
        ) from exc
    if not parsed.is_finite() or parsed < 0 or parsed > 1:
        raise ConnectorObservationValidationError(
            "quality_score must be between 0 and 1"
        )
    with localcontext() as context:
        context.prec = 20
        normalized = parsed.quantize(_QUALITY_QUANTUM)
    if normalized != parsed:
        raise ConnectorObservationValidationError(
            "quality_score exceeds 4 decimal places"
        )
    return normalized


async def record_connector_observation(
    session: AsyncSession,
    *,
    automation: Automation,
    connector_key: str,
    idempotency_key: str,
    status: ConnectorStatus,
    status_slo_seconds: int,
    last_success_at: datetime | None,
    freshness_slo_seconds: int,
    quality_status: DataQualityStatus,
    quality_score: Decimal | int | str | None,
    observed_at: datetime,
    now: datetime | None = None,
) -> ConnectorObservationCreationResult:
    """Record one redacted observation or return an exact idempotent replay."""
    if automation.id is None:
        raise ConnectorObservationValidationError(
            "automation must be persisted before recording connector state"
        )
    connector_key = _require_text(
        connector_key,
        "connector_key",
        max_length=100,
    )
    if _CONNECTOR_KEY.fullmatch(connector_key) is None:
        raise ConnectorObservationValidationError(
            "connector_key must be a lowercase ASCII slug"
        )
    idempotency_key = _require_text(
        idempotency_key,
        "idempotency_key",
        max_length=255,
    )
    try:
        connector_status = ConnectorStatus(status)
    except ValueError as exc:
        raise ConnectorObservationValidationError("status is not supported") from exc
    try:
        normalized_quality_status = DataQualityStatus(quality_status)
    except ValueError as exc:
        raise ConnectorObservationValidationError(
            "quality_status is not supported"
        ) from exc
    normalized_score = _quality_score(quality_score)
    if (
        normalized_quality_status is DataQualityStatus.UNKNOWN
        and normalized_score is not None
    ):
        raise ConnectorObservationValidationError(
            "unknown quality_status cannot have a quality_score"
        )
    normalized_observed_at = _as_utc_naive(observed_at, "observed_at")
    recorded_at = _as_utc_naive(now or datetime.now(UTC), "now")
    if normalized_observed_at > recorded_at:
        raise ConnectorObservationValidationError(
            "observed_at must not be after the server clock"
        )
    normalized_last_success_at = (
        None
        if last_success_at is None
        else _as_utc_naive(last_success_at, "last_success_at")
    )
    if (
        normalized_last_success_at is not None
        and normalized_last_success_at > normalized_observed_at
    ):
        raise ConnectorObservationValidationError(
            "last_success_at must not be after observed_at"
        )
    normalized_status_slo = _positive_slo(
        status_slo_seconds,
        "status_slo_seconds",
    )
    normalized_freshness_slo = _positive_slo(
        freshness_slo_seconds,
        "freshness_slo_seconds",
    )

    existing = await session.scalar(
        select(ConnectorObservation).where(
            ConnectorObservation.automation_id == automation.id,
            ConnectorObservation.connector_key == connector_key,
            ConnectorObservation.idempotency_key == idempotency_key,
        )
    )
    expected = (
        connector_status.value,
        normalized_status_slo,
        normalized_last_success_at,
        normalized_freshness_slo,
        normalized_quality_status.value,
        normalized_score,
        normalized_observed_at,
    )
    if existing is not None:
        actual = (
            existing.status,
            existing.status_slo_seconds,
            existing.last_success_at,
            existing.freshness_slo_seconds,
            existing.quality_status,
            existing.quality_score,
            existing.observed_at,
        )
        if actual != expected:
            raise IdempotencyConflictError(
                "connector observation idempotency key already exists with "
                "different inputs"
            )
        return ConnectorObservationCreationResult(existing, created=False)

    observation = ConnectorObservation(
        automation_id=automation.id,
        connector_key=connector_key,
        idempotency_key=idempotency_key,
        status=connector_status.value,
        status_slo_seconds=normalized_status_slo,
        last_success_at=normalized_last_success_at,
        freshness_slo_seconds=normalized_freshness_slo,
        quality_status=normalized_quality_status.value,
        quality_score=normalized_score,
        observed_at=normalized_observed_at,
        recorded_at=recorded_at,
    )
    try:
        async with session.begin_nested():
            session.add(observation)
            await session.flush()
    except IntegrityError as exc:
        concurrent = await session.scalar(
            select(ConnectorObservation).where(
                ConnectorObservation.automation_id == automation.id,
                ConnectorObservation.connector_key == connector_key,
                ConnectorObservation.idempotency_key == idempotency_key,
            )
        )
        if concurrent is None:
            raise
        actual = (
            concurrent.status,
            concurrent.status_slo_seconds,
            concurrent.last_success_at,
            concurrent.freshness_slo_seconds,
            concurrent.quality_status,
            concurrent.quality_score,
            concurrent.observed_at,
        )
        if actual != expected:
            raise IdempotencyConflictError(
                "connector observation idempotency key already exists with "
                "different inputs"
            ) from exc
        return ConnectorObservationCreationResult(concurrent, created=False)
    return ConnectorObservationCreationResult(observation, created=True)
