"""Persistent human approval gates for protected automation actions."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.platform.control_service import get_execution_control_state
from src.platform.models import (
    Approval,
    ApprovalEvent,
    ApprovalStatus,
    Run,
    RunStatus,
    StepRun,
)
from src.platform.run_service import (
    IdempotencyConflictError,
    InvalidRunTransitionError,
    transition_run,
)


class ApprovalGateError(ValueError):
    """Raised when a protected action lacks a matching approved decision."""


_REVIEW_KEY = re.compile(r"[a-z][a-z0-9_-]{0,39}")
_SENSITIVE_REVIEW_KEY = re.compile(r"(?:secret|token|password|credential|key)", re.I)


@dataclass(frozen=True, slots=True)
class ApprovalCreationResult:
    approval: Approval
    created: bool


@dataclass(frozen=True, slots=True)
class ApprovalDecisionResult:
    approval: Approval
    changed: bool


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _require_text(value: str, field: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} must not be empty")
    return normalized


def approval_payload_digest(payload: dict[str, Any]) -> str:
    """Return a stable SHA-256 digest for the exact JSON-compatible payload."""
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _normalize_review_payload(review_payload: dict[str, str]) -> dict[str, str]:
    """Validate the intentionally limited, operator-visible approval projection."""
    if not 1 <= len(review_payload) <= 8:
        raise ValueError("review_payload must contain between 1 and 8 fields")
    normalized: dict[str, str] = {}
    for key, value in review_payload.items():
        if not isinstance(key, str) or not _REVIEW_KEY.fullmatch(key):
            raise ValueError("review_payload contains an invalid field name")
        if _SENSITIVE_REVIEW_KEY.search(key):
            raise ValueError("review_payload field name suggests protected data")
        if not isinstance(value, str):
            raise ValueError("review_payload values must be strings")
        text = value.strip()
        if not text or len(text) > 300:
            raise ValueError("review_payload values must be 1 to 300 characters")
        normalized[key] = text
    return normalized


async def request_approval(
    session: AsyncSession,
    *,
    run: Run,
    idempotency_key: str,
    action: str,
    summary: str,
    input_payload: dict[str, Any],
    review_payload: dict[str, str],
) -> ApprovalCreationResult:
    """Create one pending approval and move its running run to the waiting state."""
    if run.id is None:
        raise ValueError("run must be persisted before requesting approval")
    idempotency_key = _require_text(idempotency_key, "idempotency_key")
    action = _require_text(action, "action")
    summary = _require_text(summary, "summary")
    digest = approval_payload_digest(input_payload)
    review = _normalize_review_payload(review_payload)

    existing = await session.scalar(
        select(Approval).where(
            Approval.run_id == run.id,
            Approval.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        expected = (action, summary, digest, review)
        actual = (
            existing.action,
            existing.summary,
            existing.payload_digest,
            existing.review_payload,
        )
        if actual != expected:
            raise IdempotencyConflictError(
                "approval idempotency key already exists with different inputs"
            )
        return ApprovalCreationResult(existing, created=False)

    if RunStatus(run.status) is not RunStatus.RUNNING:
        raise InvalidRunTransitionError(
            "approval can only be requested for a running run"
        )
    control = await get_execution_control_state(session, run_id=run.id)
    if control.blocked:
        raise ApprovalGateError(f"approval request blocked by {control.code}")

    approval = Approval(
        run_id=run.id,
        idempotency_key=idempotency_key,
        action=action,
        summary=summary,
        payload_digest=digest,
        review_payload=review,
        status=ApprovalStatus.PENDING.value,
    )
    session.add(approval)
    await session.flush()
    session.add(
        ApprovalEvent(
            approval_id=approval.id,
            from_status=None,
            to_status=ApprovalStatus.PENDING.value,
            reason="approval requested",
        )
    )
    await transition_run(
        session,
        run,
        RunStatus.AWAITING_APPROVAL,
        note=f"awaiting approval {approval.id}",
    )
    await session.flush()
    return ApprovalCreationResult(approval, created=True)


async def decide_approval(
    session: AsyncSession,
    *,
    approval: Approval,
    decision: ApprovalStatus,
    actor: str,
    reason: str,
) -> ApprovalDecisionResult:
    """Persist one immutable human decision and transition its waiting run."""
    if approval.id is None:
        raise ValueError("approval must be persisted before decision")
    if decision not in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}:
        raise ValueError("decision must be approved or rejected")
    actor = _require_text(actor, "actor")
    reason = _require_text(reason, "reason")

    current = ApprovalStatus(approval.status)
    if current is decision:
        return ApprovalDecisionResult(approval, changed=False)
    if current is not ApprovalStatus.PENDING:
        raise IdempotencyConflictError(
            f"approval already has conflicting decision {current.value}"
        )
    if decision is ApprovalStatus.APPROVED and approval.review_payload is None:
        raise ApprovalGateError("approval has no safe review projection")

    run = await session.get(Run, approval.run_id)
    if run is None:
        raise ValueError("approval run does not exist")
    if RunStatus(run.status) is not RunStatus.AWAITING_APPROVAL:
        raise InvalidRunTransitionError(
            "approval decision requires a run awaiting approval"
        )

    target = (
        RunStatus.RUNNING
        if decision is ApprovalStatus.APPROVED
        else RunStatus.CANCELLED
    )
    await transition_run(
        session,
        run,
        target,
        note=f"approval {approval.id} {decision.value}",
    )
    approval.status = decision.value
    approval.decided_at = _utcnow()
    approval.decided_by = actor
    approval.decision_reason = reason
    session.add(
        ApprovalEvent(
            approval_id=approval.id,
            from_status=ApprovalStatus.PENDING.value,
            to_status=decision.value,
            actor=actor,
            reason=reason,
        )
    )
    await session.flush()
    return ApprovalDecisionResult(approval, changed=True)


async def assert_approval_granted(
    session: AsyncSession,
    *,
    approval_id: int,
    run_id: int,
    action: str,
    step_idempotency_key: str,
    input_payload: dict[str, Any],
) -> Approval:
    """Return the exact approved gate or fail closed before protected work."""
    approval = await session.get(Approval, approval_id)
    if approval is None:
        raise ApprovalGateError("required approval does not exist")
    if approval.run_id != run_id:
        raise ApprovalGateError("approval belongs to a different run")
    if approval.action != action.strip():
        raise ApprovalGateError("approval action does not match protected task")
    if approval.payload_digest != approval_payload_digest(input_payload):
        raise ApprovalGateError("approval payload does not match protected task")
    if ApprovalStatus(approval.status) is not ApprovalStatus.APPROVED:
        raise ApprovalGateError("required approval is not approved")
    existing_step_key = await session.scalar(
        select(StepRun.idempotency_key)
        .where(StepRun.approval_id == approval.id)
        .limit(1)
    )
    if existing_step_key is not None and existing_step_key != step_idempotency_key:
        raise ApprovalGateError("approval is already bound to a different task")
    return approval
