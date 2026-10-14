"""FLUX.2 klein quality-test adapter with a fake runner and a fake model directory."""

from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest

from src.pipeline import visual_generation
from src.pipeline.scene_plan import parse_scene_plan, scene_image_prompt
from src.pipeline.text_model import (
    TextModelPermanentError,
    TextModelProviderName,
    TextModelRequest,
    TextModelResult,
)
from src.pipeline.visual_generation import (
    FLUX2_KLEIN_MODEL_REVISION,
    Flux2KleinQualityTestConfig,
    build_flux2_klein_quality_test_adapter,
)
from src.pipeline.visual_prompts import VISUAL_STYLES, ScenePromptError
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
_SETTING = "A tidy home office with a wooden desk against a white wall and a single shelf"


def _plan(*descriptions: str, setting: str = _SETTING) -> str:
    """A plan answer with one scene per description, all in the same setting."""
    return json.dumps(
        {
            "schema_version": 1,
            "characters": [],
            "settings": [{"id": "office", "description": setting}],
            "scenes": [
                {
                    "index": index,
                    "description": description,
                    "setting_id": "office",
                    "character_ids": [],
                    "shot": "wide" if index % 2 else "close_up",
                }
                for index, description in enumerate(descriptions, start=1)
            ],
        }
    )


def _image_prompts(answer: str) -> list[str]:
    plan = parse_scene_plan(answer, scene_count=len(json.loads(answer)["scenes"]))
    return [scene_image_prompt(plan, scene.index) for scene in plan.scenes]


class _TextModel:
    """Fake text model: answers or raises the scripted outcomes, repeating the last."""

    provider: TextModelProviderName = "ollama"
    model_id = "qwen3:14b"

    def __init__(self, *outcomes: str | Exception) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[TextModelRequest] = []

    def complete(self, request: TextModelRequest) -> TextModelResult:
        self.requests.append(request)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return TextModelResult(
            text=outcome, provider="ollama", model_id="qwen3:14b", duration_seconds=Decimal(1)
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
    return build_flux2_klein_quality_test_adapter(
        config,
        scene_model=_TextModel(_plan(*scenes)),
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
    assert first.generation_prompt == (
        f"{_image_prompts(_plan(*_SCENES))[0]}, {VISUAL_STYLES['cartoon']}"
    )
    assert first.generation_prompt.startswith(_SCENES[0]) and _SETTING in first.generation_prompt
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
    scene_model = _TextModel(_plan(*_SCENES))

    adapter = build_flux2_klein_quality_test_adapter(
        config, scene_model=scene_model, topic="Mesa", style="photo"
    )
    with pytest.raises(VisualPermanentAdapterError, match=code):
        adapter(_requests(), destination)
    assert scene_model.requests == [] and not list(destination.iterdir())


def test_adapter_fails_closed_when_a_scene_description_is_refused(
    environment: tuple[Flux2KleinQualityTestConfig, Path],
) -> None:
    config, destination = environment
    scene_model = _TextModel(
        _plan(
            "A desk with a laptop showing a chart about the sales of the quarter",
            _SCENES[1],
        )
    )
    adapter = build_flux2_klein_quality_test_adapter(
        config, scene_model=scene_model, topic="Mesa", style="photo"
    )
    with pytest.raises(
        VisualPermanentAdapterError, match="CONTENT_VISUALS_SCENE_PROMPT_REJECTED"
    ):
        adapter(_requests(), destination)
    assert len(scene_model.requests) == visual_generation.SCENE_PLAN_ATTEMPTS
    assert not list(destination.iterdir())


def test_adapter_reports_an_unreachable_scene_model_as_retryable(
    environment: tuple[Flux2KleinQualityTestConfig, Path],
) -> None:
    config, destination = environment

    adapter = build_flux2_klein_quality_test_adapter(
        config,
        scene_model=_TextModel(ConnectionError("ollama down")),
        topic="Mesa",
        style="photo",
    )
    with pytest.raises(
        VisualRetryableAdapterError, match="CONTENT_VISUALS_SCENE_MODEL_UNAVAILABLE"
    ):
        adapter(_requests(), destination)


def test_adapter_reports_a_text_model_that_is_not_signed_in_as_permanent(
    environment: tuple[Flux2KleinQualityTestConfig, Path],
) -> None:
    config, destination = environment
    adapter = build_flux2_klein_quality_test_adapter(
        config,
        scene_model=_TextModel(TextModelPermanentError("not signed in")),
        topic="Mesa",
        style="photo",
    )
    with pytest.raises(
        VisualPermanentAdapterError, match="CONTENT_VISUALS_SCENE_MODEL_MISCONFIGURED"
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


def test_scene_model_uses_the_native_api_with_a_fixed_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json as json_module
    from types import SimpleNamespace

    import httpx
    import respx

    from src.pipeline import text_model_factory

    monkeypatch.setattr(
        text_model_factory,
        "settings",
        SimpleNamespace(
            ollama_base_url="http://127.0.0.1:11434/v1",
            ollama_model="qwen3:14b",
            ollama_timeout=999,
            content_text_model_provider="ollama",
        ),
    )
    with respx.mock(assert_all_called=True) as router:
        route = router.post("http://127.0.0.1:11434/api/chat").mock(
            side_effect=[
                httpx.Response(200, json={"message": {"content": "<think></think> A desk "}}),
                httpx.Response(200, json={"message": {"content": "   "}}),
            ]
        )
        model = visual_generation.scene_text_model()
        request = TextModelRequest(purpose="scene_plan", system="system text", user="user")
        assert model.complete(request).text == "A desk"
        with pytest.raises(RuntimeError, match="empty"):
            model.complete(request)

    body = json_module.loads(route.calls[0].request.content)
    assert body["model"] == "qwen3:14b"
    assert body["think"] is False and body["stream"] is False
    assert body["options"] == {"num_ctx": visual_generation.SCENE_MODEL_NUM_CTX}
    assert [message["role"] for message in body["messages"]] == ["system", "user"]
    # The scene calls keep their own timeout instead of OLLAMA_TIMEOUT.
    timeout = route.calls[0].request.extensions["timeout"]
    assert timeout["read"] == visual_generation.SCENE_MODEL_TIMEOUT_SECONDS


def test_adapter_rewrites_only_the_images_sent_back_from_the_notes(
    environment: tuple[Flux2KleinQualityTestConfig, Path],
) -> None:
    config, destination = environment
    scene_model = _TextModel(_SCENES[1])
    adapter = build_flux2_klein_quality_test_adapter(
        config,
        scene_model=scene_model,
        topic="Mesa",
        style="dark",
        revisions={2: (_SCENES[0], "Mostre um pote, não um caderno")},
        narration="Pega só o que vai usar. Põe tudo num pote.",
        kept_scenes=(_SCENES[0],),
    )
    results = adapter(_requests()[1:], destination)

    assert [result.file_name for result in results] == ["visual-002.png"]
    seen = [request.user for request in scene_model.requests]
    assert [request.purpose for request in scene_model.requests] == ["scene_revision"]
    assert scene_model.requests[0].response_schema is None
    assert "«Mostre um pote, não um caderno»" in seen[0]
    assert "Pega só o que vai usar. Põe tudo num pote." in seen[0]
    assert results[0].generation_prompt == f"{_SCENES[1]}, {VISUAL_STYLES['dark']}"
    with pytest.raises(VisualPermanentAdapterError, match="CONTENT_VISUALS_REVISION_MISSING"):
        adapter(_requests()[:1], destination)


# ── Scene plan of the whole video ─────────────────────────────────────────────


def test_one_plan_request_carries_every_stretch_and_the_schema(
    environment: tuple[Flux2KleinQualityTestConfig, Path],
) -> None:
    config, destination = environment
    scene_model = _TextModel(_plan(*_SCENES))
    adapter = build_flux2_klein_quality_test_adapter(
        config, scene_model=scene_model, topic="Mesa organizada", style="photo"
    )

    results = adapter(_requests(), destination)

    (request,) = scene_model.requests
    assert request.purpose == "scene_plan"
    assert request.response_schema is not None
    assert "1. Pega só o que vai usar.\n2. Põe tudo num pote." in request.user
    assert "Exactly 2 scenes" in request.system
    assert [result.generation_prompt for result in results] == [
        f"{prompt}, {VISUAL_STYLES['photo']}" for prompt in _image_prompts(_plan(*_SCENES))
    ]


def test_a_refused_plan_is_asked_again_with_the_reason(
    environment: tuple[Flux2KleinQualityTestConfig, Path],
) -> None:
    config, destination = environment
    scene_model = _TextModel(
        "Here is the plan: not JSON",
        _plan(_SCENES[0]),
        _plan(*_SCENES),
    )
    adapter = build_flux2_klein_quality_test_adapter(
        config, scene_model=scene_model, topic="Mesa", style="photo"
    )

    results = adapter(_requests(), destination)

    assert len(results) == 2
    first_retry, second_retry = (request.user for request in scene_model.requests[1:])
    assert "refused because it is not a single valid JSON object" in first_retry
    assert "Here is the plan: not JSON" in first_retry
    assert "refused because it has 1 scenes; it must have exactly 2" in second_retry


def test_a_plan_whose_image_prompts_would_be_too_long_is_refused(
    environment: tuple[Flux2KleinQualityTestConfig, Path],
) -> None:
    config, destination = environment
    wordy = " ".join(["meticulously arranged mahogany bookshelves"] * 10)
    too_long = json.dumps(
        {
            "schema_version": 1,
            "characters": [
                {"id": f"person_{n}", "description": f"A person number {n} beside {wordy}"}
                for n in range(3)
            ],
            "settings": [{"id": "office", "description": f"An office with {wordy}"}],
            "scenes": [
                {
                    "index": index,
                    "description": f"{description} beside {wordy}",
                    "setting_id": "office",
                    "character_ids": ["person_0", "person_1", "person_2"],
                    "shot": shot,
                }
                for index, (description, shot) in enumerate(
                    zip(_SCENES, ("wide", "detail"), strict=True), start=1
                )
            ],
        }
    )
    scene_model = _TextModel(too_long, _plan(*_SCENES))
    adapter = build_flux2_klein_quality_test_adapter(
        config, scene_model=scene_model, topic="Mesa", style="photo"
    )

    adapter(_requests(), destination)

    retry = scene_model.requests[1].user
    assert "image prompt of scene 1" in retry and "describe the characters" in retry


def test_more_scenes_than_a_plan_holds_are_refused_without_asking() -> None:
    scene_model = _TextModel(_plan(*_SCENES))
    with pytest.raises(ScenePromptError):
        visual_generation.write_scene_plan(
            scene_model, topic="Mesa", stretches=["Trecho."] * 61, style="photo"
        )
    assert scene_model.requests == []
