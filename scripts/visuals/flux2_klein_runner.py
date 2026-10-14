"""FLUX.2 klein 4B runner for the A3 adapter `flux2_klein_quality_test`.

This file is executed by the Python of a separate virtual environment that has
`diffusers` and `torch` installed; it never runs inside the project's own
environment. It reads a requests file written by the adapter, generates one PNG
per request from a local model directory and writes each to the output
directory under the requested file name. It opens no network connection: the
adapter starts it with the Hugging Face offline switches set, and the pipeline
loads with `local_files_only=True`.

The adapter, not this runner, pins the weights by SHA-256 and chooses prompts
and seeds. The images are a quality test and carry no commercial-use claim.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import torch
from diffusers import Flux2KleinPipeline


def _generation_size(size: int) -> int:
    """The model works on multiples of 16; the extra pixels are cropped away."""
    return -(-size // 16) * 16


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--requests-file", required=True)
    parser.add_argument("--output-dir", required=True)
    arguments = parser.parse_args()

    if not torch.cuda.is_available():
        raise SystemExit("CUDA is required")
    plan = json.loads(Path(arguments.requests_file).read_text(encoding="utf-8"))
    output = Path(arguments.output_dir)

    pipeline = Flux2KleinPipeline.from_pretrained(
        arguments.model_dir,
        torch_dtype=torch.bfloat16,
        local_files_only=True,
    )
    pipeline.enable_model_cpu_offload()

    for request in plan["images"]:
        width, height = int(request["width"]), int(request["height"])
        image = pipeline(
            prompt=str(request["prompt"]),
            width=_generation_size(width),
            height=_generation_size(height),
            guidance_scale=float(plan["guidance_scale"]),
            num_inference_steps=int(plan["num_inference_steps"]),
            generator=torch.Generator(device="cuda").manual_seed(int(request["seed"])),
        ).images[0]
        left = (image.width - width) // 2
        top = (image.height - height) // 2
        image.convert("RGB").crop((left, top, left + width, top + height)).save(
            output / str(request["file_name"]),
            format="PNG",
        )
        print(request["file_name"], flush=True)


if __name__ == "__main__":
    main()
