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
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_session
from src.dashboard.service import load_dashboard_snapshot, load_run_detail
from src.platform.control_service import request_run_cancellation
from src.platform.models import Run
from src.platform.run_service import InvalidRunTransitionError

_TEMPLATE_DIRECTORY = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIRECTORY))


def create_app(*, csrf_token: str | None = None) -> FastAPI:
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
            context={"snapshot": snapshot},
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

    return application


async def _parse_cancellation_form(request: Request) -> tuple[str, str, str]:
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
            max_num_fields=4,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="invalid form body") from exc

    csrf_token = _single_form_value(fields, "csrf_token", maximum_length=128)
    confirmation = _single_form_value(fields, "confirmation", maximum_length=32)
    reason = _single_form_value(fields, "reason", maximum_length=500)
    return csrf_token, confirmation, reason


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
