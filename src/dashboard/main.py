"""Local, read-only FastAPI control-plane dashboard."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.database import get_session
from src.dashboard.service import load_dashboard_snapshot, load_run_detail

_TEMPLATE_DIRECTORY = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIRECTORY))


def create_app() -> FastAPI:
    """Create the side-effect-free dashboard application."""
    application = FastAPI(
        title="Automation Foundry",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

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
            context={"run": detail},
        )

    return application


app = create_app()
