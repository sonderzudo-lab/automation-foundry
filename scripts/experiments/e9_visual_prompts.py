"""E9 — descrição automática da imagem de cada trecho da narração.

Experimento fora do pipeline: lê, sem alterar, o roteiro aprovado e o WAV de uma run já
concluída, divide a narração nos mesmos trechos que o gate A2 mostra e pede ao modelo
do roteiro uma descrição em inglês da cena de cada trecho, pronta para um gerador de
imagem. Não cria run, não grava no banco e não gera imagem: a geração é feita pelo
runner `flux2_klein_runner.py`, em outro ambiente virtual.

As imagens geradas a partir destas descrições são teste de qualidade e não têm uso
comercial declarado.

Uso, com o Ollama local no ar e nenhuma run em execução:

    python -m scripts.experiments.e9_visual_prompts \
        --run-dir D:/automation-foundry/storage/content-engine/run-14 \
        --output storage/experiments/e9-imagens-automaticas/run14
"""

from __future__ import annotations

import argparse
import json
import re
import wave
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

from openai import OpenAI

from src.core.config import settings
from src.pipeline.script_gen import _call_llm, _count_words, _make_client
from src.pipeline.visuals import VisualPlanSlot, plan_visual_slots, visual_count_bounds

PROMPT_ATTEMPTS = 3
PROMPT_WORD_RANGE = (8, 60)

# Acrescentado a toda descrição pelo runner; o estilo é do canal, não do trecho.
# Nenhum estilo cita artista, estúdio, obra ou personagem existente.
_NO_TEXT = "vertical composition, no text, no letters, no logos, no watermark"
STYLES: dict[str, str] = {
    "photo": (
        f"realistic photography, warm natural window light, shallow depth of field, {_NO_TEXT}"
    ),
    "cartoon": (
        "flat cartoon illustration, bold clean outlines, simple rounded shapes, bright "
        f"saturated colours, 2D animation look, {_NO_TEXT}"
    ),
    "dark": (
        "dark moody cinematic photography, low-key lighting, deep shadows, desaturated "
        f"cold tones, a single dim light source, {_NO_TEXT}"
    ),
}
STYLE = STYLES["photo"]

# Imagem gerada não é fonte factual: nada de dado, gráfico, documento ou tela legível.
_BANNED_RE = re.compile(
    r"\b(text|caption|headline|logo|brand|chart|graph|infographic|screenshot|watermark"
    r"|poster|document|celebrity)s?\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SlotPrompt:
    """Um trecho da narração e a descrição da cena que deve ilustrá-lo."""

    index: int
    start_seconds: str
    end_seconds: str
    text: str
    prompt: str
    attempts: int
    # Motivo pelo qual a descrição final ainda foi recusada pelas checagens, se foi.
    problem: str | None = None


def prompt_problem(prompt: str) -> str | None:
    """Motivo determinístico para recusar uma descrição, ou None se ela serve."""
    words = _count_words(prompt)
    if not PROMPT_WORD_RANGE[0] <= words <= PROMPT_WORD_RANGE[1]:
        return (
            f"has {words} words; it must have between {PROMPT_WORD_RANGE[0]} and "
            f"{PROMPT_WORD_RANGE[1]}"
        )
    if not prompt.isascii():
        return "is not written in plain English"
    banned = _BANNED_RE.search(prompt)
    if banned is not None:
        return f"mentions '{banned.group(0).lower()}', which the image must not contain"
    return None


def prompt_request(
    topic: str,
    narration: str,
    slot: VisualPlanSlot,
    previous: list[str],
    *,
    refused: tuple[str, str] | None = None,
) -> tuple[str, str]:
    """Prompts do pedido de uma descrição; em inglês, porque a resposta é em inglês."""
    system = (
        # Token do Qwen3 que desativa o modo thinking; outros modelos ignoram a linha.
        "/no_think\n"
        "You write prompts for a text-to-image model. You receive the narration of a "
        "short vertical video in Brazilian Portuguese and ONE stretch of it. Describe, "
        "in English, one concrete photographable scene that shows what that stretch "
        "says, so that a viewer connects the image to the speech.\n"
        "Rules:\n"
        "- One sentence, 15 to 40 words: objects, setting and framing only. Do not "
        "mention light, colours, mood, camera or art style: the channel's visual "
        "style is added separately.\n"
        "- Nothing with readable content: no written words, letters, numbers, signs, "
        "screens, papers with writing, diagrams or brand marks.\n"
        "- No identifiable person: if someone is needed, show only hands or a figure "
        "from behind.\n"
        "- Do not repeat a scene already used for another stretch.\n"
        "Answer with the prompt only, without quotes or comments."
    )
    used = "\n".join(f"- {prompt}" for prompt in previous) or "- (none yet)"
    user = (
        f"Video topic: {topic}\n\n"
        f"Full narration:\n{narration}\n\n"
        f"Scenes already used:\n{used}\n\n"
        f"Stretch to illustrate (number {slot.index}):\n{slot.text}"
    )
    if refused is not None:
        user += (
            f"\n\nYour previous answer was refused because it {refused[1]}: "
            f"«{refused[0]}». Write a different prompt."
        )
    return system, user


def write_prompts(
    client: OpenAI,
    *,
    topic: str,
    narration: str,
    slots: tuple[VisualPlanSlot, ...],
) -> list[SlotPrompt]:
    """Uma descrição por trecho; a recusada é refeita com o motivo, até o teto."""
    results: list[SlotPrompt] = []
    for slot in slots:
        refused: tuple[str, str] | None = None
        prompt = ""
        problem: str | None = None
        attempts = 0
        while attempts < PROMPT_ATTEMPTS:
            attempts += 1
            system, user = prompt_request(
                topic,
                narration,
                slot,
                [result.prompt for result in results],
                refused=refused,
            )
            prompt = " ".join(
                _call_llm(client, system=system, user=user, step="e9_visual_prompt").split()
            ).strip("\"“”")
            problem = prompt_problem(prompt)
            if problem is None:
                break
            refused = (prompt, problem)
        results.append(
            SlotPrompt(
                index=slot.index,
                start_seconds=str(slot.start_seconds),
                end_seconds=str(slot.end_seconds),
                text=slot.text,
                prompt=prompt,
                attempts=attempts,
                problem=problem,
            )
        )
    return results


def load_run(run_dir: Path) -> tuple[str, str, Decimal]:
    """Pauta, narração aprovada e duração do áudio de uma run, somente leitura."""
    bundle = json.loads((run_dir / "script" / "script.json").read_text(encoding="utf-8"))
    with wave.open(str(run_dir / "audio" / "narration.wav")) as audio:
        duration = Decimal(audio.getnframes()) / Decimal(audio.getframerate())
    return str(bundle["topic"]), str(bundle["result"]["narration"]), duration


def run(run_dir: Path, output: Path, *, count: int | None) -> Path:
    prompts_path = output / "prompts.json"
    if prompts_path.exists():
        raise SystemExit(f"{prompts_path} já existe; escolha outra pasta em --output")
    topic, narration, duration = load_run(run_dir)
    if count is None:
        count = visual_count_bounds(duration, word_count=len(narration.split())).default
    slots = plan_visual_slots(narration, duration_seconds=duration, count=count)
    prompts = write_prompts(_make_client(), topic=topic, narration=narration, slots=slots)
    output.mkdir(parents=True, exist_ok=True)
    prompts_path.write_text(
        json.dumps(
            {
                "experiment": "e9-visual-prompts",
                "scope": "quality test only; not approved for monetized content",
                "source_run_dir": run_dir.name,
                "writer_model": settings.ollama_model,
                "duration_seconds": str(duration.quantize(Decimal("0.01"))),
                "style": STYLE,
                "styles": STYLES,
                "slots": [asdict(prompt) for prompt in prompts],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return prompts_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--count", type=int, default=None)
    arguments = parser.parse_args()
    path = run(arguments.run_dir, arguments.output, count=arguments.count)
    print(f"descrições: {path.resolve()}")


if __name__ == "__main__":
    main()
