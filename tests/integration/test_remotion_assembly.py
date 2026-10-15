"""Opt-in real Remotion render of the animated edit, on the GPU.

Run only after lane C has committed ``render/remotion/`` (with ``ExplainerV1``), its
dependencies were installed with the owner's authorization and the Remotion
browser is already present, and only with the owner's go-ahead for using the GPU:

    AUTOMATION_FOUNDRY_TEST_REMOTION=1 pytest tests/integration/test_remotion_assembly.py

The text model is a scripted fake, so no plan quota is used. Node.js comes from
``CONTENT_REMOTION_NODE_PATH`` and the pinned ffprobe from ``CONTENT_FFPROBE_PATH``
and ``CONTENT_FFPROBE_EXPECTED_SHA256``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from src.pipeline.assembly import (
    REMOTION_PROJECT_DIR,
    AssemblyRequest,
    RemotionQualityTestConfig,
    build_remotion_quality_test_adapter,
)
from src.pipeline.text_model import TextModelProviderName, TextModelRequest, TextModelResult
from tests.pipeline.test_captions import _write_wav
from tests.pipeline.test_visuals import _write_png

_STRETCHES = (
    "Primeiro, a pausa. Segundo, a distância.",
    "Deixar longe ajuda, mas decidir importa.",
)
_CAPTIONS = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.20,0:00:01.80,Default,,0,0,0,,{\\kf40}Primeiro, {\\kf60}a {\\kf60}pausa.
Dialogue: 0,0:00:02.00,0:00:03.30,Default,,0,0,0,,{\\kf40}Segundo, {\\kf30}a {\\kf60}distância.
Dialogue: 0,0:00:03.40,0:00:04.70,Default,,0,0,0,,{\\kf30}Deixar {\\kf20}longe {\\kf20}ajuda, {\\kf10}mas {\\kf20}decidir {\\kf30}importa.
"""
_PLAN = {
    "schema_version": 1,
    "steps": {
        "title": "Dois passos",
        "items": [
            {"label": "a pausa", "anchor": "Primeiro, a pausa"},
            {"label": "a distância", "anchor": "Segundo, a distância"},
        ],
    },
    "highlights": [],
    "closing_sentence": "Deixar longe ajuda, mas decidir importa.",
}


class _ScriptedPlan:
    provider: TextModelProviderName = "ollama"
    model_id = "scripted-plan"

    def complete(self, request: TextModelRequest) -> TextModelResult:
        return TextModelResult(
            text=json.dumps(_PLAN, ensure_ascii=False),
            provider=self.provider,
            model_id=self.model_id,
            duration_seconds=Decimal("0"),
        )


@pytest.mark.skipif(
    os.environ.get("AUTOMATION_FOUNDRY_TEST_REMOTION") != "1",
    reason="renders on the GPU with the lane C project; set AUTOMATION_FOUNDRY_TEST_REMOTION=1",
)
def test_remotion_renders_the_animated_edit_as_a_probeable_mp4(tmp_path: Path) -> None:
    assert (REMOTION_PROJECT_DIR / "src" / "index.ts").is_file(), "lane C project is missing"
    ffprobe = shutil.which(os.environ.get("CONTENT_FFPROBE_PATH", "ffprobe"))
    ffprobe_hash = os.environ.get("CONTENT_FFPROBE_EXPECTED_SHA256", "")
    assert ffprobe is not None and ffprobe_hash

    audio = tmp_path / "narration.wav"
    _write_wav("teste", audio, "fake", "p")
    captions = tmp_path / "captions.ass"
    captions.write_text(_CAPTIONS, encoding="utf-8")
    images = []
    assets = []
    for index, stretch in enumerate(_STRETCHES, start=1):
        image = tmp_path / f"visual-{index:03d}.png"
        _write_png(image, width=1080, height=1920, shade=0x40 * index)
        images.append(image)
        assets.append(
            {
                "index": index,
                "file_name": image.name,
                "sha256": hashlib.sha256(image.read_bytes()).hexdigest(),
                "narration_text": stretch,
            }
        )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"assets": assets}), encoding="utf-8")
    destination = tmp_path / "assembly" / "video.mp4"
    adapter = build_remotion_quality_test_adapter(
        RemotionQualityTestConfig(
            node_path=os.environ.get("CONTENT_REMOTION_NODE_PATH", "node"),
            ffprobe_path=ffprobe,
            ffprobe_sha256=ffprobe_hash,
            timeout_seconds=600,
        ),
        narration=" ".join(_STRETCHES),
        text_model=_ScriptedPlan(),
    )

    result = adapter(
        AssemblyRequest(
            audio_path=audio,
            visual_paths=tuple(images),
            caption_path=captions,
            format="short",
            width=1080,
            height=1920,
            duration_seconds=Decimal("5"),
            visual_manifest_path=manifest,
        ),
        destination,
    )

    assert destination.stat().st_size > 1_000
    assert (result.video_codec, result.audio_codec) == ("h264", "aac")
    assert (result.width, result.height) == (1080, 1920)
    assert abs(result.duration_seconds - Decimal("5")) <= Decimal("0.5")
    assert result.edit == "animated"
