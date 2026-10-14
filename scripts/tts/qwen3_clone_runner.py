"""Qwen3-TTS voice-clone runner for the A2 adapter `qwen3_tts_clone_quality_test`.

This file is executed by the Python of a separate virtual environment that has
`qwen-tts` installed; it never runs inside the project's own environment. It
reads a narration text and a reference voice with its transcript from local
files and writes one PCM S16LE mono WAV. It opens no network connection: the
adapter starts it with the Hugging Face offline switches set and a local model
directory.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
from qwen_tts import Qwen3TTSModel

_PAUSE_SECONDS = 0.25


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--text-file", required=True)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--reference-text-file", required=True)
    parser.add_argument("--output", required=True)
    arguments = parser.parse_args()

    text = Path(arguments.text_file).read_text(encoding="utf-8").strip()
    reference_text = Path(arguments.reference_text_file).read_text(encoding="utf-8").strip()
    # One generation per sentence keeps each call short and the pauses predictable.
    sentences = [part.strip() for part in re.split(r"(?<=[.!?…])\s+", text) if part.strip()]
    if not sentences or not reference_text:
        raise SystemExit("empty narration or reference transcript")

    model = Qwen3TTSModel.from_pretrained(
        arguments.model_dir,
        device_map="cuda:0",
        dtype=torch.bfloat16,
    )
    prompt = model.create_voice_clone_prompt(
        ref_audio=arguments.reference,
        ref_text=reference_text,
        x_vector_only_mode=False,
    )
    pieces: list[np.ndarray] = []
    sample_rate = 0
    for sentence in sentences:
        wavs, sample_rate = model.generate_voice_clone(
            text=sentence,
            language="Portuguese",
            voice_clone_prompt=prompt,
        )
        pieces.append(np.asarray(wavs[0], dtype=np.float32))
        pieces.append(np.zeros(int(sample_rate * _PAUSE_SECONDS), dtype=np.float32))
    audio = np.clip(np.concatenate(pieces[:-1]), -1.0, 1.0)
    sf.write(arguments.output, audio, sample_rate, subtype="PCM_16", format="WAV")


if __name__ == "__main__":
    main()
