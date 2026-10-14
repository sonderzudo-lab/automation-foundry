"""Content Engine readiness diagnostic: isolated, read-only, redacted probes."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import httpx
import pytest

from src.core.config import Settings
from src.pipeline import readiness, readiness_checks
from src.pipeline.readiness import (
    ReadinessReport,
    ReadinessStatus,
    collect_content_readiness,
)
from src.pipeline.readiness_checks import ProbeCode, ProbeOutcome

_DIGEST = "a" * 64
_SECRET_MARKER = "private-secret-marker"


def _settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def _ollama_transport(
    models: list[str] | None = None,
    *,
    status: int = 200,
    body: object | None = None,
    error: Exception | None = None,
    seen: list[httpx.Request] | None = None,
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if error is not None:
            raise error
        payload = body if body is not None else {
            "object": "list", "data": [{"id": name} for name in models or []],
        }
        if isinstance(payload, bytes):
            return httpx.Response(status, content=payload)
        return httpx.Response(status, json=payload)

    return httpx.MockTransport(handler)


def _by_id(report: ReadinessReport) -> dict[str, readiness.ReadinessCheck]:
    return {check.id: check for check in report.checks}


@pytest.fixture
def storage(tmp_path: Path) -> Path:
    root = tmp_path / "storage"
    root.mkdir()
    return root


# ── orchestration ─────────────────────────────────────────────────────────────


async def test_disabled_stages_are_not_applicable_and_never_probed(
    storage: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_a: object, **_k: object) -> ProbeOutcome:
        raise AssertionError("a disabled stage must not be probed")

    for name in (
        "probe_tts_packages", "probe_espeak", "probe_tts_cache", "probe_caption_packages",
        "probe_caption_model", "probe_media_tool", "probe_visual_import_config",
    ):
        monkeypatch.setattr(readiness_checks, name, forbidden)

    report = await collect_content_readiness(
        _settings(storage_root=str(storage)),
        transport=_ollama_transport(["qwen3:14b"]),
    )
    checks = _by_id(report)

    assert checks["ollama"].status is ReadinessStatus.READY
    assert checks["storage"].status is ReadinessStatus.READY
    for check_id in (
        "tts_packages", "tts_espeak", "tts_cache", "caption_packages", "caption_model",
        "ffmpeg", "ffprobe", "visual_config", "visual_inputs", "gpu_runtime",
    ):
        assert checks[check_id].status is ReadinessStatus.NOT_APPLICABLE, check_id
        assert checks[check_id].code is ProbeCode.DISABLED
        assert "desabilitad" in checks[check_id].guidance or "Nenhum backend" in (
            checks[check_id].guidance
        )
    assert not any(check.status is ReadinessStatus.PROBLEM for check in report.checks)


async def test_text_timing_captions_do_not_require_the_transcription_model(
    storage: Path,
) -> None:
    report = await collect_content_readiness(
        _settings(
            storage_root=str(storage),
            content_tts_backend="kokoro_quality_test",
            content_caption_backend="approved_text_timing_quality_test",
        ),
        transport=_ollama_transport(["qwen3:14b"]),
    )
    checks = _by_id(report)

    assert checks["caption_model"].status is ReadinessStatus.NOT_APPLICABLE
    assert "não usa o modelo" in checks["caption_model"].guidance
    assert checks["caption_packages"].status is ReadinessStatus.NOT_APPLICABLE


async def test_one_failing_probe_does_not_stop_the_others(
    storage: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def explode(*_a: object, **_k: object) -> ProbeOutcome:
        raise RuntimeError(f"C:\\Users\\{_SECRET_MARKER}\\broken")

    monkeypatch.setattr(readiness_checks, "probe_espeak", explode)
    monkeypatch.setattr(
        readiness_checks, "probe_tts_packages", lambda **_k: ProbeOutcome(ProbeCode.OK),
    )
    monkeypatch.setattr(
        readiness_checks, "probe_tts_cache", lambda **_k: ProbeOutcome(ProbeCode.OK),
    )

    report = await collect_content_readiness(
        _settings(storage_root=str(storage), content_tts_backend="kokoro_quality_test"),
        transport=_ollama_transport(["qwen3:14b"]),
    )
    checks = _by_id(report)

    assert checks["tts_espeak"].status is ReadinessStatus.NOT_CHECKED
    assert checks["tts_espeak"].code is ProbeCode.NOT_CHECKED
    assert checks["tts_packages"].status is ReadinessStatus.READY
    assert checks["tts_cache"].status is ReadinessStatus.READY
    assert checks["storage"].status is ReadinessStatus.READY
    assert checks["ollama"].status is ReadinessStatus.READY
    assert _SECRET_MARKER not in repr(report)


async def test_a_probe_that_returns_garbage_becomes_not_checked(
    storage: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(readiness_checks, "probe_storage", lambda *_a, **_k: "OK")

    report = await collect_content_readiness(
        _settings(storage_root=str(storage)), transport=_ollama_transport(["qwen3:14b"]),
    )

    assert _by_id(report)["storage"].code is ProbeCode.NOT_CHECKED


async def test_a_hung_probe_times_out_without_blocking_the_report(
    storage: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()

    def hang(*_a: object, **_k: object) -> ProbeOutcome:
        release.wait(5)
        return ProbeOutcome(ProbeCode.OK)

    monkeypatch.setattr(readiness_checks, "probe_espeak", hang)
    monkeypatch.setattr(
        readiness_checks, "probe_tts_packages", lambda **_k: ProbeOutcome(ProbeCode.OK),
    )
    monkeypatch.setattr(
        readiness_checks, "probe_tts_cache", lambda **_k: ProbeOutcome(ProbeCode.OK),
    )
    started = time.monotonic()
    try:
        report = await collect_content_readiness(
            _settings(storage_root=str(storage), content_tts_backend="kokoro_quality_test"),
            transport=_ollama_transport(["qwen3:14b"]),
            local_deadline_seconds=0.1,
        )
    finally:
        release.set()
    elapsed = time.monotonic() - started
    checks = _by_id(report)

    assert elapsed < 2
    assert checks["tts_espeak"].code is ProbeCode.TIMEOUT
    assert checks["tts_espeak"].status is ReadinessStatus.NOT_CHECKED
    assert "prazo" in checks["tts_espeak"].guidance
    assert checks["tts_packages"].status is ReadinessStatus.READY
    assert checks["storage"].status is ReadinessStatus.READY


async def test_deferred_inputs_are_not_problems_and_do_not_block_script_start(
    storage: Path,
) -> None:
    report = await collect_content_readiness(
        _settings(
            storage_root=str(storage),
            content_tts_backend="kokoro_quality_test",
            content_visual_backend="local_assets_quality_test",
            content_visual_import_root=str(storage / "imports"),
        ),
        transport=_ollama_transport(["qwen3:14b"]),
    )
    checks = _by_id(report)

    assert checks["visual_inputs"].status is ReadinessStatus.NOT_CHECKED
    assert checks["visual_inputs"].code is ProbeCode.DEFERRED
    assert "não impede iniciar o roteiro" in checks["visual_inputs"].guidance
    assert checks["visual_config"].status is ReadinessStatus.READY
    assert checks["gpu_runtime"].code is ProbeCode.DEFERRED
    # The script step's own dependencies are independent of the deferred inputs.
    assert checks["ollama"].status is ReadinessStatus.READY
    assert checks["storage"].status is ReadinessStatus.READY


async def test_generating_visual_backend_has_no_import_manifest_to_check(
    storage: Path,
) -> None:
    generating = Settings.model_construct(
        **{
            **_settings(storage_root=str(storage)).model_dump(),
            "content_tts_backend": "kokoro_quality_test",
            "content_visual_backend": "flux2_klein_quality_test",
        },
    )

    report = await collect_content_readiness(
        generating, transport=_ollama_transport(["qwen3:14b"]),
    )
    checks = _by_id(report)

    for check_id in ("visual_config", "visual_inputs"):
        assert checks[check_id].status is ReadinessStatus.NOT_APPLICABLE
        assert "conferidos apenas pela etapa A3" in checks[check_id].guidance
    # Without the image review gate the executor refuses this backend.
    assert checks["pipeline_config"].status is ReadinessStatus.PROBLEM
    assert "revisão das imagens" in checks["pipeline_config"].guidance


async def test_incoherent_stage_configuration_lists_each_conflict(storage: Path) -> None:
    # model_construct skips validators: the diagnostic must still guard on its own.
    incoherent = Settings.model_construct(
        **{
            **_settings(storage_root=str(storage)).model_dump(),
            "content_assembly_backend": "ffmpeg_quality_test",
            "content_thumbnail_review_enabled": True,
        },
    )

    report = await collect_content_readiness(
        incoherent, transport=_ollama_transport(["qwen3:14b"]),
    )
    pipeline = _by_id(report)["pipeline_config"]

    assert pipeline.status is ReadinessStatus.PROBLEM
    assert pipeline.code is ProbeCode.INVALID_CONFIG
    assert "narração habilitada" in pipeline.guidance
    assert "visuais e legendas" in pipeline.guidance
    assert "revisão final" in pipeline.guidance


async def test_coherent_configuration_is_ready(storage: Path) -> None:
    report = await collect_content_readiness(
        _settings(storage_root=str(storage)), transport=_ollama_transport(["qwen3:14b"]),
    )

    assert _by_id(report)["pipeline_config"].status is ReadinessStatus.READY


async def test_report_has_stable_codes_timestamps_and_no_paths(storage: Path) -> None:
    report = await collect_content_readiness(
        _settings(
            storage_root=str(storage),
            content_caption_backend="faster_whisper_small_quality_test",
            content_caption_model_path=str(storage / "no-such-model"),
            content_tts_backend="kokoro_quality_test",
        ),
        transport=_ollama_transport([]),
    )

    assert report.started_at <= report.completed_at
    for check in report.checks:
        assert check.result_code == f"CONTENT_READINESS_{check.id.upper()}_{check.code.value}"
        assert check.checked_at.tzinfo is not None
        assert str(storage) not in check.guidance
        assert "http" not in check.guidance
    assert _by_id(report)["ollama"].result_code == "CONTENT_READINESS_OLLAMA_MISSING"
    assert _by_id(report)["caption_model"].result_code == (
        "CONTENT_READINESS_CAPTION_MODEL_MISSING"
    )
    assert sum(report.counts.values()) == len(report.checks)


# ── ollama ────────────────────────────────────────────────────────────────────


async def test_ollama_lists_models_and_never_runs_inference(storage: Path) -> None:
    seen: list[httpx.Request] = []

    report = await collect_content_readiness(
        _settings(storage_root=str(storage)),
        transport=_ollama_transport(["qwen3:14b", "other:1b"], seen=seen),
    )

    assert _by_id(report)["ollama"].status is ReadinessStatus.READY
    assert [(request.method, request.url.path) for request in seen] == [("GET", "/v1/models")]


async def test_ollama_without_the_configured_model_is_a_problem(storage: Path) -> None:
    report = await collect_content_readiness(
        _settings(storage_root=str(storage), ollama_model="qwen3:14b"),
        transport=_ollama_transport(["llama3:8b"]),
    )
    ollama = _by_id(report)["ollama"]

    assert ollama.status is ReadinessStatus.PROBLEM
    assert ollama.code is ProbeCode.MISSING
    assert "OLLAMA_MODEL" in ollama.guidance


async def test_untagged_model_matches_latest(storage: Path) -> None:
    report = await collect_content_readiness(
        _settings(storage_root=str(storage), ollama_model="mymodel"),
        transport=_ollama_transport(["mymodel:latest"]),
    )

    assert _by_id(report)["ollama"].status is ReadinessStatus.READY


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"error": httpx.ConnectError("refused http://127.0.0.1:11434")}, ProbeCode.UNAVAILABLE),
        ({"error": httpx.ReadTimeout("slow")}, ProbeCode.TIMEOUT),
        ({"status": 500, "body": {"error": "boom"}}, ProbeCode.INCOMPATIBLE),
        ({"body": b"not json"}, ProbeCode.INCOMPATIBLE),
        ({"body": {"data": "nope"}}, ProbeCode.INCOMPATIBLE),
        ({"body": ["unexpected"]}, ProbeCode.INCOMPATIBLE),
    ],
)
async def test_ollama_failures_are_classified_and_redacted(
    storage: Path, kwargs: dict[str, Any], code: ProbeCode,
) -> None:
    report = await collect_content_readiness(
        _settings(storage_root=str(storage)), transport=_ollama_transport(**kwargs),
    )
    ollama = _by_id(report)["ollama"]

    assert ollama.code is code
    assert ollama.status is (
        ReadinessStatus.NOT_CHECKED if code is ProbeCode.TIMEOUT else ReadinessStatus.PROBLEM
    )
    assert "127.0.0.1" not in ollama.guidance and "refused" not in ollama.guidance


@pytest.mark.parametrize(
    "url",
    [
        "ftp://127.0.0.1/v1",
        "http://user:pass@127.0.0.1:11434/v1",
        "http://example.com/v1",
        "http://127.0.0.1:11434/v1?x=1",
        "http://127.0.0.1:99999/v1",
        "not a url",
    ],
)
async def test_invalid_ollama_endpoint_is_a_configuration_problem(
    storage: Path, url: str,
) -> None:
    seen: list[httpx.Request] = []
    report = await collect_content_readiness(
        _settings(storage_root=str(storage), ollama_base_url=url),
        transport=_ollama_transport(["qwen3:14b"], seen=seen),
    )
    ollama = _by_id(report)["ollama"]

    assert ollama.code is ProbeCode.INVALID_CONFIG
    assert ollama.status is ReadinessStatus.PROBLEM
    assert seen == []


async def test_ollama_deadline_bounds_a_slow_server(storage: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    report = await collect_content_readiness(
        _settings(storage_root=str(storage)),
        transport=httpx.MockTransport(handler),
        ollama_deadline_seconds=0.5,
    )

    assert _by_id(report)["ollama"].code is ProbeCode.TIMEOUT


# ── storage ───────────────────────────────────────────────────────────────────


def test_storage_ready_reports_free_space(tmp_path: Path) -> None:
    outcome = readiness_checks.probe_storage(str(tmp_path))

    assert outcome.code is ProbeCode.OK
    assert isinstance(outcome.free_bytes, int) and outcome.free_bytes > 0


def test_storage_missing_root(tmp_path: Path) -> None:
    assert readiness_checks.probe_storage(str(tmp_path / "gone")).code is ProbeCode.MISSING


def test_storage_blank_or_file_root_is_invalid_config(tmp_path: Path) -> None:
    file_path = tmp_path / "file"
    file_path.write_text("x")

    assert readiness_checks.probe_storage("  ").code is ProbeCode.INVALID_CONFIG
    assert readiness_checks.probe_storage(str(file_path)).code is ProbeCode.INVALID_CONFIG


def test_storage_low_space_keeps_the_measured_value(tmp_path: Path) -> None:
    outcome = readiness_checks.probe_storage(str(tmp_path), min_free_bytes=2**62)

    assert outcome.code is ProbeCode.LOW_SPACE
    assert outcome.free_bytes is not None


def test_storage_without_access(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "access", lambda *_a, **_k: False)

    assert readiness_checks.probe_storage(str(tmp_path)).code is ProbeCode.UNAVAILABLE


def test_storage_listing_failure_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def deny(*_a: object, **_k: object) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(os, "scandir", deny)

    assert readiness_checks.probe_storage(str(tmp_path)).code is ProbeCode.UNAVAILABLE


# ── narration ─────────────────────────────────────────────────────────────────


def _fake_versions(monkeypatch: pytest.MonkeyPatch, versions: dict[str, str]) -> None:
    def version(name: str) -> str:
        if name not in versions:
            raise importlib.metadata.PackageNotFoundError(name)
        return versions[name]

    monkeypatch.setattr(importlib.metadata, "version", version)


def test_tts_packages_missing_mismatched_and_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_versions(monkeypatch, {})
    assert readiness_checks.probe_tts_packages(kokoro_version="0.9.4").code is ProbeCode.MISSING

    _fake_versions(monkeypatch, {"kokoro": "0.9.3"})
    assert (
        readiness_checks.probe_tts_packages(kokoro_version="0.9.4").code is ProbeCode.INCOMPATIBLE
    )

    _fake_versions(monkeypatch, {"kokoro": "0.9.4"})
    monkeypatch.setattr(importlib.util, "find_spec", lambda _n: object())
    assert readiness_checks.probe_tts_packages(kokoro_version="0.9.4").code is ProbeCode.OK


def test_tts_packages_missing_module(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_versions(monkeypatch, {"kokoro": "0.9.4"})
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name: None if name == "torch" else object(),
    )

    assert readiness_checks.probe_tts_packages(kokoro_version="0.9.4").code is ProbeCode.MISSING


def test_tts_packages_does_not_require_a_distribution_named_phonemizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the dependency ships as ``phonemizer-fork``, not ``phonemizer``."""
    _fake_versions(monkeypatch, {"kokoro": "0.9.4", "phonemizer-fork": "3.3.2"})
    monkeypatch.setattr(importlib.util, "find_spec", lambda _n: object())

    assert readiness_checks.probe_tts_packages(kokoro_version="0.9.4").code is ProbeCode.OK


def test_espeak_presence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _n: None)
    assert readiness_checks.probe_espeak().code is ProbeCode.MISSING

    monkeypatch.setattr(shutil, "which", lambda _n: "/usr/bin/espeak-ng")
    assert readiness_checks.probe_espeak().code is ProbeCode.OK


def _cache_probe(**overrides: Any) -> ProbeOutcome:
    arguments: dict[str, Any] = {
        "voice_id": "pm_alex",
        "allowed_voices": ("pf_dora", "pm_alex"),
        "repository": "org/repo",
        "revision": "abc",
    }
    arguments.update(overrides)
    return readiness_checks.probe_tts_cache(**arguments)


def _patch_cache(
    monkeypatch: pytest.MonkeyPatch, resolver: Callable[[str], object],
) -> None:
    # A stand-in module: CI installs only the "dev" extra, without huggingface_hub.
    fake_hub = ModuleType("huggingface_hub")
    fake_hub.try_to_load_from_cache = (  # type: ignore[attr-defined]
        lambda _repo, filename, revision=None: resolver(filename)
    )
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_hub)


def test_tts_cache_without_huggingface_hub_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "huggingface_hub", None)

    assert _cache_probe().code is ProbeCode.MISSING


def test_tts_cache_complete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def resolve(filename: str) -> str:
        target = tmp_path / filename.replace("/", "_")
        target.write_bytes(b"x")
        return str(target)

    _patch_cache(monkeypatch, resolve)

    assert _cache_probe().code is ProbeCode.OK


def test_tts_cache_absent_is_deferred_because_the_step_downloads_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    _patch_cache(monkeypatch, lambda _f: None)

    assert _cache_probe().code is ProbeCode.DEFERRED


def test_tts_cache_absent_offline_is_a_problem(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    _patch_cache(monkeypatch, lambda _f: None)

    assert _cache_probe().code is ProbeCode.MISSING


def test_tts_cache_partial_and_stale_entries_are_not_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    present = tmp_path / "config.json"
    present.write_bytes(b"x")
    _patch_cache(
        monkeypatch,
        lambda name: str(present) if name == "config.json" else str(tmp_path / "gone.pth"),
    )

    assert _cache_probe().code is ProbeCode.DEFERRED


def test_tts_cache_rejects_an_unreviewed_voice_without_touching_the_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(_f: str) -> object:
        raise AssertionError("cache must not be consulted for an unreviewed voice")

    _patch_cache(monkeypatch, forbidden)

    assert _cache_probe(voice_id="../etc/passwd").code is ProbeCode.INVALID_CONFIG


# ── captions ──────────────────────────────────────────────────────────────────


def test_caption_packages(monkeypatch: pytest.MonkeyPatch) -> None:
    arguments = {"faster_whisper_version": "1.2.1", "ctranslate2_version": "4.8.1"}

    _fake_versions(monkeypatch, {})
    assert readiness_checks.probe_caption_packages(**arguments).code is ProbeCode.MISSING

    _fake_versions(monkeypatch, {"faster-whisper": "1.2.0", "ctranslate2": "4.8.1"})
    assert readiness_checks.probe_caption_packages(**arguments).code is ProbeCode.INCOMPATIBLE

    _fake_versions(monkeypatch, {"faster-whisper": "1.2.1"})
    assert readiness_checks.probe_caption_packages(**arguments).code is ProbeCode.MISSING

    _fake_versions(monkeypatch, {"faster-whisper": "1.2.1", "ctranslate2": "4.8.1"})
    assert readiness_checks.probe_caption_packages(**arguments).code is ProbeCode.OK


_EXPECTED = (("config.json", 4), ("model.bin", 8), ("vocabulary.txt", 2))


def _snapshot(root: Path, *, skip: str | None = None, wrong: str | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name, size in _EXPECTED:
        if name == skip:
            continue
        (root / name).write_bytes(b"x" * (size + 1 if name == wrong else size))
    return root


def test_caption_model_complete(tmp_path: Path) -> None:
    root = _snapshot(tmp_path / "model")

    assert readiness_checks.probe_caption_model(str(root), _EXPECTED).code is ProbeCode.OK


def test_caption_model_absent_empty_or_partial(tmp_path: Path) -> None:
    assert (
        readiness_checks.probe_caption_model(str(tmp_path / "none"), _EXPECTED).code
        is ProbeCode.MISSING
    )
    empty = tmp_path / "empty"
    empty.mkdir()
    assert readiness_checks.probe_caption_model(str(empty), _EXPECTED).code is ProbeCode.MISSING

    partial = _snapshot(tmp_path / "partial", skip="model.bin")
    assert (
        readiness_checks.probe_caption_model(str(partial), _EXPECTED).code
        is ProbeCode.INCOMPLETE
    )


def test_caption_model_with_a_truncated_file_is_incomplete(tmp_path: Path) -> None:
    root = _snapshot(tmp_path / "model", wrong="model.bin")

    assert readiness_checks.probe_caption_model(str(root), _EXPECTED).code is (
        ProbeCode.INCOMPLETE
    )


def test_caption_model_blank_or_file_path_is_invalid_config(tmp_path: Path) -> None:
    file_path = tmp_path / "model.bin"
    file_path.write_bytes(b"x")

    assert readiness_checks.probe_caption_model(" ", _EXPECTED).code is ProbeCode.INVALID_CONFIG
    assert (
        readiness_checks.probe_caption_model(str(file_path), _EXPECTED).code
        is ProbeCode.INVALID_CONFIG
    )


def test_caption_model_uses_the_adapters_pinned_file_list(tmp_path: Path) -> None:
    from src.pipeline.captions import caption_snapshot_file_sizes

    expected = caption_snapshot_file_sizes()
    root = tmp_path / "snapshot"
    root.mkdir()
    for name, _size in expected:
        (root / name).write_bytes(b"wrong-size")

    assert {name for name, _ in expected} == {
        "config.json", "model.bin", "tokenizer.json", "vocabulary.txt",
    }
    assert readiness_checks.probe_caption_model(str(root), expected).code is ProbeCode.INCOMPLETE


async def test_caption_model_missing_surfaces_as_problem_in_the_report(
    storage: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        readiness_checks, "probe_caption_packages", lambda **_k: ProbeOutcome(ProbeCode.OK),
    )
    report = await collect_content_readiness(
        _settings(
            storage_root=str(storage),
            content_caption_backend="faster_whisper_small_quality_test",
            content_caption_model_path=str(storage / "absent-snapshot"),
            content_tts_backend="kokoro_quality_test",
        ),
        transport=_ollama_transport(["qwen3:14b"]),
    )
    model = _by_id(report)["caption_model"]

    assert model.status is ReadinessStatus.PROBLEM
    assert model.code is ProbeCode.MISSING
    assert "CONTENT_CAPTION_MODEL_PATH" in model.guidance
    assert str(storage) not in model.guidance


# ── ffmpeg / ffprobe ──────────────────────────────────────────────────────────

_FFMPEG_FLAGS = ("--enable-gpl", "--enable-version3", "--enable-libx264")


@pytest.fixture
def fake_tool(tmp_path: Path) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    path = tmp_path / f"tool{suffix}"
    path.write_bytes(b"binary")
    path.chmod(0o755)
    return path


def _patch_run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stdout: bytes = b"",
    returncode: int = 0,
    error: Exception | None = None,
    calls: list[list[str]] | None = None,
) -> None:
    def fake_run(command: list[str], **_kwargs: Any) -> SimpleNamespace:
        if calls is not None:
            calls.append(command)
        if error is not None:
            raise error
        return SimpleNamespace(stdout=stdout, returncode=returncode)

    monkeypatch.setattr(subprocess, "run", fake_run)


def _media(path: Path | str, digest: str | None = _DIGEST, **kwargs: Any) -> ProbeOutcome:
    return readiness_checks.probe_media_tool(
        tool=kwargs.pop("tool", "ffmpeg"),
        configured_path=str(path),
        expected_sha256=digest,
        required_flags=kwargs.pop("required_flags", _FFMPEG_FLAGS),
    )


def test_media_tool_ready_only_runs_version(
    fake_tool: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    _patch_run(
        monkeypatch,
        stdout=b"ffmpeg version 8.0.1\nconfiguration: --enable-gpl --enable-version3 "
        b"--enable-libx264",
        calls=calls,
    )

    assert _media(fake_tool).code is ProbeCode.OK
    assert [command[1:] for command in calls] == [["-hide_banner", "-version"]]


def test_media_tool_missing_flags_or_wrong_program_is_incompatible(
    fake_tool: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_run(monkeypatch, stdout=b"ffmpeg version 8.0.1\n--enable-gpl")
    assert _media(fake_tool).code is ProbeCode.INCOMPATIBLE

    _patch_run(monkeypatch, stdout=b"something else v1")
    assert _media(fake_tool, required_flags=()).code is ProbeCode.INCOMPATIBLE

    _patch_run(monkeypatch, stdout=b"ffmpeg version 8.0.1", returncode=1)
    assert _media(fake_tool, required_flags=()).code is ProbeCode.INCOMPATIBLE


def test_ffprobe_is_identified_by_its_own_banner(
    fake_tool: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_run(monkeypatch, stdout=b"ffprobe version 8.0.1 Copyright")

    assert _media(fake_tool, tool="ffprobe", required_flags=()).code is ProbeCode.OK
    assert _media(fake_tool, tool="ffmpeg", required_flags=()).code is ProbeCode.INCOMPATIBLE


def test_media_tool_missing_executable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_run(monkeypatch, error=AssertionError("must not execute a missing tool"))

    assert _media(tmp_path / "nonexistent-ffmpeg").code is ProbeCode.MISSING


@pytest.mark.parametrize("digest", [None, "", "abc", "g" * 64, "a" * 63])
def test_media_tool_invalid_pin_never_executes(
    fake_tool: Path, monkeypatch: pytest.MonkeyPatch, digest: str | None,
) -> None:
    _patch_run(monkeypatch, error=AssertionError("an invalid pin must not execute anything"))

    assert _media(fake_tool, digest=digest).code is ProbeCode.INVALID_CONFIG


def test_media_tool_blank_path_is_invalid_config(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_run(monkeypatch, error=AssertionError("must not execute"))

    assert _media("   ").code is ProbeCode.INVALID_CONFIG


def test_media_tool_timeout_and_os_error(
    fake_tool: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_run(monkeypatch, error=subprocess.TimeoutExpired(cmd="x", timeout=3))
    assert _media(fake_tool).code is ProbeCode.TIMEOUT

    _patch_run(monkeypatch, error=PermissionError("denied"))
    assert _media(fake_tool).code is ProbeCode.UNAVAILABLE


async def test_ffmpeg_absent_is_a_problem_but_ffprobe_is_still_checked(
    storage: Path, fake_tool: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_run(monkeypatch, stdout=b"ffprobe version 8.0.1")
    report = await collect_content_readiness(
        _settings(
            storage_root=str(storage),
            content_tts_backend="kokoro_quality_test",
            content_visual_backend="local_assets_quality_test",
            content_caption_backend="approved_text_timing_quality_test",
            content_assembly_backend="ffmpeg_quality_test",
            content_ffmpeg_path=str(storage / "missing-ffmpeg"),
            content_ffprobe_path=str(fake_tool),
            content_ffmpeg_expected_sha256=_DIGEST,
            content_ffprobe_expected_sha256=_DIGEST,
        ),
        transport=_ollama_transport(["qwen3:14b"]),
    )
    checks = _by_id(report)

    assert checks["ffmpeg"].status is ReadinessStatus.PROBLEM
    assert checks["ffmpeg"].code is ProbeCode.MISSING
    assert checks["ffprobe"].status is ReadinessStatus.READY
    assert "CONTENT_FFMPEG_PATH" in checks["ffmpeg"].guidance
    assert str(storage) not in checks["ffmpeg"].guidance


async def test_unpinned_ffmpeg_hash_is_reported_as_invalid_config(
    storage: Path, fake_tool: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_run(monkeypatch, error=AssertionError("must not execute"))
    incomplete = Settings.model_construct(
        **{
            **_settings(storage_root=str(storage)).model_dump(),
            "content_assembly_backend": "ffmpeg_quality_test",
            "content_ffmpeg_path": str(fake_tool),
            "content_ffprobe_path": str(fake_tool),
            "content_ffmpeg_expected_sha256": None,
            "content_ffprobe_expected_sha256": "not-a-digest",
        },
    )

    report = await collect_content_readiness(
        incomplete, transport=_ollama_transport(["qwen3:14b"]),
    )
    checks = _by_id(report)

    assert checks["ffmpeg"].code is ProbeCode.INVALID_CONFIG
    assert checks["ffprobe"].code is ProbeCode.INVALID_CONFIG
    assert "CONTENT_FFMPEG_EXPECTED_SHA256" in checks["ffmpeg"].guidance


# ── visual import configuration ───────────────────────────────────────────────


def test_visual_configuration_valid_even_when_inputs_do_not_exist_yet(tmp_path: Path) -> None:
    outcome = readiness_checks.probe_visual_import_config(
        str(tmp_path / "imports" / "visuals"), "manifest.json",
    )

    assert outcome.code is ProbeCode.OK


def test_visual_configuration_accepts_an_absolute_manifest_inside_the_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "imports"
    root.mkdir()

    assert (
        readiness_checks.probe_visual_import_config(str(root), str(root / "sub" / "m.json")).code
        is ProbeCode.OK
    )


@pytest.mark.parametrize(
    "case",
    ["blank_root", "blank_manifest", "anchor_root", "escapes_root", "manifest_is_root"],
)
def test_visual_configuration_rejects_unsafe_values(tmp_path: Path, case: str) -> None:
    root = tmp_path / "imports"
    root.mkdir()
    arguments = {
        "blank_root": (" ", "manifest.json"),
        "blank_manifest": (str(root), "  "),
        "anchor_root": (str(Path(tmp_path.anchor)), "manifest.json"),
        "escapes_root": (str(root), "../outside.json"),
        "manifest_is_root": (str(root), "."),
    }[case]

    outcome = readiness_checks.probe_visual_import_config(*arguments)

    assert outcome.code is ProbeCode.INVALID_CONFIG


def test_visual_configuration_rejects_a_file_as_root_or_a_directory_as_manifest(
    tmp_path: Path,
) -> None:
    file_root = tmp_path / "root-file"
    file_root.write_text("x")
    root = tmp_path / "imports"
    (root / "manifest.json").mkdir(parents=True)

    assert (
        readiness_checks.probe_visual_import_config(str(file_root), "manifest.json").code
        is ProbeCode.INVALID_CONFIG
    )
    assert (
        readiness_checks.probe_visual_import_config(str(root), "manifest.json").code
        is ProbeCode.INVALID_CONFIG
    )


async def test_invalid_visual_configuration_is_a_problem_while_inputs_stay_deferred(
    storage: Path,
) -> None:
    report = await collect_content_readiness(
        _settings(
            storage_root=str(storage),
            content_tts_backend="kokoro_quality_test",
            content_visual_backend="local_assets_quality_test",
            content_visual_import_root=str(storage),
            content_visual_manifest_path="../escape.json",
        ),
        transport=_ollama_transport(["qwen3:14b"]),
    )
    checks = _by_id(report)

    assert checks["visual_config"].status is ReadinessStatus.PROBLEM
    assert checks["visual_config"].code is ProbeCode.INVALID_CONFIG
    assert checks["visual_inputs"].code is ProbeCode.DEFERRED
    assert json.dumps(checks["visual_config"].guidance).count(str(storage)) == 0
