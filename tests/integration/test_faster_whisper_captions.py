r"""Opt-in local GPU quality test for the pinned faster-whisper A4 adapter.

The A4 adapter itself never downloads weights. Prepare the audited snapshot once:

    .venv\Scripts\hf download Systran/faster-whisper-small \
      --revision 536b0662742c02347bc0e980a01041f333bce120 \
      --local-dir storage/models/captions/faster-whisper-small-536b066

Then run this test with ``AUTOMATION_FOUNDRY_TEST_FASTER_WHISPER=1`` and
``CONTENT_CAPTION_MODEL_PATH`` pointing to that directory. Kokoro generates a
fresh local PT-BR WAV; nothing is published or sent to an external API.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.pipeline import captions
from src.pipeline.captions import (
    FASTER_WHISPER_QUALITY_TEST_BACKEND,
    FasterWhisperQualityTestConfig,
    get_configured_caption_adapter,
)
from src.pipeline.tts import synthesize_kokoro_quality_test


@pytest.mark.skipif(
    os.environ.get("AUTOMATION_FOUNDRY_TEST_FASTER_WHISPER") != "1",
    reason="requires pinned faster-whisper snapshot, CUDA and explicit local opt-in",
)
def test_faster_whisper_times_a_fresh_kokoro_pt_br_narration(tmp_path: Path) -> None:
    narration = (
        "Dormir reorganiza memórias, fortalece o aprendizado e prepara o cérebro "
        "para o próximo dia."
    )
    configured_path = os.environ.get("CONTENT_CAPTION_MODEL_PATH", "").strip()
    assert configured_path, "CONTENT_CAPTION_MODEL_PATH must point to the pinned snapshot"
    audio_path = tmp_path / "narration.wav"
    caption_path = tmp_path / "captions.ass"
    synthesize_kokoro_quality_test(narration, audio_path, "pm_alex", "p")
    adapter = get_configured_caption_adapter(
        FASTER_WHISPER_QUALITY_TEST_BACKEND,
        narration=narration,
        faster_whisper_config=FasterWhisperQualityTestConfig(model_path=Path(configured_path)),
    )
    duration = captions._adapter_wav_duration(audio_path)

    bundle = captions._generate_caption_atomic(
        adapter,
        audio_path,
        caption_path,
        narration,
        "pt",
        duration,
        "short",
    )

    assert bundle.result.backend == FASTER_WHISPER_QUALITY_TEST_BACKEND
    assert bundle.transcript_match >= captions._MIN_TRANSCRIPT_MATCH
    assert bundle.result.words[0].start_seconds >= 0
    assert bundle.result.words[-1].end_seconds <= duration
    assert any(
        following.start_seconds > previous.end_seconds
        for previous, following in zip(
            bundle.result.words,
            bundle.result.words[1:],
            strict=False,
        )
    )
    assert caption_path.read_text("utf-8").count("{\\kf") == bundle.word_count
