"""Local FastAPI control-plane dashboard."""

from __future__ import annotations

import hmac
import ipaddress
import mimetypes
import secrets
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.briefs.executor import OperationsBriefReview
from src.core.database import get_session
from src.dashboard.formatting import format_exact_decimal
from src.dashboard.service import (
    load_connector_summaries,
    load_dashboard_snapshot,
    load_run_detail,
)
from src.operations.health import HealthReport, collect_health_report
from src.operations.retention import (
    RetentionInventory,
    RetentionInventoryError,
    collect_retention_inventory,
)
from src.operations.retention_policy import declared_retention_policies
from src.pipeline.a1_executor import ContentScriptReview, load_content_local_export
from src.pipeline.caption_alignment import (
    CaptionAlignmentError,
    CaptionAlignmentReportView,
    load_caption_alignment_report,
)
from src.pipeline.final_review import FinalVideoReview
from src.pipeline.local_export import (
    LocalExportIntegrityError,
    LocalExportNotAvailableError,
    LocalExportPackage,
)
from src.pipeline.narration_review import NarrationReview
from src.pipeline.thumbnail_review import (
    THUMBNAIL_REVIEW_APPROVAL_ACTION,
    ThumbnailReview,
    thumbnail_decision_payload,
)
from src.platform.approval_service import decide_approval
from src.platform.control_service import (
    request_run_cancellation,
    set_automation_enabled,
    set_automation_kill_switch,
)
from src.platform.dispatch_service import (
    DispatchPublishError,
    publish_prepared_dispatch,
    publish_registered_dispatch,
    requeue_completed_run_dispatch,
)
from src.platform.executor_registry import (
    AutomationExecutor,
    AutomationExecutorNotFoundError,
    finalize_registered_approval,
    get_automation_executor,
    list_automation_executors,
    load_registered_approval_review,
    parse_registered_manual_input,
    retry_registered_automation,
    start_registered_automation,
)
from src.platform.models import (
    Approval,
    ApprovalStatus,
    Automation,
    QueueClass,
    Run,
    Schedule,
)
from src.platform.run_service import IdempotencyConflictError, InvalidRunTransitionError
from src.platform.schedule_service import (
    ScheduleControlError,
    set_schedule_enabled,
)

_REVIEW_TEMPLATES: dict[type, str] = {
    ContentScriptReview: "content_script_review.html",
    NarrationReview: "content_narration_review.html",
    FinalVideoReview: "content_final_video_review.html",
    ThumbnailReview: "content_thumbnail_review.html",
    OperationsBriefReview: "operations_brief_review.html",
}
_DASHBOARD_NOTICES = {
    "schedule-change-blocked": (
        "O schedule não foi habilitado. Verifique se o módulo está ativo, "
        "sem kill switch e possui executor local registrado."
    ),
}
_TEMPLATE_DIRECTORY = Path(__file__).resolve().parent / "templates"
_STATIC_DIRECTORY = Path(__file__).resolve().parent / "static"
templates = Jinja2Templates(directory=str(_TEMPLATE_DIRECTORY))
templates.env.filters["exact_decimal"] = format_exact_decimal
HealthCollector = Callable[[AsyncSession], Awaitable[HealthReport]]
RetentionCollector = Callable[[AsyncSession], Awaitable[RetentionInventory]]
DispatchPublisher = Callable[[int, str, QueueClass], None]


def _csrf_token_matches(submitted: str, expected: str) -> bool:
    """Compare arbitrary Unicode form input without leaking token timing."""
    return hmac.compare_digest(submitted.encode("utf-8"), expected.encode("utf-8"))


def _publish_dispatch_message(
    dispatch_id: int,
    delivery_id: str,
    queue: QueueClass,
) -> None:
    from src.core.celery_app import publish_dispatch_message

    publish_dispatch_message(dispatch_id, delivery_id, queue)


def _pin_static_media_types() -> None:
    """Serve scripts as text/javascript regardless of the operating system registry.

    The static file handler asks the platform for the type of each file. Windows answers
    from its registry, which reports text/javascript on some machines and
    application/javascript on others, so the same dashboard served different headers.
    """
    mimetypes.add_type("text/javascript", ".js")


def create_app(
    *,
    csrf_token: str | None = None,
    health_collector: HealthCollector = collect_health_report,
    retention_collector: RetentionCollector = collect_retention_inventory,
    dispatch_publisher: DispatchPublisher = _publish_dispatch_message,
) -> FastAPI:
    """Create the loopback dashboard with a process-local CSRF token."""
    _pin_static_media_types()
    control_token = csrf_token or secrets.token_urlsafe(32)
    if not control_token.strip():
        raise ValueError("csrf token must not be empty")
    application = FastAPI(
        title="Automation Foundry",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.mount(
        "/static",
        StaticFiles(directory=str(_STATIC_DIRECTORY)),
        name="static",
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
                "notice": _DASHBOARD_NOTICES.get(request.query_params.get("notice", "")),
            },
            headers={"Cache-Control": "no-store"},
        )

    @application.get("/data-observability/fragment", response_class=HTMLResponse)
    async def data_observability_fragment(
        request: Request,
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> HTMLResponse:
        connector_page = await load_connector_summaries(session)
        return templates.TemplateResponse(
            request=request,
            name="data_observability_fragment.html",
            context={
                "connectors": connector_page.items,
                "connectors_truncated": connector_page.truncated,
            },
            headers={"Cache-Control": "no-store"},
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
        alignment: CaptionAlignmentReportView | None = None
        alignment_unavailable = False
        try:
            alignment = await load_caption_alignment_report(session, run_id=run_id)
        except CaptionAlignmentError:
            # A tampered or unreadable report must never break the run page; the
            # operator sees a safe notice and re-runs the local diagnostic.
            alignment_unavailable = True
        return templates.TemplateResponse(
            request=request,
            name="run_detail.html",
            context={
                "run": detail,
                "csrf_token": control_token,
                "caption_alignment": alignment,
                "caption_alignment_unavailable": alignment_unavailable,
            },
            headers={"Cache-Control": "no-store"},
        )

    async def verified_local_export(
        session: AsyncSession,
        *,
        run_id: int,
    ) -> LocalExportPackage:
        run = await session.get(Run, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="run not found")
        automation = await session.get(Automation, run.automation_id)
        if automation is None or automation.slug != "content-engine":
            raise HTTPException(status_code=404, detail="local export not available")
        try:
            return await load_content_local_export(session, run=run)
        except LocalExportNotAvailableError as exc:
            raise HTTPException(
                status_code=404,
                detail="local export not available",
            ) from exc
        except LocalExportIntegrityError as exc:
            raise HTTPException(
                status_code=409,
                detail="local export evidence is unavailable",
            ) from exc

    @application.get("/runs/{run_id}/local-export", response_class=HTMLResponse)
    async def local_export_page(
        request: Request,
        run_id: int,
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> HTMLResponse:
        package = await verified_local_export(session, run_id=run_id)
        return templates.TemplateResponse(
            request=request,
            name="content_local_export.html",
            context={
                "run_id": package.run_id,
                "manifest_sha256": package.manifest_sha256,
                "video": {
                    "artifact_id": package.video.artifact_id,
                    "size_bytes": package.video.size_bytes,
                    "sha256": package.video.sha256,
                },
                "thumbnail": (
                    None
                    if package.thumbnail is None
                    else {
                        "artifact_id": package.thumbnail.artifact_id,
                        "size_bytes": package.thumbnail.size_bytes,
                        "sha256": package.thumbnail.sha256,
                    }
                ),
            },
            headers={"Cache-Control": "no-store"},
        )

    @application.get("/runs/{run_id}/local-export/manifest.json")
    async def local_export_manifest(
        run_id: int,
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> Response:
        package = await verified_local_export(session, run_id=run_id)
        return Response(
            content=package.manifest_bytes,
            media_type="application/json",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": (
                    f'attachment; filename="run-{run_id}-local-export.json"'
                ),
                "X-Content-Type-Options": "nosniff",
            },
        )

    @application.get("/runs/{run_id}/local-export/video")
    async def local_export_video(
        run_id: int,
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> FileResponse:
        package = await verified_local_export(session, run_id=run_id)
        return FileResponse(
            package.video.path,
            media_type=package.video.media_type,
            filename=package.video.filename,
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @application.get("/runs/{run_id}/local-export/thumbnail")
    async def local_export_thumbnail(
        run_id: int,
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> FileResponse:
        package = await verified_local_export(session, run_id=run_id)
        if package.thumbnail is None:
            raise HTTPException(status_code=404, detail="thumbnail not selected")
        return FileResponse(
            package.thumbnail.path,
            media_type=package.thumbnail.media_type,
            filename=package.thumbnail.filename,
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @application.get("/runs/{run_id}/fragment", response_class=HTMLResponse)
    async def run_status_fragment(
        request: Request,
        run_id: int,
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> HTMLResponse:
        detail = await load_run_detail(session, run_id=run_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="run not found")
        return templates.TemplateResponse(
            request=request,
            name="run_status_fragment.html",
            context={"run": detail},
            headers={"Cache-Control": "no-store"},
        )

    @application.get("/approvals/{approval_id}/review", response_class=HTMLResponse)
    async def approval_review(
        request: Request,
        approval_id: int,
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> HTMLResponse:
        approval = await session.get(Approval, approval_id)
        if approval is None:
            raise HTTPException(status_code=404, detail="approval not found")
        try:
            review = await load_registered_approval_review(
                session,
                approval=approval,
            )
        except AutomationExecutorNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail="approval has no complete review page",
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=409,
                detail="approval review evidence is unavailable",
            ) from exc
        template = _REVIEW_TEMPLATES.get(type(review))
        if template is None:
            raise HTTPException(
                status_code=404,
                detail="approval has no complete review page",
            )
        return templates.TemplateResponse(
            request=request,
            name=template,
            context={
                "review": review,
                "csrf_token": control_token,
                "approval_status": approval.status,
            },
            headers={"Cache-Control": "no-store"},
        )

    @application.get("/approvals/{approval_id}/final-video")
    async def approval_final_video(
        approval_id: int,
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> FileResponse:
        approval = await session.get(Approval, approval_id)
        if approval is None:
            raise HTTPException(status_code=404, detail="approval not found")
        try:
            review = await load_registered_approval_review(
                session,
                approval=approval,
            )
        except AutomationExecutorNotFoundError as exc:
            raise HTTPException(status_code=404, detail="approval has no video") from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=409,
                detail="approval review evidence is unavailable",
            ) from exc
        if not isinstance(review, FinalVideoReview):
            raise HTTPException(status_code=404, detail="approval has no video")
        return FileResponse(
            review.video_path,
            media_type="video/mp4",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": (
                    f'inline; filename="run-{review.run_id}-final-video.mp4"'
                ),
                "X-Content-Type-Options": "nosniff",
            },
        )

    @application.get("/approvals/{approval_id}/narration-audio")
    async def approval_narration_audio(
        approval_id: int,
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> FileResponse:
        approval = await session.get(Approval, approval_id)
        if approval is None:
            raise HTTPException(status_code=404, detail="approval not found")
        try:
            review = await load_registered_approval_review(
                session,
                approval=approval,
            )
        except AutomationExecutorNotFoundError as exc:
            raise HTTPException(status_code=404, detail="approval has no audio") from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=409,
                detail="approval review evidence is unavailable",
            ) from exc
        if not isinstance(review, NarrationReview):
            raise HTTPException(status_code=404, detail="approval has no audio")
        return FileResponse(
            review.audio_path,
            media_type="audio/wav",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": (
                    f'inline; filename="run-{review.run_id}-narration.wav"'
                ),
                "X-Content-Type-Options": "nosniff",
            },
        )

    @application.get("/approvals/{approval_id}/thumbnails/{artifact_id}")
    async def approval_thumbnail_candidate(
        approval_id: int,
        artifact_id: int,
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> FileResponse:
        approval = await session.get(Approval, approval_id)
        if approval is None:
            raise HTTPException(status_code=404, detail="approval not found")
        try:
            review = await load_registered_approval_review(
                session,
                approval=approval,
            )
        except AutomationExecutorNotFoundError as exc:
            raise HTTPException(
                status_code=404,
                detail="approval has no thumbnail candidates",
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=409,
                detail="approval review evidence is unavailable",
            ) from exc
        if not isinstance(review, ThumbnailReview):
            raise HTTPException(
                status_code=404,
                detail="approval has no thumbnail candidates",
            )
        candidate = next(
            (
                item
                for item in review.candidates
                if item.artifact_id == artifact_id
            ),
            None,
        )
        if candidate is None:
            raise HTTPException(status_code=404, detail="thumbnail candidate not found")
        return FileResponse(
            candidate.image_path,
            media_type="image/png",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": (
                    f'inline; filename="artifact-{candidate.artifact_id}-thumbnail.png"'
                ),
                "X-Content-Type-Options": "nosniff",
            },
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
            headers={"Cache-Control": "no-store"},
        )

    @application.get("/storage", response_class=HTMLResponse)
    async def storage_page(
        request: Request,
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> HTMLResponse:
        error: str | None = None
        inventory: RetentionInventory | None = None
        try:
            inventory = await retention_collector(session)
        except RetentionInventoryError:
            error = (
                "O inventário não pôde ser concluído com segurança. "
                "Verifique a configuração local de storage pela CLI."
            )
        return templates.TemplateResponse(
            request=request,
            name="storage.html",
            context={
                "inventory": inventory,
                "error": error,
                "retention_policies": declared_retention_policies(),
            },
            headers={"Cache-Control": "no-store"},
        )

    @application.post("/automations/{automation_slug}/runs", response_class=HTMLResponse)
    async def start_manual_run(
        request: Request,
        automation_slug: str,
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> RedirectResponse:
        try:
            executor = get_automation_executor(automation_slug)
        except AutomationExecutorNotFoundError as exc:
            raise HTTPException(status_code=404, detail="automation executor not found") from exc
        submitted_token, confirmation, idempotency_key, experiment_id, input_payload = (
            await _parse_manual_run_form(request, executor=executor)
        )
        if not _csrf_token_matches(submitted_token, control_token):
            raise HTTPException(status_code=403, detail="invalid csrf token")
        if confirmation != "start-example-run":
            raise HTTPException(status_code=400, detail="explicit confirmation required")
        try:
            if executor.runs_in_background:
                prepared = await publish_registered_dispatch(
                    session,
                    slug=automation_slug,
                    idempotency_key=idempotency_key,
                    experiment_id=experiment_id,
                    input_payload=input_payload,
                    publish=dispatch_publisher,
                )
                run_id = prepared.run_id
            else:
                execution = await start_registered_automation(
                    session,
                    slug=automation_slug,
                    idempotency_key=idempotency_key,
                    experiment_id=experiment_id,
                    input_payload=input_payload,
                )
                run_id = execution.run_id
            await session.commit()
        except DispatchPublishError as exc:
            raise HTTPException(status_code=503, detail="dispatch broker unavailable") from exc
        except ValueError as exc:
            await session.rollback()
            raise HTTPException(status_code=409, detail="automation run cannot start") from exc
        except Exception:
            await session.rollback()
            raise
        return RedirectResponse(url=f"/runs/{run_id}", status_code=303)

    @application.post("/runs/{run_id}/retry", response_class=HTMLResponse)
    async def retry_run(
        request: Request,
        run_id: int,
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> RedirectResponse:
        submitted_token, confirmation, idempotency_key, actor, reason = await _parse_retry_form(request)
        if not _csrf_token_matches(submitted_token, control_token):
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
            executor = get_automation_executor(automation_slug)
        except AutomationExecutorNotFoundError as exc:
            raise HTTPException(status_code=409, detail="automation cannot be retried") from exc
        try:
            if executor.runs_in_background:
                prepared = await publish_registered_dispatch(
                    session,
                    slug=automation_slug,
                    idempotency_key=idempotency_key,
                    publish=dispatch_publisher,
                    trigger="retry",
                    input_payload=run.input_payload,
                    retry_of_run_id=run.id,
                    retry_requested_by=actor,
                    retry_reason=reason,
                )
                retry_run_id = prepared.run_id
            else:
                execution = await retry_registered_automation(
                    session,
                    slug=automation_slug,
                    retry_of_run_id=run.id,
                    idempotency_key=idempotency_key,
                    actor=actor,
                    reason=reason,
                )
                retry_run_id = execution.run_id
            await session.commit()
        except DispatchPublishError as exc:
            raise HTTPException(status_code=503, detail="dispatch broker unavailable") from exc
        except ValueError as exc:
            await session.rollback()
            raise HTTPException(status_code=409, detail="run cannot be retried") from exc
        except Exception:
            await session.rollback()
            raise
        return RedirectResponse(url=f"/runs/{retry_run_id}", status_code=303)

    @application.post("/runs/{run_id}/cancel", response_class=HTMLResponse)
    async def cancel_run(
        request: Request,
        run_id: int,
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> RedirectResponse:
        submitted_token, confirmation, reason = await _parse_cancellation_form(request)
        if not _csrf_token_matches(submitted_token, control_token):
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
        if not _csrf_token_matches(submitted_token, control_token):
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
        if not _csrf_token_matches(submitted_token, control_token):
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

    @application.post("/automations/{automation_id}/enabled", response_class=HTMLResponse)
    async def change_automation_enabled(
        request: Request,
        automation_id: int,
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> RedirectResponse:
        submitted_token, confirmation, target, actor, reason = (
            await _parse_automation_enabled_form(request)
        )
        if not _csrf_token_matches(submitted_token, control_token):
            raise HTTPException(status_code=403, detail="invalid csrf token")
        enabled = target == "enable"
        if target not in {"enable", "disable"} or confirmation != f"automation-{target}":
            raise HTTPException(status_code=400, detail="explicit confirmation required")
        automation = await session.get(Automation, automation_id)
        if automation is None:
            raise HTTPException(status_code=404, detail="automation not found")
        try:
            await set_automation_enabled(
                session,
                automation=automation,
                enabled=enabled,
                actor=actor,
                reason=reason,
            )
            await session.commit()
        except ValueError as exc:
            await session.rollback()
            raise HTTPException(
                status_code=400,
                detail="invalid automation enabled change",
            ) from exc
        except Exception:
            await session.rollback()
            raise
        return RedirectResponse(url="/", status_code=303)

    @application.post("/schedules/{schedule_id}/enabled", response_class=HTMLResponse)
    async def change_schedule_enabled(
        request: Request,
        schedule_id: int,
        session: Annotated[AsyncSession, Depends(get_session)],
    ) -> RedirectResponse:
        submitted_token, confirmation, target, actor, reason = (
            await _parse_schedule_enabled_form(request)
        )
        if not _csrf_token_matches(submitted_token, control_token):
            raise HTTPException(status_code=403, detail="invalid csrf token")
        enabled = target == "enable"
        if target not in {"enable", "disable"} or confirmation != f"schedule-{target}":
            raise HTTPException(status_code=400, detail="explicit confirmation required")
        schedule = await session.get(Schedule, schedule_id)
        if schedule is None:
            raise HTTPException(status_code=404, detail="schedule not found")
        if enabled:
            automation = await session.get(Automation, schedule.automation_id)
            if automation is None:
                raise HTTPException(status_code=409, detail="schedule automation not found")
            try:
                get_automation_executor(automation.slug)
            except AutomationExecutorNotFoundError:
                return RedirectResponse(
                    url="/?notice=schedule-change-blocked",
                    status_code=303,
                )
        try:
            await set_schedule_enabled(
                session,
                schedule=schedule,
                enabled=enabled,
                actor=actor,
                reason=reason,
            )
            await session.commit()
        except ScheduleControlError:
            await session.rollback()
            return RedirectResponse(
                url="/?notice=schedule-change-blocked",
                status_code=303,
            )
        except ValueError as exc:
            await session.rollback()
            raise HTTPException(status_code=400, detail="invalid schedule enabled change") from exc
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
        approval = await session.get(Approval, approval_id)
        if approval is None:
            raise HTTPException(status_code=404, detail="approval not found")
        thumbnail_artifact_id: int | None = None
        if approval.action == THUMBNAIL_REVIEW_APPROVAL_ACTION:
            (
                submitted_token,
                confirmation,
                actor,
                reason,
                thumbnail_artifact_id,
            ) = await _parse_thumbnail_approval_form(request)
        else:
            submitted_token, confirmation, actor, reason = (
                await _parse_approval_decision_form(request)
            )
        if not _csrf_token_matches(submitted_token, control_token):
            raise HTTPException(status_code=403, detail="invalid csrf token")
        if confirmation != "approve-approval":
            raise HTTPException(status_code=400, detail="explicit confirmation required")
        run_id = approval.run_id
        try:
            decision_payload: dict[str, object] | None = None
            if thumbnail_artifact_id is not None:
                review = await load_registered_approval_review(
                    session,
                    approval=approval,
                )
                if not isinstance(review, ThumbnailReview):
                    raise ValueError("approval is not an A8 thumbnail review")
                decision_payload = thumbnail_decision_payload(
                    review,
                    thumbnail_artifact_id=thumbnail_artifact_id,
                )
            await decide_approval(
                session,
                approval=approval,
                decision=ApprovalStatus.APPROVED,
                actor=actor,
                reason=reason,
                decision_payload=decision_payload,
            )
            continuation_required = await finalize_registered_approval(
                session,
                approval=approval,
            )
            continuation_dispatch_id: int | None = None
            if continuation_required:
                requeued = await requeue_completed_run_dispatch(
                    session,
                    run_id=run_id,
                    actor=actor,
                )
                if requeued.should_publish:
                    continuation_dispatch_id = requeued.dispatch_id
            await session.commit()
            if continuation_dispatch_id is not None:
                await publish_prepared_dispatch(
                    session,
                    dispatch_id=continuation_dispatch_id,
                    publish=dispatch_publisher,
                    only_if_pending=True,
                )
        except DispatchPublishError as exc:
            raise HTTPException(
                status_code=503,
                detail="approval persisted; continuation broker unavailable",
            ) from exc
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


async def _parse_manual_run_form(
    request: Request,
    *,
    executor: AutomationExecutor,
) -> tuple[str, str, str, int | None, dict[str, object]]:
    fields = await _parse_form_fields(
        request,
        maximum_fields=4 + len(executor.manual_input_fields),
    )
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
    raw_input: dict[str, str] = {}
    for field in executor.manual_input_fields:
        if field.required:
            raw_input[field.name] = _single_form_value(
                fields,
                field.name,
                maximum_length=field.max_length,
            )
        else:
            raw_input[field.name] = (
                _optional_single_form_value(
                    fields,
                    field.name,
                    maximum_length=field.max_length,
                )
                or ""
            )
    allowed_fields = {
        "csrf_token",
        "confirmation",
        "idempotency_key",
        "experiment_id",
        *(field.name for field in executor.manual_input_fields),
    }
    if set(fields) - allowed_fields:
        raise HTTPException(status_code=400, detail="unexpected manual run field")
    try:
        input_payload = parse_registered_manual_input(
            slug=executor.slug,
            values=raw_input,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid automation input") from exc
    return csrf_token, confirmation, idempotency_key, experiment_id, input_payload


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


async def _parse_automation_enabled_form(
    request: Request,
) -> tuple[str, str, str, str, str]:
    return await _parse_kill_switch_form(request)


async def _parse_schedule_enabled_form(
    request: Request,
) -> tuple[str, str, str, str, str]:
    return await _parse_kill_switch_form(request)


async def _parse_approval_decision_form(
    request: Request,
) -> tuple[str, str, str, str]:
    return await _parse_approval_rejection_form(request)


async def _parse_thumbnail_approval_form(
    request: Request,
) -> tuple[str, str, str, str, int]:
    fields = await _parse_form_fields(request, maximum_fields=5)
    expected = {
        "csrf_token",
        "confirmation",
        "actor",
        "reason",
        "thumbnail_artifact_id",
    }
    if set(fields) != expected:
        raise HTTPException(status_code=400, detail="invalid thumbnail selection form")
    csrf_token = _single_form_value(fields, "csrf_token", maximum_length=128)
    confirmation = _single_form_value(fields, "confirmation", maximum_length=32)
    actor = _single_form_value(fields, "actor", maximum_length=200)
    reason = _single_form_value(fields, "reason", maximum_length=500)
    artifact_value = _single_form_value(
        fields,
        "thumbnail_artifact_id",
        maximum_length=20,
    )
    try:
        artifact_id = int(artifact_value)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="invalid thumbnail_artifact_id field",
        ) from exc
    if artifact_id < 1:
        raise HTTPException(
            status_code=400,
            detail="invalid thumbnail_artifact_id field",
        )
    return csrf_token, confirmation, actor, reason, artifact_id


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
