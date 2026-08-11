"""Opt-in real FFmpeg playback artifact for the local Windows toolchain."""

from __future__ import annotations

import os
import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from src.pipeline.assembly import (
    AssemblyRequest,
    FFmpegQualityTestConfig,
    build_ffmpeg_quality_test_adapter,
)
from tests.pipeline.test_captions import _write_wav
from tests.pipeline.test_visuals import _write_png


@pytest.mark.skipif(
    os.environ.get("AUTOMATION_FOUNDRY_TEST_FFMPEG") != "1",
    reason="requires explicit opt-in to pinned external FFmpeg/ffprobe binaries",
)
def test_ffmpeg_quality_test_generates_probeable_h264_aac_mp4(
    tmp_path: Path,
) -> None:
    ffmpeg = shutil.which(os.environ.get("CONTENT_FFMPEG_PATH", "ffmpeg"))
    ffprobe = shutil.which(os.environ.get("CONTENT_FFPROBE_PATH", "ffprobe"))
    ffmpeg_hash = os.environ.get("CONTENT_FFMPEG_EXPECTED_SHA256", "")
    ffprobe_hash = os.environ.get("CONTENT_FFPROBE_EXPECTED_SHA256", "")
    assert ffmpeg is not None and ffprobe is not None
    assert ffmpeg_hash and ffprobe_hash

    visual = tmp_path / "visual.png"
    audio = tmp_path / "narration.wav"
    captions = tmp_path / "captions.ass"
    destination = tmp_path / "playable.mp4"
    _write_png(visual, width=1080, height=1920)
    _write_wav("teste", audio, "fake", "p")
    captions.write_text(
        """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,72,&H00FFFFFF,&H0000FFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,4,1,2,60,60,180,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:05.00,Default,,0,0,0,,{\\kf500}Vídeo local reproduzível
""",
        encoding="utf-8",
    )
    adapter = build_ffmpeg_quality_test_adapter(
        FFmpegQualityTestConfig(
            ffmpeg_path=ffmpeg,
            ffprobe_path=ffprobe,
            ffmpeg_sha256=ffmpeg_hash,
            ffprobe_sha256=ffprobe_hash,
            timeout_seconds=120,
        )
    )

    result = adapter(
        AssemblyRequest(
            audio_path=audio,
            visual_paths=(visual,),
            caption_path=captions,
            format="short",
            width=1080,
            height=1920,
            duration_seconds=Decimal("5"),
        ),
        destination,
    )

    assert destination.stat().st_size > 1_000
    assert result.video_codec == "h264"
    assert result.audio_codec == "aac"
    assert result.width == 1080 and result.height == 1920
    assert abs(result.duration_seconds - Decimal("5")) <= Decimal("0.50")
    assert result.subtitles_burned is True
