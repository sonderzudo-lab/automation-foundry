"""Opt-in Kokoro PT-BR quality gate for the local RTX 3090."""

from __future__ import annotations

import os
import wave
from pathlib import Path

import pytest

from src.pipeline.tts import synthesize_kokoro_quality_test


@pytest.mark.skipif(
    os.environ.get("AUTOMATION_FOUNDRY_TEST_KOKORO") != "1",
    reason="requires opt-in Kokoro download, eSpeak NG, CUDA and human listening",
)
def test_kokoro_pt_br_generates_audible_quality_sample(tmp_path: Path) -> None:
    destination = tmp_path / "kokoro-pf-dora-quality-sample.wav"

    result = synthesize_kokoro_quality_test(
        (
            "Este é um teste local de qualidade da narração em português brasileiro. "
            "A aprovação comercial da voz ainda depende de escuta humana."
        ),
        destination,
        "pf_dora",
        "p",
    )

    with wave.open(str(destination), "rb") as audio:
        assert audio.getframerate() == 24_000
        assert audio.getnframes() > 24_000
    assert result.backend == "kokoro_quality_test"
    assert "scope=quality-test-only" in result.license_id
