"""Local image generation for A3 with FLUX.2 klein 4B, quality test only.

The adapter writes one scene description per stretch of approved narration,
checks it, and renders it with pinned local weights in a separate virtual
environment. Nothing is downloaded: the whole model directory is compared with
a SHA-256 inventory before each inference and the runner starts offline.

Every asset is declared with ``commercial_use=False``. The model's training
data is not disclosed and its licence does not address ownership of the
output, so the images are not approved for monetized content; see
``docs/licensing/content-visuals.md``.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from src.pipeline.text_model import as_scene_model_call
from src.pipeline.text_model_factory import configured_ollama_model, configured_text_model
from src.pipeline.text_model_ollama import OLLAMA_NUM_CTX
from src.pipeline.visual_prompts import (
    SceneModelCall,
    ScenePromptError,
    VisualStyle,
    revise_scene_prompt,
    styled_prompt,
    write_scene_prompts,
)
from src.pipeline.visuals import (
    VisualAdapter,
    VisualAssetResult,
    VisualPermanentAdapterError,
    VisualRequest,
    VisualRetryableAdapterError,
)

FLUX2_KLEIN_QUALITY_TEST_BACKEND = "flux2_klein_quality_test"
FLUX2_KLEIN_MODEL_REPOSITORY = "black-forest-labs/FLUX.2-klein-4B"
FLUX2_KLEIN_MODEL_REVISION = "e7b7dc27f91deacad38e78976d1f2b499d76a294"
FLUX2_KLEIN_STEPS = 4
FLUX2_KLEIN_GUIDANCE_SCALE = 1.0
# Every file the Diffusers layout of that revision loads, relative to the local
# model directory. Pinning only the large weights would let a changed config or
# tokenizer through.
FLUX2_KLEIN_MODEL_SHA256 = {
    "LICENSE.md": "ca02bc51900ab07789d1b70283329e7137f5af98f5161c23a1c81fc38a4af1fe",
    "model_index.json": "51a76cb1cf3ed37423a1128c79c22faee8e6fbe7f5aaeb737f0a258930dbaac0",
    "scheduler/scheduler_config.json": (
        "067afb012cef64553a763447d1efd93daeffcc0123ca7e25b09f8de20b90762e"
    ),
    "text_encoder/config.json": (
        "214b4c29a0d975e9fddf9994a5673f22cb2c4c5750352f9227c2c3251ebeab40"
    ),
    "text_encoder/generation_config.json": (
        "4347b1aeed2b2b78bc059920a0b7f5fec71482e1344952b76d7665d638d71f13"
    ),
    "text_encoder/model-00001-of-00002.safetensors": (
        "8c0506e7f4936fa7e26183a4fd8da4e2bdbc5990ba64ae441f965d51228f36ea"
    ),
    "text_encoder/model-00002-of-00002.safetensors": (
        "82f2bd839378541b0557bfabaf37c7d3d637071fdcb73302dedd7cf61162ce07"
    ),
    "text_encoder/model.safetensors.index.json": (
        "06b3d5319b6d76d1a4a2433419180016cfd54ed62d086a5e6567a809f8c82634"
    ),
    "tokenizer/added_tokens.json": (
        "c0284b582e14987fbd3d5a2cb2bd139084371ed9acbae488829a1c900833c680"
    ),
    "tokenizer/chat_template.jinja": (
        "a55ee1b1660128b7098723e0abcd92caa0788061051c62d51cbe87d9cf1974d8"
    ),
    "tokenizer/merges.txt": "8831e4f1a044471340f7c0a83d7bd71306a5b867e95fd870f74d0c5308a904d5",
    "tokenizer/special_tokens_map.json": (
        "76862e765266b85aa9459767e33cbaf13970f327a0e88d1c65846c2ddd3a1ecd"
    ),
    "tokenizer/tokenizer.json": (
        "aeb13307a71acd8fe81861d94ad54ab689df773318809eed3cbe794b4492dae4"
    ),
    "tokenizer/tokenizer_config.json": (
        "443bfa629eb16387a12edbf92a76f6a6f10b2af3b53d87ba1550adfcf45f7fa0"
    ),
    "tokenizer/vocab.json": "ca10d7e9fb3ed18575dd1e277a2579c16d108e32f27439684afa0e10b1440910",
    "transformer/config.json": (
        "09733c74a3da6d17dd0a0472a091a8950c7c6935889c32c16cc800ede05029de"
    ),
    "transformer/diffusion_pytorch_model.safetensors": (
        "9f29f9edcfdae452a653ffb51a534ca4decd389952c225724ff3b94042612a6e"
    ),
    "vae/config.json": "0d6dfb69ae95a5e2ac9836284bbb63d8b38ce67b25ba2dff380752b2a10ab948",
    "vae/diffusion_pytorch_model.safetensors": (
        "ca70d2202afe6415bdbcb8793ba8cd99fd159cfe6192381504d6c4d3036e0f04"
    ),
}
# Files a download leaves next to the model that Diffusers never loads.
_IGNORED_MODEL_FILES = frozenset({"README.md"})
_IGNORED_MODEL_DIRECTORIES = frozenset({".cache"})
_INVENTORY_DIGEST = hashlib.sha256(
    "\n".join(
        f"{path}:{digest}" for path, digest in sorted(FLUX2_KLEIN_MODEL_SHA256.items())
    ).encode("utf-8")
).hexdigest()
_LICENSE_ID = (
    "Apache-2.0(weights; LICENSE.md pinned); training-data=undisclosed; "
    "output-ownership=unaddressed; scope=quality-test-only"
)
_RUNNER = (
    Path(__file__).resolve().parents[2] / "scripts" / "visuals" / "flux2_klein_runner.py"
)


@dataclass(frozen=True, slots=True)
class Flux2KleinQualityTestConfig:
    """Local paths of the separate image-generation environment."""

    python_path: str
    model_dir: str
    timeout_seconds: float = 240.0


def build_flux2_klein_quality_test_adapter(
    config: Flux2KleinQualityTestConfig,
    *,
    scene_model: SceneModelCall,
    topic: str,
    style: VisualStyle,
    release_scene_model: Callable[[], None] | None = None,
    revisions: Mapping[int, tuple[str, str]] | None = None,
    narration: str | None = None,
    kept_scenes: Sequence[str] = (),
) -> VisualAdapter:
    """Build an adapter that generates one image per stretch of narration.

    ``scene_model`` writes the scene descriptions; ``release_scene_model`` is
    called before the image model loads so both do not hold the GPU at once.
    With ``revisions`` (request index -> previous scene and reviewer's note) every
    request is an image the reviewer sent back: its description is rewritten from
    the note, with the full ``narration`` and the ``kept_scenes`` as context.
    """

    def generate(
        requests: tuple[VisualRequest, ...],
        destination: Path,
    ) -> tuple[VisualAssetResult, ...]:
        if not 1 <= config.timeout_seconds <= 3600:
            raise VisualPermanentAdapterError("CONTENT_VISUALS_FLUX2_TIMEOUT_INVALID")
        python = _existing_file(config.python_path, "CONTENT_VISUALS_FLUX2_PYTHON_UNAVAILABLE")
        model_dir = _verified_model_directory(config.model_dir)
        if not _RUNNER.is_file():
            raise VisualPermanentAdapterError("CONTENT_VISUALS_FLUX2_RUNNER_UNAVAILABLE")
        stretches = [request.narration_text.strip() for request in requests]
        if not all(stretches):
            raise VisualPermanentAdapterError("CONTENT_VISUALS_NARRATION_STRETCH_MISSING")

        context = narration or " ".join(stretches)
        try:
            if revisions is None:
                scenes = write_scene_prompts(
                    scene_model,
                    topic=topic,
                    narration=context,
                    stretches=stretches,
                )
            else:
                if any(request.index not in revisions for request in requests):
                    raise VisualPermanentAdapterError("CONTENT_VISUALS_REVISION_MISSING")
                scenes = tuple(
                    revise_scene_prompt(
                        scene_model,
                        topic=topic,
                        narration=context,
                        stretch=stretch,
                        previous_scene=revisions[request.index][0],
                        note=revisions[request.index][1],
                        other_scenes=kept_scenes,
                    )
                    for request, stretch in zip(requests, stretches, strict=True)
                )
        except VisualPermanentAdapterError:
            raise
        except ScenePromptError as exc:
            raise VisualPermanentAdapterError(
                "CONTENT_VISUALS_SCENE_PROMPT_REJECTED"
            ) from exc
        except Exception as exc:
            raise VisualRetryableAdapterError(
                "CONTENT_VISUALS_SCENE_MODEL_UNAVAILABLE"
            ) from exc
        if release_scene_model is not None:
            release_scene_model()

        images = [
            {
                "file_name": f"visual-{request.index:03d}.png",
                "prompt": styled_prompt(scene, style),
                "seed": _seed(request.index, styled_prompt(scene, style)),
                "width": request.width,
                "height": request.height,
            }
            for request, scene in zip(requests, scenes, strict=True)
        ]
        with tempfile.TemporaryDirectory(
            prefix="flux2-klein-",
            dir=destination.parent,
        ) as workspace:
            requests_file = Path(workspace) / "requests.json"
            requests_file.write_text(
                json.dumps(
                    {
                        "num_inference_steps": FLUX2_KLEIN_STEPS,
                        "guidance_scale": FLUX2_KLEIN_GUIDANCE_SCALE,
                        "images": images,
                    }
                ),
                encoding="utf-8",
            )
            try:
                completed = subprocess.run(
                    [
                        str(python),
                        str(_RUNNER),
                        "--model-dir", str(model_dir),
                        "--requests-file", str(requests_file),
                        "--output-dir", str(destination),
                    ],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=config.timeout_seconds,
                    check=False,
                    shell=False,
                    # The weights are local and pinned; the runner must not reach the Hub.
                    env={**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"},
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise VisualRetryableAdapterError(
                    "CONTENT_VISUALS_FLUX2_INFERENCE_FAILED"
                ) from exc
        if completed.returncode != 0:
            raise VisualRetryableAdapterError("CONTENT_VISUALS_FLUX2_INFERENCE_FAILED")

        return tuple(
            VisualAssetResult(
                file_name=str(image["file_name"]),
                source_type="generated",
                provider="local-flux2-klein",
                model_id=(
                    f"{FLUX2_KLEIN_MODEL_REPOSITORY}@{FLUX2_KLEIN_MODEL_REVISION}"
                    f"#inventory-sha256:{_INVENTORY_DIGEST[:16]}"
                ),
                source_id=f"flux2-klein:{request.index}:seed-{image['seed']}",
                source_uri=f"local-model://flux2-klein-4b/{FLUX2_KLEIN_MODEL_REVISION}",
                license_id=_LICENSE_ID,
                commercial_use=False,
                attribution="Imagem gerada localmente; teste de qualidade, não monetizável",
                generation_prompt=str(image["prompt"]),
                seed=int(str(image["seed"])),
            )
            for request, image in zip(requests, images, strict=True)
        )

    return generate


# Each description request is a few thousand tokens. Without a fixed window the
# OpenAI-compatible endpoint loads the model with its default context, several
# times larger, which made each call take up to a minute in run 16.
SCENE_MODEL_NUM_CTX = OLLAMA_NUM_CTX
SCENE_MODEL_TIMEOUT_SECONDS = 120.0


def ollama_scene_model() -> SceneModelCall:
    """Scene descriptions written by the configured text model.

    Ollama by default, the same local model that writes the script: native chat
    API with a fixed context window and thinking disabled. With ``codex_exec`` the
    ChatGPT plan writes them and Ollama answers only after its usage limit. An
    unreachable model or an empty answer fails the step as retryable.
    """
    return as_scene_model_call(
        configured_text_model(ollama_timeout_seconds=SCENE_MODEL_TIMEOUT_SECONDS),
        purpose="scene_description",
    )


def release_ollama_scene_model() -> None:
    """Ask Ollama to unload the script model so the image model has the GPU.

    Best effort: if the request fails the image model may run short of memory,
    which surfaces as a retryable inference failure. Harmless when the scenes
    came from another provider and the local model was never loaded.
    """
    configured_ollama_model(timeout_seconds=SCENE_MODEL_TIMEOUT_SECONDS).release()


def _seed(index: int, prompt: str) -> int:
    """Stable per-image seed, so the same approved content renders the same image."""
    return int(hashlib.sha256(f"{index}:{prompt}".encode()).hexdigest()[:8], 16)


def _existing_file(path: str, code: str) -> Path:
    try:
        resolved = Path(path).resolve(strict=True)
    except OSError as exc:
        raise VisualPermanentAdapterError(code) from exc
    if not resolved.is_file():
        raise VisualPermanentAdapterError(code)
    return resolved


def _verified_model_directory(path: str) -> Path:
    """Return the model directory only if it holds exactly the pinned inventory."""
    try:
        model_dir = Path(path).resolve(strict=True)
    except OSError as exc:
        raise VisualPermanentAdapterError("CONTENT_VISUALS_FLUX2_MODEL_UNAVAILABLE") from exc
    if not model_dir.is_dir():
        raise VisualPermanentAdapterError("CONTENT_VISUALS_FLUX2_MODEL_UNAVAILABLE")
    present: set[str] = set()
    for file_path in model_dir.rglob("*"):
        relative = file_path.relative_to(model_dir)
        if relative.parts[0] in _IGNORED_MODEL_DIRECTORIES or not file_path.is_file():
            continue
        if file_path.is_symlink():
            raise VisualPermanentAdapterError("CONTENT_VISUALS_FLUX2_MODEL_INVENTORY_MISMATCH")
        present.add(relative.as_posix())
    if present - _IGNORED_MODEL_FILES != set(FLUX2_KLEIN_MODEL_SHA256):
        raise VisualPermanentAdapterError("CONTENT_VISUALS_FLUX2_MODEL_INVENTORY_MISMATCH")
    for relative_path, expected_sha256 in FLUX2_KLEIN_MODEL_SHA256.items():
        if _sha256_unchanged_file(model_dir / relative_path) != expected_sha256:
            raise VisualPermanentAdapterError("CONTENT_VISUALS_FLUX2_MODEL_HASH_MISMATCH")
    return model_dir


def _sha256_unchanged_file(path: Path) -> str:
    """Hash a multi-gigabyte weight file once per size and modification time."""
    stat = path.stat()
    return _sha256_file_cached(str(path), stat.st_size, stat.st_mtime_ns)


@lru_cache(maxsize=64)
def _sha256_file_cached(path: str, _size: int, _mtime_ns: int) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(8 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
