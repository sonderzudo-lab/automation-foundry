"""Validated, append-only financial observations for the local control plane."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation, localcontext

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.platform.models import (
    Automation,
    LedgerEntry,
    LedgerEntryType,
    MetricPoint,
    Run,
    StepRun,
)
from src.platform.run_service import IdempotencyConflictError

_AMOUNT_QUANTUM = Decimal("0.0000000001")
_AMOUNT_LIMIT = Decimal("99999999999999999999.9999999999")
_CONFIDENCE_QUANTUM = Decimal("0.0001")


class LedgerValidationError(ValueError):
    """Raised when a ledger observation is ambiguous or inconsistently scoped."""


@dataclass(frozen=True, slots=True)
class LedgerEntryCreationResult:
    """Result of recording or replaying one immutable ledger entry."""

    ledger_entry: LedgerEntry
    created: bool


def _utcnow_aware() -> datetime:
    return datetime.now(UTC)


def _require_text(value: str, field: str, *, max_length: int) -> str:
    normalized = value.strip()
    if not normalized:
        raise LedgerValidationError(f"{field} must not be empty")
    if len(normalized) > max_length:
        raise LedgerValidationError(f"{field} exceeds {max_length} characters")
    return normalized


def _as_utc_naive(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise LedgerValidationError(f"{field} must be timezone-aware")
    return value.astimezone(UTC).replace(tzinfo=None)


def _as_decimal(
    value: Decimal | int | str,
    field: str,
    *,
    quantum: Decimal,
    limit: Decimal,
) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, int, str)):
        raise LedgerValidationError(f"{field} must be a decimal, integer, or string")
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise LedgerValidationError(f"{field} must be a valid decimal") from exc
    if not parsed.is_finite():
        raise LedgerValidationError(f"{field} must be finite")
    if parsed.copy_abs() > limit:
        raise LedgerValidationError(f"{field} exceeds the supported numeric range")
    with localcontext() as context:
        context.prec = 50
        normalized = parsed.quantize(quantum)
    if normalized != parsed:
        exponent = quantum.as_tuple().exponent
        if not isinstance(exponent, int):
            raise LedgerValidationError(f"{field} has an invalid numeric scale")
        decimal_places = max(0, -exponent)
        raise LedgerValidationError(
            f"{field} exceeds {decimal_places} decimal places"
        )
    return normalized


def _validate_currency(value: str) -> str:
    currency = _require_text(value, "currency", max_length=3)
    if (
        len(currency) != 3
        or not currency.isascii()
        or not currency.isalpha()
        or not currency.isupper()
    ):
        raise LedgerValidationError(
            "currency must be a three-letter uppercase code"
        )
    return currency


def _validate_scope(
    automation: Automation,
    run: Run | None,
    step_run: StepRun | None,
    metric_point: MetricPoint | None,
) -> None:
    if automation.id is None:
        raise LedgerValidationError(
            "automation must be persisted before recording a ledger entry"
        )
    if run is not None:
        if run.id is None:
            raise LedgerValidationError(
                "run must be persisted before recording a ledger entry"
            )
        if run.automation_id != automation.id:
            raise LedgerValidationError("run does not belong to ledger automation")
    if step_run is not None:
        if run is None:
            raise LedgerValidationError("step ledger entry requires its parent run")
        if step_run.id is None:
            raise LedgerValidationError(
                "step run must be persisted before recording a ledger entry"
            )
        if step_run.run_id != run.id:
            raise LedgerValidationError("step run does not belong to ledger run")
    if metric_point is not None:
        if metric_point.id is None:
            raise LedgerValidationError(
                "metric point must be persisted before recording a ledger entry"
            )
        if metric_point.automation_id != automation.id:
            raise LedgerValidationError(
                "metric point does not belong to ledger automation"
            )
        run_id = run.id if run is not None else None
        step_run_id = step_run.id if step_run is not None else None
        if metric_point.run_id != run_id or metric_point.step_run_id != step_run_id:
            raise LedgerValidationError(
                "metric point scope does not match ledger run and step"
            )


async def record_ledger_entry(
    session: AsyncSession,
    *,
    automation: Automation,
    idempotency_key: str,
    entry_type: LedgerEntryType,
    category: str,
    amount: Decimal | int | str,
    currency: str,
    source: str,
    run: Run | None = None,
    step_run: StepRun | None = None,
    metric_point: MetricPoint | None = None,
    confidence: Decimal | int | str | None = None,
    observed_at: datetime | None = None,
    now: datetime | None = None,
) -> LedgerEntryCreationResult:
    """Record one financial observation or return an exact idempotent replay."""
    _validate_scope(automation, run, step_run, metric_point)
    idempotency_key = _require_text(
        idempotency_key,
        "idempotency_key",
        max_length=255,
    )
    try:
        normalized_type = LedgerEntryType(entry_type)
    except ValueError as exc:
        raise LedgerValidationError("entry_type is not supported") from exc
    category = _require_text(category, "category", max_length=100)
    normalized_amount = _as_decimal(
        amount,
        "amount",
        quantum=_AMOUNT_QUANTUM,
        limit=_AMOUNT_LIMIT,
    )
    currency = _validate_currency(currency)
    source = _require_text(source, "source", max_length=200)
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
        raise LedgerValidationError("confidence must be between 0 and 1")
    normalized_observed_at = (
        _as_utc_naive(observed_at, "observed_at")
        if observed_at is not None
        else None
    )

    existing = await session.scalar(
        select(LedgerEntry).where(
            LedgerEntry.automation_id == automation.id,
            LedgerEntry.idempotency_key == idempotency_key,
        )
    )
    expected = (
        run.id if run is not None else None,
        step_run.id if step_run is not None else None,
        metric_point.id if metric_point is not None else None,
        normalized_type.value,
        category,
        normalized_amount,
        currency,
        source,
        normalized_confidence,
    )
    if existing is not None:
        actual = (
            existing.run_id,
            existing.step_run_id,
            existing.metric_point_id,
            existing.entry_type,
            existing.category,
            existing.amount,
            existing.currency,
            existing.source,
            existing.confidence,
        )
        if actual != expected or (
            normalized_observed_at is not None
            and existing.observed_at != normalized_observed_at
        ):
            raise IdempotencyConflictError(
                "ledger idempotency key already exists with different inputs"
            )
        return LedgerEntryCreationResult(existing, created=False)

    recorded_at = _as_utc_naive(now or _utcnow_aware(), "now")
    ledger_entry = LedgerEntry(
        automation_id=automation.id,
        run_id=run.id if run is not None else None,
        step_run_id=step_run.id if step_run is not None else None,
        metric_point_id=metric_point.id if metric_point is not None else None,
        idempotency_key=idempotency_key,
        entry_type=normalized_type.value,
        category=category,
        amount=normalized_amount,
        currency=currency,
        source=source,
        confidence=normalized_confidence,
        observed_at=normalized_observed_at or recorded_at,
        recorded_at=recorded_at,
    )
    session.add(ledger_entry)
    await session.flush()
    return LedgerEntryCreationResult(ledger_entry, created=True)
