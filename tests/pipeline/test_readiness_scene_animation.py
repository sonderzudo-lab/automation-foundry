"""Readiness of the A5a scene animation: ComfyUI, weights, RAM and Remotion."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from src.core.config import Settings
from src.pipeline import readiness, readiness_checks
from src.pipeline.readiness import ReadinessStatus, collect_content_readiness
from src.pipeline.readiness_checks import ProbeCode

_VERSION = "9.9.9"
_PACKAGES = ("remotion", "@remotion/cli")
_SIZES = {"checkpoints/a.safetensors": 10, "text_encoders/b.safetensors": 20}
_IDS = ("scene_comfyui", "scene_weights", "scene_ram", "scene_remotion")


def _comfyui(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "ComfyUI"
    root.mkdir()
    (root / "main.py").write_text("", encoding="utf-8")
    (root / "comfyui_version.py").write_text('__version__ = "0.40.0"\n', encoding="utf-8")
    python = tmp_path / "venv-python.exe"
    python.write_text("", encoding="utf-8")
    return root, python


def _weights(root: Path, sizes: dict[str, int] | None = None) -> Path:
    models = root / "models"
    for relative, size in (sizes or _SIZES).items():
        target = models / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * size)
    return models


def _remotion(tmp_path: Path, *, lock_version: str = _VERSION, browser: bool = True) -> Path:
    root = tmp_path / "remotion"
    (root / "src").mkdir(parents=True)
    (root / "src" / "index.ts").write_text("", encoding="utf-8")
    modules = root / "node_modules"
    for name in _PACKAGES:
        (modules / name).mkdir(parents=True)
        (modules / name / "package.json").write_text(
            json.dumps({"version": _VERSION}), encoding="utf-8",
        )
    (modules / "@remotion" / "cli" / "remotion-cli.js").write_text("", encoding="utf-8")
    if browser:
        shell = modules / ".remotion" / "chrome-headless-shell" / "win64"
        shell.mkdir(parents=True)
        (shell / "chrome-headless-shell.exe").write_text("", encoding="utf-8")
    lock = {"packages": {f"node_modules/{name}": {"version": lock_version} for name in _PACKAGES}}
    (root / "package-lock.json").write_text(json.dumps(lock), encoding="utf-8")
    return root


def test_comfyui_install_codes(tmp_path: Path) -> None:
    root, python = _comfyui(tmp_path)
    probe = readiness_checks.probe_comfyui_install
    assert probe(str(root), str(python), expected_version="0.40.0").code is ProbeCode.OK
    assert probe(str(root), str(python), expected_version="0.41.0").code is ProbeCode.INCOMPATIBLE
    assert probe("", str(python), expected_version="0.40.0").code is ProbeCode.INVALID_CONFIG
    missing = str(tmp_path / "gone" / "python.exe")
    assert probe(str(root), missing, expected_version="0.40.0").code is ProbeCode.MISSING
    (root / "main.py").unlink()
    assert probe(str(root), str(python), expected_version="0.40.0").code is ProbeCode.MISSING


def test_weights_codes(tmp_path: Path) -> None:
    root, _ = _comfyui(tmp_path)
    models = _weights(root)
    expected = tuple(sorted(_SIZES.items()))
    probe = readiness_checks.probe_model_weights
    assert probe(str(models), expected).code is ProbeCode.OK
    (models / "text_encoders" / "b.safetensors").write_bytes(b"x" * 19)
    assert probe(str(models), expected).code is ProbeCode.INCOMPLETE
    (models / "text_encoders" / "b.safetensors").unlink()
    assert probe(str(models), expected).code is ProbeCode.INCOMPLETE
    (models / "checkpoints" / "a.safetensors").unlink()
    assert probe(str(models), expected).code is ProbeCode.MISSING
    assert probe(str(tmp_path / "nope"), expected).code is ProbeCode.MISSING
    assert probe("", expected).code is ProbeCode.INVALID_CONFIG


def test_free_ram_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    import psutil

    monkeypatch.setattr(
        psutil, "virtual_memory", lambda: type("M", (), {"available": 8 * 1024**3})(),
    )
    assert readiness_checks.probe_free_ram(8 * 1024**3).code is ProbeCode.OK
    assert readiness_checks.probe_free_ram(8 * 1024**3 + 1).code is ProbeCode.LOW_RAM


def test_remotion_codes(tmp_path: Path) -> None:
    probe = readiness_checks.probe_remotion_project
    good = _remotion(tmp_path)
    assert probe(str(good), version=_VERSION, packages=_PACKAGES).code is ProbeCode.OK
    assert probe(str(good), version="1.0.0", packages=_PACKAGES).code is ProbeCode.INCOMPATIBLE
    assert (
        probe(str(tmp_path / "absent"), version=_VERSION, packages=_PACKAGES).code
        is ProbeCode.MISSING
    )
    other = tmp_path / "second"
    other.mkdir()
    mispinned = _remotion(other, lock_version="4.0.1")
    assert probe(str(mispinned), version=_VERSION, packages=_PACKAGES).code is (
        ProbeCode.INCOMPATIBLE
    )
    third = tmp_path / "third"
    third.mkdir()
    no_modules = _remotion(third)
    for child in sorted((no_modules / "node_modules").rglob("*"), reverse=True):
        child.unlink() if child.is_file() else child.rmdir()
    (no_modules / "node_modules").rmdir()
    assert probe(str(no_modules), version=_VERSION, packages=_PACKAGES).code is ProbeCode.MISSING
    fourth = tmp_path / "fourth"
    fourth.mkdir()
    no_browser = _remotion(fourth, browser=False)
    assert probe(str(no_browser), version=_VERSION, packages=_PACKAGES).code is (
        ProbeCode.INCOMPLETE
    )


def _collect(**overrides: Any) -> dict[str, readiness.ReadinessCheck]:
    config = Settings(_env_file=None, **overrides)  # type: ignore[call-arg]
    report = asyncio.run(collect_content_readiness(config))
    return {check.id: check for check in report.checks}


def test_disabled_backend_reports_not_applicable() -> None:
    checks = _collect()
    for check_id in _IDS:
        assert checks[check_id].status is ReadinessStatus.NOT_APPLICABLE
        assert checks[check_id].code is ProbeCode.DISABLED


def test_enabled_backend_ready_and_problems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import psutil

    root, python = _comfyui(tmp_path)
    _weights(root)
    project = _remotion(tmp_path)
    monkeypatch.setattr(readiness, "LTX23_MODEL_BYTES", _SIZES)
    monkeypatch.setattr(readiness, "REMOTION_PROJECT_DIR", project)
    monkeypatch.setattr(readiness, "REMOTION_VERSION", _VERSION)
    monkeypatch.setattr(
        psutil, "virtual_memory", lambda: type("M", (), {"available": 64 * 1024**3})(),
    )
    settings: dict[str, Any] = {
        "content_scene_animation_backend": "ltx23_quality_test",
        "content_scene_animation_comfyui_dir": str(root),
        "content_scene_animation_python_path": str(python),
        "content_scene_animation_min_free_ram_gb": 40,
    }
    checks = _collect(**settings)
    assert {i: checks[i].status for i in _IDS} == dict.fromkeys(_IDS, ReadinessStatus.READY)
    assert "NÃO foram conferidos" in checks["scene_weights"].guidance

    # Low RAM, a wrong-size weight and a missing venv are each their own problem.
    monkeypatch.setattr(
        psutil, "virtual_memory", lambda: type("M", (), {"available": 4 * 1024**3})(),
    )
    (root / "models" / "checkpoints" / "a.safetensors").write_bytes(b"x")
    python.unlink()
    checks = _collect(**settings)
    assert checks["scene_ram"].code is ProbeCode.LOW_RAM
    assert checks["scene_weights"].code is ProbeCode.INCOMPLETE
    assert checks["scene_comfyui"].code is ProbeCode.MISSING
    assert checks["scene_remotion"].status is ReadinessStatus.READY
    for check_id in ("scene_ram", "scene_weights", "scene_comfyui"):
        assert checks[check_id].status is ReadinessStatus.PROBLEM
        assert str(tmp_path) not in checks[check_id].guidance
