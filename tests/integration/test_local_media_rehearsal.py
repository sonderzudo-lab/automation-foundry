"""Opt-in A1 to A6 rehearsal with the pinned external FFmpeg toolchain.

This is the first execution that walks the whole Content Engine chain with real
local media instead of fake adapters: the approved A1 bundle, a declared
synthetic WAV fixture, the real `local_assets_quality_test` import, the real
`approved_text_timing_quality_test` alignment, a real FFmpeg render and the A6
originality gate. It publishes nothing and contacts no network service.

What it does NOT prove: narration quality, PT-BR voice licensing, rights over a
real visual package, and caption synchronism against a real voice. Those still
require Kokoro on the RTX 3090, reviewed assets and human review.

Run it with the same opt-in variables used by the A5 adapter test:

    $env:AUTOMATION_FOUNDRY_TEST_FFMPEG = "1"
    $env:CONTENT_FFMPEG_EXPECTED_SHA256 = "<sha256 do ffmpeg.exe fixado>"
    $env:CONTENT_FFPROBE_EXPECTED_SHA256 = "<sha256 do ffprobe.exe fixado>"
    # opcional, para inspecionar o MP4 depois do teste:
    $env:AUTOMATION_FOUNDRY_REHEARSAL_OUTPUT = "C:\\temp\\rehearsal"
    .venv\\Scripts\\python -m pytest tests/integration/test_local_media_rehearsal.py -s
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import AsyncGenerator
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.core.database import Base
from src.pipeline import a1_executor
from src.pipeline.a1_executor import execute_content_script_run
from src.platform.approval_service import decide_approval
from src.platform.models import (
    Approval,
    ApprovalStatus,
    Artifact,
    MetricPoint,
    Run,
    RunStatus,
    StepRun,
)
from tests.support.local_media_rehearsal import (
    SYNTHETIC_NARRATION_SECONDS,
    expected_visual_count,
    rehearsal_input_payload,
    rehearsal_script,
    rehearsal_settings,
    write_synthetic_narration_wav,
    write_synthetic_visual_import,
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


def _rehearsal_root(tmp_path: Path) -> Path:
    configured = os.environ.get("AUTOMATION_FOUNDRY_REHEARSAL_OUTPUT", "").strip()
    if not configured:
        return tmp_path
    root = Path(configured).resolve() / f"rehearsal-{uuid4().hex[:8]}"
    root.mkdir(parents=True, exist_ok=False)
    return root


@pytest.mark.skipif(
    os.environ.get("AUTOMATION_FOUNDRY_TEST_FFMPEG") != "1",
    reason="requires explicit opt-in to pinned external FFmpeg/ffprobe binaries",
)
async def test_local_media_rehearsal_renders_reviewable_mp4_through_a6(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ffmpeg = shutil.which(os.environ.get("CONTENT_FFMPEG_PATH", "ffmpeg"))
    ffprobe = shutil.which(os.environ.get("CONTENT_FFPROBE_PATH", "ffprobe"))
    ffmpeg_hash = os.environ.get("CONTENT_FFMPEG_EXPECTED_SHA256", "")
    ffprobe_hash = os.environ.get("CONTENT_FFPROBE_EXPECTED_SHA256", "")
    assert ffmpeg is not None and ffprobe is not None
    assert ffmpeg_hash and ffprobe_hash

    root = _rehearsal_root(tmp_path)
    storage_root = root / "storage"
    import_root = root / "imports" / "content-visuals"
    visual_count = expected_visual_count()
    write_synthetic_visual_import(
        import_root,
        count=visual_count,
        width=1080,
        height=1920,
    )
    imported_before = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(import_root.glob("*.png"))
    }
    monkeypatch.setattr(
        a1_executor,
        "settings",
        rehearsal_settings(
            storage_root,
            import_root=import_root,
            assembly_backend="ffmpeg_quality_test",
            ffmpeg_path=ffmpeg,
            ffprobe_path=ffprobe,
            ffmpeg_sha256=ffmpeg_hash,
            ffprobe_sha256=ffprobe_hash,
        ),
    )

    first = await execute_content_script_run(
        session,
        idempotency_key="content-local-media-rehearsal-ffmpeg",
        input_payload=rehearsal_input_payload(),
        script_generator=lambda *_args: rehearsal_script(),
        storage_root=storage_root,
    )
    approval = await session.get(Approval, first.approval_id)
    assert approval is not None
    await decide_approval(
        session,
        approval=approval,
        decision=ApprovalStatus.APPROVED,
        actor="local-owner",
        reason="roteiro integral revisado antes do ensaio local",
    )
    rehearsed = await execute_content_script_run(
        session,
        idempotency_key="content-local-media-rehearsal-ffmpeg",
        input_payload=rehearsal_input_payload(),
        script_generator=lambda *_args: pytest.fail("A1 must replay after approval"),
        tts_synthesizer=write_synthetic_narration_wav,
        storage_root=storage_root,
    )

    run = await session.get(Run, rehearsed.run_id)
    steps = list(
        (
            await session.scalars(
                select(StepRun)
                .where(StepRun.run_id == rehearsed.run_id)
                .order_by(StepRun.ordinal, StepRun.attempt)
            )
        ).all()
    )
    artifacts = {
        artifact.artifact_type: artifact
        for artifact in (
            await session.scalars(
                select(Artifact).where(Artifact.run_id == rehearsed.run_id)
            )
        ).all()
    }

    assert run is not None and run.status == RunStatus.SUCCEEDED.value
    assert [step.name for step in steps if step.status == "succeeded"] == [
        "generate-script-a1",
        "synthesize-tts-a2",
        "prepare-visuals-a3",
        "generate-captions-a4",
        "assemble-video-a5",
        "check-originality-a6",
    ]

    video_artifact = artifacts["final_video"]
    video_path = storage_root / video_artifact.relative_path
    assert video_path.is_file()
    assert video_artifact.size_bytes > 10_000
    assert (
        hashlib.sha256(video_path.read_bytes()).hexdigest() == video_artifact.sha256
    )

    report_artifact = artifacts["originality_report"]
    report = json.loads((storage_root / report_artifact.relative_path).read_text("utf-8"))
    assert report["blocked"] is False
    assert report["reference_count"] == 0

    metrics = {
        metric.name: metric
        for metric in (
            await session.scalars(
                select(MetricPoint).where(MetricPoint.run_id == rehearsed.run_id)
            )
        ).all()
    }
    duration = Decimal(str(metrics["content_assembly_video_duration_seconds"].value))
    assert abs(duration - SYNTHETIC_NARRATION_SECONDS) <= Decimal("0.50")

    imported_after = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(import_root.glob("*.png"))
    }
    assert imported_after == imported_before

    print(f"\nrehearsal MP4 para inspecao humana: {video_path}")
    print(f"rehearsal relatorio A6: {storage_root / report_artifact.relative_path}")
