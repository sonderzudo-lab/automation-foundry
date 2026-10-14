"""FLUX.2 klein quality-test adapter with a fake runner and a fake model directory."""

from __future__ import annotations

import hashlib
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from src.pipeline import visual_generation
from src.pipeline.visual_generation import (
    FLUX2_KLEIN_MODEL_REVISION,
    Flux2KleinQualityTestConfig,
    build_flux2_klein_quality_test_adapter,
)
from src.pipeline.visual_prompts import VISUAL_STYLES
from src.pipeline.visuals import (
    VisualAdapter,
    VisualPermanentAdapterError,
    VisualRequest,
    VisualRetryableAdapterError,
)

_SCENES = (
    "A wooden desk seen from above with only a pencil, scissors and blank paper in the center",
    "A small ceramic tray on the edge of a desk holding pens and paper clips",
)
# Stands in for the real runner: writes a valid PNG of the requested size per request.
_FAKE_RUNNER = '''
import argparse, json, struct, sys, zlib
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--model-dir")
parser.add_argument("--requests-file")
parser.add_argument("--output-dir")
arguments = parser.parse_args()
plan = json.loads(Path(arguments.requests_file).read_text(encoding="utf-8"))
if any("FAIL" in image["prompt"] for image in plan["images"]):
    sys.exit(3)


def chunk(kind, data):
    return (
        struct.pack(">I", len(data)) + kind + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


for image in plan["images"]:
    width, height = image["width"], image["height"]
    row = b"\\x00" + b"\\x22\\x44\\x66" * width
    Path(arguments.output_dir, image["file_name"]).write_bytes(
        b"\\x89PNG\\r\\n\\x1a\\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(row * height))
        + chunk(b"IEND", b"")
    )
'''


def _requests() -> tuple[VisualRequest, ...]:
    return (
        VisualRequest(1, "segment one", 16, 32, narration_text="Pega só o que vai usar."),
        VisualRequest(2, "segment two", 16, 32, narration_text="Põe tudo num pote."),
    )


@pytest.fixture
def environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Flux2KleinQualityTestConfig, Path]:
    """A model directory matching a small pinned inventory, and the fake runner."""
    model_dir = tmp_path / "model"
    files = {"model_index.json": b"{}", "vae/config.json": b'{"vae": true}'}
    for relative_path, content in files.items():
        (model_dir / relative_path).parent.mkdir(parents=True, exist_ok=True)
        (model_dir / relative_path).write_bytes(content)
    (model_dir / "README.md").write_text("ignored", encoding="utf-8")
    (model_dir / ".cache").mkdir()
    (model_dir / ".cache" / "download.metadata").write_text("ignored", encoding="utf-8")
    monkeypatch.setattr(
        visual_generation,
        "FLUX2_KLEIN_MODEL_SHA256",
        {path: hashlib.sha256(content).hexdigest() for path, content in files.items()},
    )
    runner = tmp_path / "fake_runner.py"
    runner.write_text(_FAKE_RUNNER, encoding="utf-8")
    monkeypatch.setattr(visual_generation, "_RUNNER", runner)
    destination = tmp_path / "bundle" / "visuals.tmp"
    destination.mkdir(parents=True)
    return (
        Flux2KleinQualityTestConfig(
            python_path=sys.executable, model_dir=str(model_dir), timeout_seconds=60
        ),
        destination,
    )


def _adapter(
    config: Flux2KleinQualityTestConfig,
    scenes: tuple[str, ...] = _SCENES,
    *,
    release_scene_model: Callable[[], None] | None = None,
) -> VisualAdapter:
    answers = iter(scenes)
    return build_flux2_klein_quality_test_adapter(
        config,
        scene_model=lambda _system, _user: next(answers),
        topic="Mesa organizada",
        style="cartoon",
        release_scene_model=release_scene_model,
    )


def test_adapter_generates_one_non_commercial_image_per_stretch(
    environment: tuple[Flux2KleinQualityTestConfig, Path],
) -> None:
    config, destination = environment
    released: list[int] = []

    results = _adapter(
        config,
        release_scene_model=lambda: released.append(len(list(destination.glob("*.png")))),
    )(_requests(), destination)

    assert [result.file_name for result in results] == ["visual-001.png", "visual-002.png"]
    assert sorted(path.name for path in destination.iterdir()) == [
        "visual-001.png",
        "visual-002.png",
    ]
    # The scene model was released before any image existed.
    assert released == [0]
    first = results[0]
    assert first.commercial_use is False
    assert first.source_type == "generated"
    assert "scope=quality-test-only" in first.license_id
    assert "training-data=undisclosed" in first.license_id
    assert FLUX2_KLEIN_MODEL_REVISION in first.model_id
    assert first.generation_prompt == f"{_SCENES[0]}, {VISUAL_STYLES['cartoon']}"
    assert first.seed is not None and 0 <= first.seed < 2**32
    assert first.source_uri.startswith("local-model://")
    assert not list(destination.parent.glob("flux2-klein-*"))


def test_adapter_uses_the_same_seed_for_the_same_content(
    environment: tuple[Flux2KleinQualityTestConfig, Path], tmp_path: Path
) -> None:
    config, destination = environment
    again = tmp_path / "bundle" / "again.tmp"
    again.mkdir()
    first = _adapter(config)(_requests(), destination)
    second = _adapter(config)(_requests(), again)
    assert [result.seed for result in first] == [result.seed for result in second]
    assert first[0].seed != first[1].seed


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("extra", "CONTENT_VISUALS_FLUX2_MODEL_INVENTORY_MISMATCH"),
        ("missing", "CONTENT_VISUALS_FLUX2_MODEL_INVENTORY_MISMATCH"),
        ("changed", "CONTENT_VISUALS_FLUX2_MODEL_HASH_MISMATCH"),
        ("absent", "CONTENT_VISUALS_FLUX2_MODEL_UNAVAILABLE"),
    ],
)
def test_adapter_refuses_a_model_directory_that_is_not_the_pinned_one(
    environment: tuple[Flux2KleinQualityTestConfig, Path], change: str, code: str
) -> None:
    config, destination = environment
    model_dir = Path(config.model_dir)
    if change == "extra":
        (model_dir / "vae" / "custom_pipeline.py").write_text("print(1)", encoding="utf-8")
    elif change == "missing":
        (model_dir / "vae" / "config.json").unlink()
    elif change == "changed":
        (model_dir / "model_index.json").write_bytes(b'{"changed": true}')
    else:
        config = Flux2KleinQualityTestConfig(
            python_path=config.python_path, model_dir=str(model_dir / "nowhere")
        )
    called: list[str] = []

    def scene_model(_system: str, _user: str) -> str:
        called.append("scene")
        return _SCENES[0]

    adapter = build_flux2_klein_quality_test_adapter(
        config, scene_model=scene_model, topic="Mesa", style="photo"
    )
    with pytest.raises(VisualPermanentAdapterError, match=code):
        adapter(_requests(), destination)
    assert called == [] and not list(destination.iterdir())


def test_adapter_fails_closed_when_a_scene_description_is_refused(
    environment: tuple[Flux2KleinQualityTestConfig, Path],
) -> None:
    config, destination = environment
    adapter = build_flux2_klein_quality_test_adapter(
        config,
        scene_model=lambda _system, _user: "A desk with a laptop showing a chart about sales",
        topic="Mesa",
        style="photo",
    )
    with pytest.raises(
        VisualPermanentAdapterError, match="CONTENT_VISUALS_SCENE_PROMPT_REJECTED"
    ):
        adapter(_requests(), destination)
    assert not list(destination.iterdir())


def test_adapter_reports_an_unreachable_scene_model_as_retryable(
    environment: tuple[Flux2KleinQualityTestConfig, Path],
) -> None:
    config, destination = environment

    def unreachable(_system: str, _user: str) -> str:
        raise ConnectionError("ollama down")

    adapter = build_flux2_klein_quality_test_adapter(
        config, scene_model=unreachable, topic="Mesa", style="photo"
    )
    with pytest.raises(
        VisualRetryableAdapterError, match="CONTENT_VISUALS_SCENE_MODEL_UNAVAILABLE"
    ):
        adapter(_requests(), destination)


def test_adapter_reports_a_failed_runner_as_retryable(
    environment: tuple[Flux2KleinQualityTestConfig, Path],
) -> None:
    config, destination = environment
    failing = ("FAIL " + _SCENES[0], _SCENES[1])
    with pytest.raises(
        VisualRetryableAdapterError, match="CONTENT_VISUALS_FLUX2_INFERENCE_FAILED"
    ):
        _adapter(config, failing)(_requests(), destination)


def test_adapter_requires_the_narration_stretch_of_every_request(
    environment: tuple[Flux2KleinQualityTestConfig, Path],
) -> None:
    config, destination = environment
    requests = (VisualRequest(1, "segment one", 16, 32),)
    with pytest.raises(
        VisualPermanentAdapterError, match="CONTENT_VISUALS_NARRATION_STRETCH_MISSING"
    ):
        _adapter(config)(requests, destination)


def test_adapter_refuses_a_missing_python_or_an_invalid_timeout(
    environment: tuple[Flux2KleinQualityTestConfig, Path],
) -> None:
    config, destination = environment
    missing_python = Flux2KleinQualityTestConfig(
        python_path=str(destination / "python.exe"), model_dir=config.model_dir
    )
    with pytest.raises(
        VisualPermanentAdapterError, match="CONTENT_VISUALS_FLUX2_PYTHON_UNAVAILABLE"
    ):
        _adapter(missing_python)(_requests(), destination)
    no_time = Flux2KleinQualityTestConfig(
        python_path=config.python_path, model_dir=config.model_dir, timeout_seconds=0
    )
    with pytest.raises(
        VisualPermanentAdapterError, match="CONTENT_VISUALS_FLUX2_TIMEOUT_INVALID"
    ):
        _adapter(no_time)(_requests(), destination)
