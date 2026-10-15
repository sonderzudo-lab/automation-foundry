"""Opt-in real LTX-2.3 clip through the pinned ComfyUI on the local GPU.

Uses the whole GPU and about 60 GB of RAM: run it only after agreeing with the
owner, with no other generation running and the workers stopped or idle::

    AUTOMATION_FOUNDRY_TEST_LTX23=1
    CONTENT_SCENE_ANIMATION_COMFYUI_DIR=D:\\automation-foundry\\labs\\video-wan22-i2v\\ComfyUI
    CONTENT_SCENE_ANIMATION_PYTHON_PATH=D:\\automation-foundry\\labs\\video-wan22-i2v\\venv\\Scripts\\python.exe
    CONTENT_FFMPEG_PATH=... CONTENT_FFMPEG_EXPECTED_SHA256=...
    AUTOMATION_FOUNDRY_TEST_LTX23_IMAGE=<an approved 1080x1920 PNG>
    pytest tests/integration/test_ltx23_scene_animation.py

It renders one 41-frame clip (1.64 s) and checks the MP4 and the memory peaks.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

import pytest

from src.pipeline.comfyui_runtime import (
    ClipRenderRequest,
    ComfyUIConfig,
    ComfyUILtx23Renderer,
)

pytestmark = [
    pytest.mark.gpu,
    pytest.mark.skipif(
        os.environ.get("AUTOMATION_FOUNDRY_TEST_LTX23") != "1",
        reason="requires explicit opt-in to a real LTX-2.3 render on the local GPU",
    ),
]


def test_ltx23_renders_one_real_clip(tmp_path: Path) -> None:
    image = Path(os.environ["AUTOMATION_FOUNDRY_TEST_LTX23_IMAGE"])
    ffmpeg = shutil.which(os.environ.get("CONTENT_FFMPEG_PATH", "ffmpeg"))
    assert ffmpeg is not None
    config = ComfyUIConfig(
        comfyui_dir=os.environ["CONTENT_SCENE_ANIMATION_COMFYUI_DIR"],
        python_path=os.environ["CONTENT_SCENE_ANIMATION_PYTHON_PATH"],
        models_dir=os.environ.get("CONTENT_SCENE_ANIMATION_MODELS_DIR") or None,
        ffmpeg_path=ffmpeg,
        ffmpeg_sha256=os.environ["CONTENT_FFMPEG_EXPECTED_SHA256"],
        min_free_ram_bytes=int(
            float(os.environ.get("CONTENT_SCENE_ANIMATION_MIN_FREE_RAM_GB", "40")) * 1024**3
        ),
        sage_attention=os.environ.get("CONTENT_SCENE_ANIMATION_SAGE_ATTENTION") == "true",
        fp16_accumulation=os.environ.get("CONTENT_SCENE_ANIMATION_FP16_ACCUMULATION") == "true",
    )
    renderer = ComfyUILtx23Renderer(config, workspace=tmp_path / "workspace")
    output = tmp_path / "clip.mp4"
    try:
        renderer.start(timeout_seconds=180)
        result = renderer.render(
            ClipRenderRequest(
                index=1,
                image_path=image,
                image_sha256=hashlib.sha256(image.read_bytes()).hexdigest(),
                motion="Subtle natural movement in the scene, slow and steady camera push-in",
                seed=190000,
                frames=41,
                format="short",
                width=1080,
                height=1920,
                output_path=output,
            ),
            timeout_seconds=600,
        )
    finally:
        renderer.close()

    assert output.read_bytes()[4:8] == b"ftyp"
    assert result.seconds > 0
    assert result.peak_ram_bytes is not None
    assert not renderer.running
