"""Precision, attribution, and idempotency tests for ledger observations."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.database import Base
from src.platform.ledger_service import LedgerValidationError, record_ledger_entry
from src.platform.metric_service import record_metric_point
from src.platform.models import (
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
    IdempotencyConflictError,
    get_or_create_automation,
    get_or_create_run,
    transition_run,
)
from src.platform.step_service import get_or_create_step_run


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as database_session:
        yield database_session
    await engine.dispose()


async def _scope(
    session: AsyncSession,
    *,
    slug: str = "ledger-tests",
) -> tuple[Automation, Run, StepRun, MetricPoint]:
    automation = (
        await get_or_create_automation(
            session,
            slug=slug,
            name=f"{slug} automation",
            owner="local-owner",
        )
    ).automation
    run = (
        await get_or_create_run(
            session,
            automation=automation,
            idempotency_key=f"{slug}:run",
        )
    ).run
    await transition_run(session, run, RunStatus.RUNNING)
    step_run = (
        await get_or_create_step_run(
            session,
            run=run,
            name="measure-value",
            queue=QueueClass.CPU,
            ordinal=1,
            idempotency_key=f"{slug}:step",
        )
    ).step_run
    metric_point = (
        await record_metric_point(
            session,
            automation=automation,
            run=run,
            step_run=step_run,
            idempotency_key=f"{slug}:metric",
            name="value.evidence",
            kind=MetricKind.CURRENCY,
            value="12.34",
            unit="BRL",
            source="local-estimate",
        )
    ).metric_point
    return automation, run, step_run, metric_point


async def test_ledger_entry_is_precise_attributable_and_idempotent(
    session: AsyncSession,
) -> None:
    automation, run, step_run, metric_point = await _scope(session)
    observed_at = datetime(
        2026,
        7,
        23,
        9,
        30,
        tzinfo=timezone(timedelta(hours=-3)),
    )
    first = await record_ledger_entry(
        session,
        automation=automation,
        run=run,
        step_run=step_run,
        metric_point=metric_point,
        idempotency_key="ledger-tests:value:1",
        entry_type=LedgerEntryType.ATTRIBUTED_VALUE,
        category="human-time-saved",
        amount="12.345",
        currency="BRL",
        source="local-estimate",
        confidence="0.7500",
        observed_at=observed_at,
        now=datetime(2026, 7, 23, 12, 31, tzinfo=UTC),
    )
    replay = await record_ledger_entry(
        session,
        automation=automation,
        run=run,
        step_run=step_run,
        metric_point=metric_point,
        idempotency_key="ledger-tests:value:1",
        entry_type=LedgerEntryType.ATTRIBUTED_VALUE,
        category="human-time-saved",
        amount=Decimal("12.3450000000"),
        currency="BRL",
        source="local-estimate",
        confidence=Decimal("0.75"),
        observed_at=observed_at,
    )
    await session.commit()

    stored = await session.scalar(
        select(LedgerEntry).where(LedgerEntry.id == first.ledger_entry.id)
    )
    assert stored is not None
    assert first.created is True
    assert replay.created is False
    assert replay.ledger_entry.id == first.ledger_entry.id
    assert stored.amount == Decimal("12.3450000000")
    assert stored.confidence == Decimal("0.7500")
    assert stored.observed_at == datetime(2026, 7, 23, 12, 30)
    assert stored.recorded_at == datetime(2026, 7, 23, 12, 31)
    assert stored.metric_point_id == metric_point.id


async def test_sqlite_preserves_full_supported_ledger_precision(
    session: AsyncSession,
) -> None:
    automation, _, _, _ = await _scope(session)
    expected = Decimal("-99999999999999999999.9999999999")
    creation = await record_ledger_entry(
        session,
        automation=automation,
        idempotency_key="ledger-tests:precision",
        entry_type=LedgerEntryType.COST,
        category="correction",
        amount=expected,
        currency="USD",
        source="precision-test",
    )
    ledger_entry_id = creation.ledger_entry.id
    await session.commit()
    session.expire_all()

    stored = await session.get(LedgerEntry, ledger_entry_id)
    assert stored is not None
    assert stored.amount == expected


async def test_ledger_replay_rejects_changed_inputs(session: AsyncSession) -> None:
    automation, _, _, _ = await _scope(session)
    await record_ledger_entry(
        session,
        automation=automation,
        idempotency_key="ledger-tests:cost",
        entry_type=LedgerEntryType.COST,
        category="api",
        amount="1.25",
        currency="USD",
        source="invoice-import",
    )

    with pytest.raises(IdempotencyConflictError, match="different inputs"):
        await record_ledger_entry(
            session,
            automation=automation,
            idempotency_key="ledger-tests:cost",
            entry_type=LedgerEntryType.COST,
            category="api",
            amount="1.26",
            currency="USD",
            source="invoice-import",
        )


async def test_ledger_scope_rejects_inconsistent_links(session: AsyncSession) -> None:
    first_automation, first_run, first_step, first_metric = await _scope(
        session,
        slug="ledger-first",
    )
    second_automation, second_run, _, _ = await _scope(
        session,
        slug="ledger-second",
    )

    with pytest.raises(LedgerValidationError, match="does not belong"):
        await record_ledger_entry(
            session,
            automation=first_automation,
            run=second_run,
            idempotency_key="ledger-first:wrong-run",
            entry_type=LedgerEntryType.REVENUE,
            category="sale",
            amount="10",
            currency="BRL",
            source="manual",
        )
    with pytest.raises(LedgerValidationError, match="requires its parent run"):
        await record_ledger_entry(
            session,
            automation=first_automation,
            step_run=first_step,
            idempotency_key="ledger-first:missing-run",
            entry_type=LedgerEntryType.COST,
            category="compute",
            amount="1",
            currency="BRL",
            source="manual",
        )
    with pytest.raises(LedgerValidationError, match="scope does not match"):
        await record_ledger_entry(
            session,
            automation=first_automation,
            run=first_run,
            idempotency_key="ledger-first:metric-scope",
            entry_type=LedgerEntryType.ATTRIBUTED_VALUE,
            category="estimate",
            amount="10",
            currency="BRL",
            source="manual",
            metric_point=first_metric,
        )
    assert second_automation.id != first_automation.id


@pytest.mark.parametrize(
    ("amount", "currency", "confidence", "error"),
    [
        ("NaN", "BRL", None, "must be finite"),
        ("0.00000000001", "BRL", None, "10 decimal places"),
        ("1", "brl", None, "three-letter uppercase"),
        ("1", "US", None, "three-letter uppercase"),
        ("1", "USD", "1.1", "supported numeric range"),
        ("1", "USD", "-0.1", "between 0 and 1"),
    ],
)
async def test_ledger_rejects_ambiguous_values(
    session: AsyncSession,
    amount: str,
    currency: str,
    confidence: str | None,
    error: str,
) -> None:
    automation, _, _, _ = await _scope(session)

    with pytest.raises(LedgerValidationError, match=error):
        await record_ledger_entry(
            session,
            automation=automation,
            idempotency_key=f"ledger-tests:invalid:{amount}:{currency}:{confidence}",
            entry_type=LedgerEntryType.COST,
            category="test",
            amount=amount,
            currency=currency,
            source="test",
            confidence=confidence,
        )


async def test_database_rejects_invalid_currency(session: AsyncSession) -> None:
    automation, _, _, _ = await _scope(session)
    session.add(
        LedgerEntry(
            automation_id=automation.id,
            idempotency_key="ledger-tests:invalid-db-currency",
            entry_type=LedgerEntryType.COST.value,
            category="test",
            amount=Decimal("1"),
            currency="usd",
            source="direct-test",
            confidence=None,
            observed_at=datetime(2026, 7, 23, 12, 0),
        )
    )

    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()
