"""Content Engine A3 visual contract with fake local adapters."""

from __future__ import annotations

import hashlib
import json
import struct
import wave
import zlib
from collections.abc import AsyncGenerator
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.core.database import Base
from src.pipeline import a1_executor, visuals
from src.pipeline.a1_executor import execute_content_script_run
from src.pipeline.script_gen import ScriptResult
from src.pipeline.tts import TTSAdapterResult, execute_approved_tts_step
from src.pipeline.visuals import (
    VisualAssetResult,
    VisualPermanentAdapterError,
    VisualRequest,
    VisualRetryableAdapterError,
    execute_approved_visual_step,
    get_configured_visual_adapter,
)
from src.platform.approval_service import decide_approval
from src.platform.models import (
    Approval,
    ApprovalStatus,
    Artifact,
    LedgerEntry,
    MetricPoint,
    QueueClass,
    Run,
    RunStatus,
    StepRun,
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


def _script() -> ScriptResult:
    return ScriptResult(
        angle="Dormir reorganiza memórias.",
        outline="1. Gancho\n2. Explicação\n3. Fecho",
        full_script=(
            "Durante o sono, o cérebro reorganiza conexões importantes. "
            "Memórias recentes são estabilizadas e detalhes irrelevantes perdem força. "
            "Esse processo ajuda a aprender e tomar decisões no dia seguinte."
        ),
        hook="Seu cérebro edita memórias enquanto você dorme.",
        narration="O sono reorganiza memórias e fortalece o aprendizado.",
    )


async def _running_after_tts(
    session: AsyncSession,
    storage_root: Path,
    *,
    idempotency_key: str,
    audio_seconds: int,
) -> tuple[Run, Approval, Artifact, Artifact, dict[str, object]]:
    a1 = await execute_content_script_run(
        session,
        idempotency_key=idempotency_key,
        input_payload={
            "topic": "Sono e memória",
            "persona": "Ciência acessível",
            "format": "short",
            "recent_openings": [],
        },
        script_generator=lambda *_args: _script(),
        storage_root=storage_root,
    )
    run = await session.get(Run, a1.run_id)
    approval = await session.get(Approval, a1.approval_id)
    script_artifact = await session.get(Artifact, a1.artifact_id)
    assert run is not None and approval is not None and script_artifact is not None
    await decide_approval(
        session,
        approval=approval,
        decision=ApprovalStatus.APPROVED,
        actor="local-owner",
        reason="roteiro integral revisado",
    )
    approval_input = {
        "schema_version": 1,
        "run_id": run.id,
        "step_run_id": a1.step_run_id,
        "artifact_id": script_artifact.id,
        "artifact_sha256": script_artifact.sha256,
        "narration_sha256": hashlib.sha256(
            _script().narration.encode("utf-8")
        ).hexdigest(),
    }

    def synthesize(
        _text: str,
        destination: Path,
        voice_id: str,
        language_code: str,
    ) -> TTSAdapterResult:
        with wave.open(str(destination), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(24_000)
            audio.writeframes(b"\x00\x00" * 24_000 * audio_seconds)
        return TTSAdapterResult(
            backend="fake-local",
            model_id="fake-tts-v1",
            voice_id=voice_id,
            language_code=language_code,
            license_id="TEST-ONLY",
        )

    tts_result = await execute_approved_tts_step(
        session,
        run=run,
        approval=approval,
        approval_input_payload=approval_input,
        source_artifact=script_artifact,
        narration=_script().narration,
        idempotency_key=idempotency_key,
        storage_root=storage_root,
        synthesizer=synthesize,
        voice_id="pf_test",
        language_code="p",
        gpu_power_watts=400,
        timeout_seconds=5,
        finalize_run=False,
    )
    audio_artifact = await session.get(Artifact, tts_result.artifact_id)
    assert audio_artifact is not None
    assert run.status == RunStatus.RUNNING.value
    return run, approval, script_artifact, audio_artifact, approval_input


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def _write_png(path: Path, *, width: int, height: int) -> None:
    row = b"\x00" + (b"\x22\x44\x66" * width)
    encoded = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(
            b"IHDR",
            struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0),
        )
        + _png_chunk(b"IDAT", zlib.compress(row * height, level=9))
        + _png_chunk(b"IEND", b"")
    )
    path.write_bytes(encoded)


def _fake_visual_adapter(
    requests: tuple[VisualRequest, ...],
    destination: Path,
) -> tuple[VisualAssetResult, ...]:
    results: list[VisualAssetResult] = []
    for request in requests:
        file_name = f"visual-{request.index:03d}.png"
        _write_png(
            destination / file_name,
            width=request.width,
            height=request.height,
        )
        results.append(
            VisualAssetResult(
                file_name=file_name,
                source_type="generated",
                provider="fake-local",
                model_id="fake-image-model-v1",
                source_id=f"request-{request.index}",
                source_uri=f"local-model://fake-image-model-v1/{request.index}",
                license_id="TEST-ONLY",
                commercial_use=True,
                attribution="test fixture",
            )
        )
    return tuple(results)


def _write_local_asset_manifest(
    path: Path,
    assets: list[Path],
    *,
    commercial_use: bool = True,
) -> None:
    payload = {
        "schema_version": 1,
        "assets": [
            {
                "relative_path": asset.name,
                "sha256": hashlib.sha256(asset.read_bytes()).hexdigest(),
                "source_type": "owned",
                "provider": "local-owner",
                "model_id": "not-applicable",
                "source_id": f"owned-{index}",
                "source_uri": f"local-asset://operator/owned-{index}",
                "license_id": "OWNER-ATTESTED",
                "commercial_use": commercial_use,
                "attribution": "Acervo próprio do operador",
            }
            for index, asset in enumerate(assets, start=1)
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize(
    ("backend", "expected_code"),
    [
        ("disabled", "CONTENT_VISUALS_DISABLED"),
        ("flux", "CONTENT_VISUALS_BACKEND_UNREVIEWED"),
        ("flux_schnell", "CONTENT_VISUALS_BACKEND_UNREVIEWED"),
        ("flux_schnell_quality_test", "CONTENT_VISUALS_BACKEND_UNREVIEWED"),
        ("flux_dev", "CONTENT_VISUALS_BACKEND_UNREVIEWED"),
        ("local_assets_quality_test", "CONTENT_VISUALS_IMPORT_CONFIG_REQUIRED"),
    ],
)
def test_configured_visual_backend_fails_closed(
    tmp_path: Path,
    backend: str,
    expected_code: str,
) -> None:
    with pytest.raises(VisualPermanentAdapterError, match=expected_code):
        get_configured_visual_adapter(backend)(
            (VisualRequest(index=1, prompt="safe prompt", width=1080, height=1920),),
            tmp_path,
        )


def test_local_asset_adapter_imports_exact_hash_pinned_pngs(tmp_path: Path) -> None:
    import_root = tmp_path / "imports"
    destination = tmp_path / "destination"
    import_root.mkdir()
    destination.mkdir()
    sources = [import_root / "first.png", import_root / "second.png"]
    for source in sources:
        _write_png(source, width=1080, height=1920)
    manifest = import_root / "manifest.json"
    _write_local_asset_manifest(manifest, sources)
    requests = tuple(
        VisualRequest(index=index, prompt=f"prompt {index}", width=1080, height=1920)
        for index in range(1, 3)
    )

    adapter = get_configured_visual_adapter(
        "local_assets_quality_test",
        import_root=import_root,
        manifest_path=Path("manifest.json"),
    )
    results = adapter(requests, destination)

    assert [result.source_type for result in results] == ["owned", "owned"]
    assert [result.source_id for result in results] == ["owned-1", "owned-2"]
    assert (destination / "visual-001.png").read_bytes() == sources[0].read_bytes()
    assert (destination / "visual-002.png").read_bytes() == sources[1].read_bytes()


def test_local_asset_backend_routes_a3_to_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        a1_executor,
        "settings",
        SimpleNamespace(content_visual_backend="local_assets_quality_test"),
    )

    assert a1_executor._visuals_queue() is QueueClass.IO


def test_local_asset_adapter_rejects_unlicensed_or_changed_input(
    tmp_path: Path,
) -> None:
    import_root = tmp_path / "imports"
    destination = tmp_path / "destination"
    import_root.mkdir()
    destination.mkdir()
    source = import_root / "source.png"
    _write_png(source, width=1080, height=1920)
    manifest = import_root / "manifest.json"
    request = (VisualRequest(index=1, prompt="prompt", width=1080, height=1920),)

    _write_local_asset_manifest(manifest, [source], commercial_use=False)
    adapter = get_configured_visual_adapter(
        "local_assets_quality_test",
        import_root=import_root,
        manifest_path=manifest,
    )
    with pytest.raises(
        VisualPermanentAdapterError,
        match="CONTENT_VISUALS_COMMERCIAL_USE_BLOCKED",
    ):
        adapter(request, destination)

    _write_local_asset_manifest(manifest, [source])
    source.write_bytes(source.read_bytes() + b"changed")
    with pytest.raises(
        VisualPermanentAdapterError,
        match="CONTENT_VISUALS_LOCAL_ASSET_HASH_MISMATCH",
    ):
        adapter(request, destination)


def test_local_asset_adapter_contains_manifest_and_asset_paths(tmp_path: Path) -> None:
    import_root = tmp_path / "imports"
    import_root.mkdir()
    outside = tmp_path / "outside.png"
    _write_png(outside, width=1080, height=1920)
    outside_manifest = tmp_path / "manifest.json"
    _write_local_asset_manifest(outside_manifest, [outside])

    with pytest.raises(
        VisualPermanentAdapterError,
        match="CONTENT_VISUALS_MANIFEST_OUTSIDE_IMPORT_ROOT",
    ):
        get_configured_visual_adapter(
            "local_assets_quality_test",
            import_root=import_root,
            manifest_path=outside_manifest,
        )

    payload = json.loads(outside_manifest.read_text(encoding="utf-8"))
    payload["assets"][0]["relative_path"] = "../outside.png"
    contained_manifest = import_root / "manifest.json"
    contained_manifest.write_text(json.dumps(payload), encoding="utf-8")
    adapter = get_configured_visual_adapter(
        "local_assets_quality_test",
        import_root=import_root,
        manifest_path=contained_manifest,
    )
    with pytest.raises(
        VisualPermanentAdapterError,
        match="CONTENT_VISUALS_INVALID_LOCAL_PATH",
    ):
        adapter(
            (VisualRequest(index=1, prompt="prompt", width=1080, height=1920),),
            tmp_path / "destination",
        )


async def test_local_asset_backend_completes_a3_on_io_without_gpu_energy(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    run, approval, script_artifact, audio_artifact, approval_input = (
        await _running_after_tts(
            session,
            storage_root,
            idempotency_key="content-local-assets-a3",
            audio_seconds=1,
        )
    )
    import_root = tmp_path / "imports"
    import_root.mkdir()
    source = import_root / "owned.png"
    _write_png(source, width=1080, height=1920)
    _write_local_asset_manifest(import_root / "manifest.json", [source])
    adapter = get_configured_visual_adapter(
        "local_assets_quality_test",
        import_root=import_root,
        manifest_path=Path("manifest.json"),
    )

    result = await execute_approved_visual_step(
        session,
        run=run,
        approval=approval,
        approval_input_payload=approval_input,
        script_artifact=script_artifact,
        audio_artifact=audio_artifact,
        idempotency_key="content-local-assets-a3",
        storage_root=storage_root,
        adapter=adapter,
        queue=QueueClass.IO,
        gpu_power_watts=400,
        timeout_seconds=5,
    )

    step = await session.get(StepRun, result.step_run_id)
    metrics = (
        await session.scalars(
            select(MetricPoint).where(MetricPoint.run_id == result.run_id)
        )
    ).all()
    assert result.run_status == RunStatus.SUCCEEDED.value
    assert step is not None and step.queue == QueueClass.IO.value
    assert "content_visual_asset_count" in {metric.name for metric in metrics}
    assert "content_visual_energy_estimate_kwh" not in {
        metric.name for metric in metrics
    }


async def test_a1_continuation_runs_a2_then_a3_before_completing(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_root = tmp_path / "storage"
    monkeypatch.setattr(
        a1_executor,
        "settings",
        SimpleNamespace(
            storage_root=str(storage_root),
            ollama_model="fake-local-model",
            celery_soft_time_limit_seconds=300,
            content_tts_backend="test",
            content_tts_voice_id="pf_test",
            content_tts_language_code="p",
            content_visual_backend="test",
            gpu_power_watts=400,
        ),
    )
    first = await execute_content_script_run(
        session,
        idempotency_key="content-a1-a2-a3-chain",
        input_payload={
            "topic": "Sono e memória",
            "persona": "Ciência acessível",
            "format": "short",
            "recent_openings": [],
        },
        script_generator=lambda *_args: _script(),
        storage_root=storage_root,
    )
    approval = await session.get(Approval, first.approval_id)
    assert approval is not None
    await decide_approval(
        session,
        approval=approval,
        decision=ApprovalStatus.APPROVED,
        actor="local-owner",
        reason="roteiro integral revisado",
    )

    def synthesize(
        _text: str,
        destination: Path,
        voice_id: str,
        language_code: str,
    ) -> TTSAdapterResult:
        with wave.open(str(destination), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(24_000)
            audio.writeframes(b"\x00\x00" * 24_000)
        return TTSAdapterResult(
            backend="fake-local",
            model_id="fake-tts-v1",
            voice_id=voice_id,
            language_code=language_code,
            license_id="TEST-ONLY",
        )

    continued = await execute_content_script_run(
        session,
        idempotency_key="content-a1-a2-a3-chain",
        input_payload={
            "topic": "Sono e memória",
            "persona": "Ciência acessível",
            "format": "short",
            "recent_openings": [],
        },
        script_generator=lambda *_args: pytest.fail("A1 must replay"),
        tts_synthesizer=synthesize,
        visual_adapter=_fake_visual_adapter,
        storage_root=storage_root,
    )
    run = await session.get(Run, continued.run_id)
    steps = list(
        (
            await session.scalars(
                select(StepRun)
                .where(StepRun.run_id == continued.run_id)
                .order_by(StepRun.ordinal, StepRun.attempt)
            )
        ).all()
    )

    assert run is not None and run.status == RunStatus.SUCCEEDED.value
    assert [(step.ordinal, step.name) for step in steps] == [
        (1, "generate-script-a1"),
        (2, "synthesize-tts-a2"),
        (3, "prepare-visuals-a3"),
    ]
    assert run.output_payload is not None
    assert len(run.output_payload["visual_artifact_ids"]) == 1


async def test_a3_creates_proportional_licensed_manifest_without_changing_audio(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    run, approval, script_artifact, audio_artifact, approval_input = (
        await _running_after_tts(
            session,
            storage_root,
            idempotency_key="content-a3-success",
            audio_seconds=13,
        )
    )
    audio_path = storage_root / audio_artifact.relative_path
    audio_digest_before = hashlib.sha256(audio_path.read_bytes()).hexdigest()

    result = await execute_approved_visual_step(
        session,
        run=run,
        approval=approval,
        approval_input_payload=approval_input,
        script_artifact=script_artifact,
        audio_artifact=audio_artifact,
        idempotency_key=run.idempotency_key,
        storage_root=storage_root,
        adapter=_fake_visual_adapter,
        queue=QueueClass.GPU,
        gpu_power_watts=400,
        timeout_seconds=10,
    )

    step = await session.get(StepRun, result.step_run_id)
    manifest_artifact = await session.get(Artifact, result.manifest_artifact_id)
    visual_artifacts = [
        await session.get(Artifact, artifact_id)
        for artifact_id in result.visual_artifact_ids
    ]
    metrics = list(
        (
            await session.scalars(
                select(MetricPoint).where(MetricPoint.source == "content-engine:a3")
            )
        ).all()
    )
    ledger = list(
        (
            await session.scalars(
                select(LedgerEntry).where(LedgerEntry.source == "content-engine:a3")
            )
        ).all()
    )

    assert run.status == RunStatus.SUCCEEDED.value
    assert step is not None and step.status == RunStatus.SUCCEEDED.value
    assert step.queue == "gpu" and step.ordinal == 3
    assert manifest_artifact is not None
    assert manifest_artifact.artifact_type == "visual_manifest"
    assert len(visual_artifacts) == 3
    assert all(item is not None for item in visual_artifacts)
    manifest = json.loads(
        (storage_root / manifest_artifact.relative_path).read_text(encoding="utf-8")
    )
    assert manifest["visual_count"] == 3
    assert manifest["audio_artifact_sha256"] == audio_artifact.sha256
    assert all(asset["commercial_use"] is True for asset in manifest["assets"])
    assert all(asset["license_id"] == "TEST-ONLY" for asset in manifest["assets"])
    assert {metric.name for metric in metrics} == {
        "content_visual_asset_count",
        "content_visual_seconds_per_asset",
        "content_visual_energy_estimate_kwh",
    }
    assert len(ledger) == 1 and str(ledger[0].amount) == "0E-10"
    assert hashlib.sha256(audio_path.read_bytes()).hexdigest() == audio_digest_before
    assert run.output_payload is not None
    assert run.output_payload["visual_artifact_ids"] == list(result.visual_artifact_ids)
    assert not list((storage_root / "content-engine").rglob("*.tmp"))


async def test_a3_blocks_asset_without_commercial_rights(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    run, approval, script_artifact, audio_artifact, approval_input = (
        await _running_after_tts(
            session,
            storage_root,
            idempotency_key="content-a3-license-block",
            audio_seconds=1,
        )
    )

    def blocked_adapter(
        requests: tuple[VisualRequest, ...],
        destination: Path,
    ) -> tuple[VisualAssetResult, ...]:
        valid = _fake_visual_adapter(requests, destination)[0]
        return (
            VisualAssetResult(
                file_name=valid.file_name,
                source_type=valid.source_type,
                provider=valid.provider,
                model_id=valid.model_id,
                source_id=valid.source_id,
                source_uri=valid.source_uri,
                license_id="NON-COMMERCIAL",
                commercial_use=False,
                attribution=valid.attribution,
            ),
        )

    result = await execute_approved_visual_step(
        session,
        run=run,
        approval=approval,
        approval_input_payload=approval_input,
        script_artifact=script_artifact,
        audio_artifact=audio_artifact,
        idempotency_key=run.idempotency_key,
        storage_root=storage_root,
        adapter=blocked_adapter,
        queue=QueueClass.GPU,
        gpu_power_watts=400,
        timeout_seconds=10,
    )

    assert result.run_status == RunStatus.FAILED.value
    assert run.error is not None
    assert run.error["code"] == "CONTENT_VISUALS_COMMERCIAL_USE_BLOCKED"
    assert not list(storage_root.rglob("visual-*.png"))


async def test_a3_rejects_truncated_png_without_registering_visual_artifact(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    run, approval, script_artifact, audio_artifact, approval_input = (
        await _running_after_tts(
            session,
            storage_root,
            idempotency_key="content-a3-invalid-png",
            audio_seconds=1,
        )
    )

    def invalid_adapter(
        _requests: tuple[VisualRequest, ...],
        destination: Path,
    ) -> tuple[VisualAssetResult, ...]:
        (destination / "visual-001.png").write_bytes(b"not-a-png")
        return (
            VisualAssetResult(
                file_name="visual-001.png",
                source_type="generated",
                provider="fake-local",
                model_id="fake-image-model-v1",
                source_id="request-1",
                source_uri="local-model://fake-image-model-v1/1",
                license_id="TEST-ONLY",
                commercial_use=True,
                attribution="test fixture",
            ),
        )

    result = await execute_approved_visual_step(
        session,
        run=run,
        approval=approval,
        approval_input_payload=approval_input,
        script_artifact=script_artifact,
        audio_artifact=audio_artifact,
        idempotency_key=run.idempotency_key,
        storage_root=storage_root,
        adapter=invalid_adapter,
        queue=QueueClass.GPU,
        gpu_power_watts=400,
        timeout_seconds=10,
    )

    visual_artifacts = list(
        (
            await session.scalars(
                select(Artifact).where(Artifact.artifact_type == "visual_image")
            )
        ).all()
    )
    assert result.run_status == RunStatus.FAILED.value
    assert run.error is not None
    assert run.error["code"] == "CONTENT_VISUALS_INVALID_IMAGE_SIZE"
    assert visual_artifacts == []
    assert not list(storage_root.rglob("visual-*.png"))


async def test_a3_retries_transient_adapter_without_duplicate_assets(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    run, approval, script_artifact, audio_artifact, approval_input = (
        await _running_after_tts(
            session,
            storage_root,
            idempotency_key="content-a3-retry",
            audio_seconds=1,
        )
    )
    calls = 0

    def flaky(
        requests: tuple[VisualRequest, ...],
        destination: Path,
    ) -> tuple[VisualAssetResult, ...]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise VisualRetryableAdapterError("temporary provider failure")
        return _fake_visual_adapter(requests, destination)

    async def no_sleep(_seconds: float) -> None:
        return None

    result = await execute_approved_visual_step(
        session,
        run=run,
        approval=approval,
        approval_input_payload=approval_input,
        script_artifact=script_artifact,
        audio_artifact=audio_artifact,
        idempotency_key=run.idempotency_key,
        storage_root=storage_root,
        adapter=flaky,
        queue=QueueClass.GPU,
        gpu_power_watts=400,
        timeout_seconds=10,
        sleep=no_sleep,
    )

    attempts = list(
        (
            await session.scalars(
                select(StepRun)
                .where(StepRun.name == "prepare-visuals-a3")
                .order_by(StepRun.attempt)
            )
        ).all()
    )
    assert calls == 2
    assert result.run_status == RunStatus.SUCCEEDED.value
    assert [attempt.status for attempt in attempts] == ["failed", "succeeded"]
    assert len(list(storage_root.rglob("visual-*.png"))) == 1


async def test_a3_refuses_tampered_audio_before_contacting_adapter(
    session: AsyncSession,
    tmp_path: Path,
) -> None:
    storage_root = tmp_path / "storage"
    run, approval, script_artifact, audio_artifact, approval_input = (
        await _running_after_tts(
            session,
            storage_root,
            idempotency_key="content-a3-tampered-audio",
            audio_seconds=1,
        )
    )
    (storage_root / audio_artifact.relative_path).write_bytes(b"changed")
    called = False

    def must_not_run(
        _requests: tuple[VisualRequest, ...],
        _destination: Path,
    ) -> tuple[VisualAssetResult, ...]:
        nonlocal called
        called = True
        raise AssertionError("adapter must not run")

    with pytest.raises(ValueError, match="size|checksum"):
        await execute_approved_visual_step(
            session,
            run=run,
            approval=approval,
            approval_input_payload=approval_input,
            script_artifact=script_artifact,
            audio_artifact=audio_artifact,
            idempotency_key=run.idempotency_key,
            storage_root=storage_root,
            adapter=must_not_run,
            queue=QueueClass.GPU,
            gpu_power_watts=400,
            timeout_seconds=10,
        )

    assert called is False
    assert run.status == RunStatus.RUNNING.value


async def test_a3_evidence_failure_is_retryable_and_redacted(
    session: AsyncSession,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_root = tmp_path / "storage"
    run, approval, script_artifact, audio_artifact, approval_input = (
        await _running_after_tts(
            session,
            storage_root,
            idempotency_key="content-a3-evidence-failure",
            audio_seconds=1,
        )
    )

    async def fail_registration(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("private filesystem detail")

    monkeypatch.setattr(visuals, "register_local_artifact", fail_registration)
    result = await execute_approved_visual_step(
        session,
        run=run,
        approval=approval,
        approval_input_payload=approval_input,
        script_artifact=script_artifact,
        audio_artifact=audio_artifact,
        idempotency_key=run.idempotency_key,
        storage_root=storage_root,
        adapter=_fake_visual_adapter,
        queue=QueueClass.GPU,
        gpu_power_watts=400,
        timeout_seconds=10,
    )

    assert result.run_status == RunStatus.FAILED.value
    assert run.error == {
        "code": "CONTENT_VISUALS_EVIDENCE_PERSIST_FAILED",
        "exception_type": "RuntimeError",
        "retryable": True,
        "timed_out": False,
    }
    assert "private filesystem detail" not in str(run.error)
    assert len(list(storage_root.rglob("visual-*.png"))) == 1
