"""Local FastAPI control-plane dashboard."""

from __future__ import annotations

import hmac
import ipaddress
import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_session
from src.dashboard.service import load_dashboard_snapshot, load_run_detail
from src.operations.health import HealthReport, collect_health_report
from src.platform.approval_service import decide_approval
from src.platform.control_service import request_run_cancellation, set_automation_kill_switch
from src.platform.executor_registry import (
    list_automation_executors,
    retry_registered_automation,
    start_registered_automation,
)
from src.platform.models import Approval, ApprovalStatus, Automation, Run
from src.platform.run_service import IdempotencyConflictError, InvalidRunTransitionError

_TEMPLATE_DIRECTORY = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIRECTORY))
HealthCollector = Callable[[AsyncSession], Awaitable[HealthReport]]


def create_app(
    *,
    csrf_token: str | None = None,
    health_collector: HealthCollector = collect_health_report,
) -> FastAPI:
    """Create the loopback dashboard with a process-local CSRF token."""
    control_token = csrf_token or secrets.token_urlsafe(32)
    if not control_token.strip():
        raise ValueError("csrf token must not be empty")
    application = FastAPI(
        title="Automation Foundry",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @application.middleware("http")
    async def require_loopback_host(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if not _is_loopback_host(request.url.hostname):
            return PlainTextResponse("invalid host", status_code=400)
        return await call_next(request)

    @application.get("/", response_class=HTMLResponse)
    async def dashboard_home(
        request: Request,
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> HTMLResponse:
        snapshot = await load_dashboard_snapshot(session)
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={
                "snapshot": snapshot,
                "csrf_token": control_token,
                "executors": list_automation_executors(),
            },
        )

    @application.get("/runs/{run_id}", response_class=HTMLResponse)
    async def run_detail(
        request: Request,
        run_id: int,
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> HTMLResponse:
        detail = await load_run_detail(session, run_id=run_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="run not found")
        return templates.TemplateResponse(
            request=request,
            name="run_detail.html",
            context={"run": detail, "csrf_token": control_token},
        )

    @application.get("/health", response_class=HTMLResponse)
    async def health_page(
        request: Request,
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> HTMLResponse:
        report = await health_collector(session)
        return templates.TemplateResponse(
            request=request,
            name="health.html",
            context={"report": report},
        )

    @application.post("/automations/{automation_slug}/runs", response_class=HTMLResponse)
    async def start_manual_run(
        request: Request,
        automation_slug: str,
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> RedirectResponse:
        submitted_token, confirmation, idempotency_key, experiment_id = await _parse_example_run_form(request)
        if not hmac.compare_digest(submitted_token, control_token):
            raise HTTPException(status_code=403, detail="invalid csrf token")
        if confirmation != "start-example-run":
            raise HTTPException(status_code=400, detail="explicit confirmation required")
        try:
            result = await start_registered_automation(
                session,
                slug=automation_slug,
                idempotency_key=idempotency_key,
                experiment_id=experiment_id,
            )
            await session.commit()
        except ValueError as exc:
            await session.rollback()
            raise HTTPException(status_code=409, detail="example run cannot start") from exc
        except Exception:
            await session.rollback()
            raise
        return RedirectResponse(url=f"/runs/{result.run_id}", status_code=303)

    @application.post("/runs/{run_id}/retry", response_class=HTMLResponse)
    async def retry_run(
        request: Request,
        run_id: int,
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> RedirectResponse:
        submitted_token, confirmation, idempotency_key, actor, reason = await _parse_retry_form(request)
        if not hmac.compare_digest(submitted_token, control_token):
            raise HTTPException(status_code=403, detail="invalid csrf token")
        if confirmation != "retry-run":
            raise HTTPException(status_code=400, detail="explicit confirmation required")
        row = (
            await session.execute(
                select(Run, Automation.slug)
                .join(Automation, Automation.id == Run.automation_id)
                .where(Run.id == run_id)
            )
        ).one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="run not found")
        run, automation_slug = row
        try:
            result = await retry_registered_automation(
                session,
                slug=automation_slug,
                retry_of_run_id=run.id,
                idempotency_key=idempotency_key,
                actor=actor,
                reason=reason,
            )
            await session.commit()
        except ValueError as exc:
            await session.rollback()
            raise HTTPException(status_code=409, detail="run cannot be retried") from exc
        except Exception:
            await session.rollback()
            raise
        return RedirectResponse(url=f"/runs/{result.run_id}", status_code=303)

    @application.post("/runs/{run_id}/cancel", response_class=HTMLResponse)
    async def cancel_run(
        request: Request,
        run_id: int,
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> RedirectResponse:
        submitted_token, confirmation, reason = await _parse_cancellation_form(request)
        if not hmac.compare_digest(submitted_token, control_token):
            raise HTTPException(status_code=403, detail="invalid csrf token")
        if confirmation != "cancel-run":
            raise HTTPException(status_code=400, detail="explicit confirmation required")

        run = await session.get(Run, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        try:
            await request_run_cancellation(session, run=run, reason=reason)
            await session.commit()
        except InvalidRunTransitionError as exc:
            await session.rollback()
            raise HTTPException(status_code=409, detail="run cannot be cancelled") from exc
        except ValueError as exc:
            await session.rollback()
            raise HTTPException(status_code=400, detail="invalid cancellation reason") from exc
        except Exception:
            await session.rollback()
            raise
        return RedirectResponse(url=f"/runs/{run_id}", status_code=303)

    @application.post("/approvals/{approval_id}/reject", response_class=HTMLResponse)
    async def reject_approval(
        request: Request,
        approval_id: int,
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> RedirectResponse:
        submitted_token, confirmation, actor, reason = (
            await _parse_approval_rejection_form(request)
        )
        if not hmac.compare_digest(submitted_token, control_token):
            raise HTTPException(status_code=403, detail="invalid csrf token")
        if confirmation != "reject-approval":
            raise HTTPException(status_code=400, detail="explicit confirmation required")

        approval = await session.get(Approval, approval_id)
        if approval is None:
            raise HTTPException(status_code=404, detail="approval not found")
        run_id = approval.run_id
        try:
            await decide_approval(
                session,
                approval=approval,
                decision=ApprovalStatus.REJECTED,
                actor=actor,
                reason=reason,
            )
            await session.commit()
        except (IdempotencyConflictError, InvalidRunTransitionError) as exc:
            await session.rollback()
            raise HTTPException(status_code=409, detail="approval cannot be rejected") from exc
        except ValueError as exc:
            await session.rollback()
            raise HTTPException(status_code=400, detail="invalid approval decision") from exc
        except Exception:
            await session.rollback()
            raise
        return RedirectResponse(url=f"/runs/{run_id}", status_code=303)

    @application.post("/automations/{automation_id}/kill-switch", response_class=HTMLResponse)
    async def change_kill_switch(
        request: Request,
        automation_id: int,
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> RedirectResponse:
        submitted_token, confirmation, target, actor, reason = await _parse_kill_switch_form(request)
        if not hmac.compare_digest(submitted_token, control_token):
            raise HTTPException(status_code=403, detail="invalid csrf token")
        active = target == "enable"
        if target not in {"enable", "disable"} or confirmation != f"kill-switch-{target}":
            raise HTTPException(status_code=400, detail="explicit confirmation required")
        automation = await session.get(Automation, automation_id)
        if automation is None:
            raise HTTPException(status_code=404, detail="automation not found")
        try:
            await set_automation_kill_switch(
                session,
                automation=automation,
                active=active,
                actor=actor,
                reason=reason,
            )
            await session.commit()
        except ValueError as exc:
            await session.rollback()
            raise HTTPException(status_code=400, detail="invalid kill switch change") from exc
        except Exception:
            await session.rollback()
            raise
        return RedirectResponse(url="/", status_code=303)

    @application.post("/approvals/{approval_id}/approve", response_class=HTMLResponse)
    async def approve_approval(
        request: Request,
        approval_id: int,
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> RedirectResponse:
        submitted_token, confirmation, actor, reason = await _parse_approval_decision_form(request)
        if not hmac.compare_digest(submitted_token, control_token):
            raise HTTPException(status_code=403, detail="invalid csrf token")
        if confirmation != "approve-approval":
            raise HTTPException(status_code=400, detail="explicit confirmation required")
        approval = await session.get(Approval, approval_id)
        if approval is None:
            raise HTTPException(status_code=404, detail="approval not found")
        run_id = approval.run_id
        try:
            await decide_approval(session, approval=approval, decision=ApprovalStatus.APPROVED, actor=actor, reason=reason)
            await session.commit()
        except (IdempotencyConflictError, InvalidRunTransitionError) as exc:
            await session.rollback()
            raise HTTPException(status_code=409, detail="approval cannot be approved") from exc
        except ValueError as exc:
            await session.rollback()
            raise HTTPException(status_code=400, detail="invalid approval decision") from exc
        except Exception:
            await session.rollback()
            raise
        return RedirectResponse(url=f"/runs/{run_id}", status_code=303)

    return application


async def _parse_cancellation_form(request: Request) -> tuple[str, str, str]:
    fields = await _parse_form_fields(request, maximum_fields=3)
    csrf_token = _single_form_value(fields, "csrf_token", maximum_length=128)
    confirmation = _single_form_value(fields, "confirmation", maximum_length=32)
    reason = _single_form_value(fields, "reason", maximum_length=500)
    return csrf_token, confirmation, reason


async def _parse_example_run_form(
    request: Request,
) -> tuple[str, str, str, int | None]:
    fields = await _parse_form_fields(request, maximum_fields=4)
    csrf_token = _single_form_value(fields, "csrf_token", maximum_length=128)
    confirmation = _single_form_value(fields, "confirmation", maximum_length=40)
    idempotency_key = _single_form_value(fields, "idempotency_key", maximum_length=255)
    experiment_value = _optional_single_form_value(
        fields,
        "experiment_id",
        maximum_length=20,
    )
    if experiment_value is None:
        experiment_id = None
    else:
        try:
            experiment_id = int(experiment_value)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid experiment_id field") from exc
        if experiment_id < 1:
            raise HTTPException(status_code=400, detail="invalid experiment_id field")
    return csrf_token, confirmation, idempotency_key, experiment_id


async def _parse_retry_form(request: Request) -> tuple[str, str, str, str, str]:
    fields = await _parse_form_fields(request, maximum_fields=5)
    csrf_token = _single_form_value(fields, "csrf_token", maximum_length=128)
    confirmation = _single_form_value(fields, "confirmation", maximum_length=40)
    idempotency_key = _single_form_value(fields, "idempotency_key", maximum_length=255)
    actor = _single_form_value(fields, "actor", maximum_length=200)
    reason = _single_form_value(fields, "reason", maximum_length=500)
    return csrf_token, confirmation, idempotency_key, actor, reason


async def _parse_approval_rejection_form(
    request: Request,
) -> tuple[str, str, str, str]:
    fields = await _parse_form_fields(request, maximum_fields=4)
    csrf_token = _single_form_value(fields, "csrf_token", maximum_length=128)
    confirmation = _single_form_value(fields, "confirmation", maximum_length=32)
    actor = _single_form_value(fields, "actor", maximum_length=200)
    reason = _single_form_value(fields, "reason", maximum_length=500)
    return csrf_token, confirmation, actor, reason


async def _parse_kill_switch_form(
    request: Request,
) -> tuple[str, str, str, str, str]:
    fields = await _parse_form_fields(request, maximum_fields=5)
    csrf_token = _single_form_value(fields, "csrf_token", maximum_length=128)
    confirmation = _single_form_value(fields, "confirmation", maximum_length=40)
    target = _single_form_value(fields, "target", maximum_length=10)
    actor = _single_form_value(fields, "actor", maximum_length=200)
    reason = _single_form_value(fields, "reason", maximum_length=500)
    return csrf_token, confirmation, target, actor, reason


async def _parse_approval_decision_form(
    request: Request,
) -> tuple[str, str, str, str]:
    return await _parse_approval_rejection_form(request)


async def _parse_form_fields(
    request: Request,
    *,
    maximum_fields: int,
) -> dict[str, list[str]]:
    content_type = request.headers.get("content-type", "").split(";", maxsplit=1)[0]
    if content_type.lower() != "application/x-www-form-urlencoded":
        raise HTTPException(status_code=415, detail="unsupported form content type")
    body = await request.body()
    if len(body) > 4_096:
        raise HTTPException(status_code=413, detail="form body too large")
    try:
        fields = parse_qs(
            body.decode("utf-8", errors="strict"),
            keep_blank_values=True,
            max_num_fields=maximum_fields,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="invalid form body") from exc

    return fields


def _single_form_value(
    fields: dict[str, list[str]],
    name: str,
    *,
    maximum_length: int,
) -> str:
    values = fields.get(name)
    if values is None or len(values) != 1:
        raise HTTPException(status_code=400, detail=f"invalid {name} field")
    value = values[0].strip()
    if not value or len(value) > maximum_length:
        raise HTTPException(status_code=400, detail=f"invalid {name} field")
    return value


def _optional_single_form_value(
    fields: dict[str, list[str]],
    name: str,
    *,
    maximum_length: int,
) -> str | None:
    values = fields.get(name)
    if values is None:
        return None
    if len(values) != 1:
        raise HTTPException(status_code=400, detail=f"invalid {name} field")
    value = values[0].strip()
    if not value:
        return None
    if len(value) > maximum_length:
        raise HTTPException(status_code=400, detail=f"invalid {name} field")
    return value


def _is_loopback_host(host: str | None) -> bool:
    if host is None:
        return False
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


app = create_app()
