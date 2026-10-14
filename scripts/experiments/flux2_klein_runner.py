"""FLUX.2 klein 4B runner for the E9 experiment, outside the pipeline.

This file is executed by the Python of a separate virtual environment that has
`diffusers` and `torch` installed; it never runs inside the project's own
environment. It reads the `prompts.json` written by `e9_visual_prompts.py`,
generates one 1080x1920 PNG per slot from a local model directory and writes a
contact sheet that puts each image next to the stretch of narration it covers.
It opens no network connection: the Hugging Face offline switches are set before
the libraries load.

The images are a quality test. They are NOT approved for monetized content and
carry no declared commercial use. The weights are not re-hashed here.

Usage: flux2_klein_runner.py --model-dir <dir> --prompts-file <prompts.json>
           [--style-name <name> --output-dir <dir>]

Without --style-name the images use the default style and are written next to
the prompts file; with it, the same scene descriptions are rendered in another
of the styles listed in the prompts file, into --output-dir.
"""

from __future__ import annotations

import argparse
import json
import os
import textwrap
import time
from pathlib import Path

os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

import torch  # noqa: E402
from diffusers import Flux2KleinPipeline  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

_WIDTH, _HEIGHT = 1088, 1920  # multiples of 16; cropped to 1080x1920 below
_SEED_BASE = 1400
_THUMB_WIDTH, _THUMB_HEIGHT = 360, 640
_CAPTION_HEIGHT = 250
_FONT_PATH = Path("C:/Windows/Fonts/arial.ttf")


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if _FONT_PATH.is_file():
        return ImageFont.truetype(str(_FONT_PATH), size)
    return ImageFont.load_default()


def _contact_sheet(slots: list[dict[str, object]], output: Path) -> Path:
    """One column per slot: the image and, below it, the narration it covers."""
    sheet = Image.new(
        "RGB",
        (_THUMB_WIDTH * len(slots), _THUMB_HEIGHT + _CAPTION_HEIGHT),
        "white",
    )
    draw = ImageDraw.Draw(sheet)
    title, body = _font(20), _font(17)
    for column, slot in enumerate(slots):
        left = column * _THUMB_WIDTH
        with Image.open(output / str(slot["file"])) as image:
            sheet.paste(image.resize((_THUMB_WIDTH, _THUMB_HEIGHT)), (left, 0))
        draw.text(
            (left + 10, _THUMB_HEIGHT + 8),
            f"{slot['index']} · {slot['start_seconds']}–{slot['end_seconds']} s",
            fill="black",
            font=title,
        )
        draw.multiline_text(
            (left + 10, _THUMB_HEIGHT + 38),
            textwrap.fill(str(slot["text"]), width=38),
            fill="black",
            font=body,
            spacing=4,
        )
    path = output / "folha-de-contato.png"
    sheet.save(path)
    return path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--prompts-file", required=True)
    parser.add_argument("--style-name", default=None)
    parser.add_argument("--output-dir", default=None)
    arguments = parser.parse_args()

    prompts_path = Path(arguments.prompts_file).resolve()
    plan = json.loads(prompts_path.read_text(encoding="utf-8"))
    output = prompts_path.parent
    style = str(plan["style"])
    if arguments.style_name is not None:
        if arguments.output_dir is None:
            raise SystemExit("--style-name requires --output-dir")
        style = str(plan["styles"][arguments.style_name])
        output = Path(arguments.output_dir).resolve()
        output.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    pipeline = Flux2KleinPipeline.from_pretrained(
        arguments.model_dir,
        torch_dtype=torch.bfloat16,
        local_files_only=True,
    )
    pipeline.enable_model_cpu_offload()
    load_seconds = time.perf_counter() - started

    records: list[dict[str, object]] = []
    for slot in plan["slots"]:
        seed = _SEED_BASE + int(slot["index"])
        torch.cuda.reset_peak_memory_stats()
        image_started = time.perf_counter()
        image = pipeline(
            prompt=f"{slot['prompt']}, {style}",
            height=_HEIGHT,
            width=_WIDTH,
            guidance_scale=1.0,
            num_inference_steps=4,
            generator=torch.Generator(device="cuda").manual_seed(seed),
        ).images[0]
        left = (image.width - 1080) // 2
        file_name = f"visual-{int(slot['index']):03d}.png"
        image.crop((left, 0, left + 1080, 1920)).save(output / file_name)
        records.append(
            {
                **slot,
                "file": file_name,
                "seed": seed,
                "seconds": round(time.perf_counter() - image_started, 2),
                "peak_vram_gb": round(torch.cuda.max_memory_allocated() / 1024**3, 2),
            }
        )
        print(file_name, f"{records[-1]['seconds']}s", flush=True)

    (output / "generation.json").write_text(
        json.dumps(
            {
                "model": "black-forest-labs/FLUX.2-klein-4B",
                "scope": "quality test only; not approved for monetized content",
                "style": style,
                "load_seconds": round(load_seconds, 2),
                "steps": 4,
                "guidance_scale": 1.0,
                "saved_size": [1080, 1920],
                "images": records,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print("folha:", _contact_sheet(records, output))
    print("total", f"{time.perf_counter() - started:.1f}s")


if __name__ == "__main__":
    main()
