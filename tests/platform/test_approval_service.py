"""Persistence and safety tests for human approval gates."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload

from src.core.database import Base
from src.platform.approval_service import (
    ApprovalGateError,
    assert_approval_granted,
    decide_approval,
    request_approval,
)
from src.platform.control_service import request_run_cancellation
from src.platform.models import Approval, ApprovalStatus, Run, RunStatus
from src.platform.run_service import (
    IdempotencyConflictError,
    get_or_create_automation,
    get_or_create_run,
    transition_run,
)


@pytest.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as database_session:
        yield database_session
    await engine.dispose()


async def _running_run(session: AsyncSession, key: str = "approval-run") -> Run:
    automation = (
        await get_or_create_automation(
            session,
            slug="approval-tests",
            name="Approval Tests",
            owner="local-owner",
        )
    ).automation
    run = (
        await get_or_create_run(
            session,
            automation=automation,
            idempotency_key=key,
        )
    ).run
    await transition_run(session, run, RunStatus.RUNNING)
    return run


async def _request(session: AsyncSession, run: Run) -> Approval:
    return (
        await request_approval(
            session,
            run=run,
            idempotency_key="approval:publish:001",
            action="publish",
            summary="Publish reviewed local artifact",
            input_payload={"artifact_id": 7, "visibility": "private"},
        )
    ).approval


async def test_request_is_idempotent_and_moves_run_to_waiting(
    session: AsyncSession,
) -> None:
    run = await _running_run(session)
    first = await request_approval(
        session,
        run=run,
        idempotency_key="approval:publish:001",
        action="publish",
        summary="Publish reviewed local artifact",
        input_payload={"artifact_id": 7, "visibility": "private"},
    )
    second = await request_approval(
        session,
        run=run,
        idempotency_key="approval:publish:001",
        action="publish",
        summary="Publish reviewed local artifact",
        input_payload={"visibility": "private", "artifact_id": 7},
    )
    await session.commit()

    fetched = await session.scalar(
        select(Approval)
        .where(Approval.id == first.approval.id)
        .options(selectinload(Approval.events))
    )
    assert fetched is not None
    assert first.created is True
    assert second.created is False
    assert second.approval.id == first.approval.id
    assert run.status == RunStatus.AWAITING_APPROVAL.value
    assert fetched.status == ApprovalStatus.PENDING.value
    assert [(event.from_status, event.to_status) for event in fetched.events] == [
        (None, ApprovalStatus.PENDING.value)
    ]

    with pytest.raises(IdempotencyConflictError, match="different inputs"):
        await request_approval(
            session,
            run=run,
            idempotency_key="approval:publish:001",
            action="publish",
            summary="Changed summary",
            input_payload={"artifact_id": 7, "visibility": "private"},
        )


async def test_approval_decision_is_immutable_and_resumes_run(
    session: AsyncSession,
) -> None:
    run = await _running_run(session)
    approval = await _request(session, run)
    first = await decide_approval(
        session,
        approval=approval,
        decision=ApprovalStatus.APPROVED,
        actor="local-owner",
        reason="manual review passed",
    )
    duplicate = await decide_approval(
        session,
        approval=approval,
        decision=ApprovalStatus.APPROVED,
        actor="local-owner",
        reason="duplicate click",
    )
    await session.commit()

    fetched = await session.scalar(
        select(Approval)
        .where(Approval.id == approval.id)
        .options(selectinload(Approval.events))
    )
    assert fetched is not None
    assert first.changed is True
    assert duplicate.changed is False
    assert run.status == RunStatus.RUNNING.value
    assert fetched.decided_by == "local-owner"
    assert fetched.decision_reason == "manual review passed"
    assert [event.to_status for event in fetched.events] == [
        ApprovalStatus.PENDING.value,
        ApprovalStatus.APPROVED.value,
    ]

    with pytest.raises(IdempotencyConflictError, match="conflicting decision"):
        await decide_approval(
            session,
            approval=approval,
            decision=ApprovalStatus.REJECTED,
            actor="local-owner",
            reason="cannot reverse immutable decision",
        )


async def test_rejection_cancels_run(session: AsyncSession) -> None:
    run = await _running_run(session)
    approval = await _request(session, run)
    await decide_approval(
        session,
        approval=approval,
        decision=ApprovalStatus.REJECTED,
        actor="local-owner",
        reason="content needs revision",
    )

    assert approval.status == ApprovalStatus.REJECTED.value
    assert run.status == RunStatus.CANCELLED.value


async def test_gate_matches_run_action_and_exact_payload(
    session: AsyncSession,
) -> None:
    run = await _running_run(session)
    approval = await _request(session, run)
    await decide_approval(
        session,
        approval=approval,
        decision=ApprovalStatus.APPROVED,
        actor="local-owner",
        reason="reviewed",
    )

    granted = await assert_approval_granted(
        session,
        approval_id=approval.id,
        run_id=run.id,
        action="publish",
        step_idempotency_key="publish:step:001",
        input_payload={"visibility": "private", "artifact_id": 7},
    )
    assert granted.id == approval.id

    with pytest.raises(ApprovalGateError, match="payload"):
        await assert_approval_granted(
            session,
            approval_id=approval.id,
            run_id=run.id,
            action="publish",
            step_idempotency_key="publish:step:001",
            input_payload={"visibility": "public", "artifact_id": 7},
        )


async def test_cancellation_while_waiting_cancels_run(session: AsyncSession) -> None:
    run = await _running_run(session)
    approval = await _request(session, run)
    await request_run_cancellation(
        session,
        run=run,
        reason="operator cancelled pending action",
    )

    assert run.status == RunStatus.CANCELLED.value
    assert approval.status == ApprovalStatus.CANCELLED.value
