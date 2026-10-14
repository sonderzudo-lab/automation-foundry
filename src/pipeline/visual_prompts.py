"""Scene descriptions and visual styles for locally generated A3 visuals.

A generating visual backend needs, for each stretch of narration, an English
description of one scene. The scene says what is shown and is written by the
script model from the approved narration; the visual style says how it looks,
is chosen per video and is appended to every scene unchanged.

Every description passes deterministic checks before it reaches an image model.
A generated image is not a factual source: it must not show text, data, charts,
documents or screens, and no style names an artist, studio, work or character.
The checks are a filter, not proof; human review of the images stays mandatory.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Literal

VisualStyle = Literal["photo", "cartoon", "dark"]
DEFAULT_VISUAL_STYLE: VisualStyle = "photo"

_NO_TEXT = "no text, no letters, no logos, no watermark"
VISUAL_STYLES: dict[VisualStyle, str] = {
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
VISUAL_STYLE_LABELS: dict[VisualStyle, str] = {
    "photo": "Fotografia realista",
    "cartoon": "Desenho",
    "dark": "Escuro e cinematográfico",
}

SCENE_PROMPT_ATTEMPTS = 4
SCENE_PROMPT_WORD_RANGE = (8, 60)
_MAX_TOPIC_CHARS = 500
_MAX_NARRATION_CHARS = 20_000

_BANNED_RE = re.compile(
    r"\b(text|caption|headline|logo|brand|chart|graph|infographic|screenshot|watermark"
    r"|poster|document|celebrity|screen|monitor|laptop|phone|smartphone|tablet"
    r"|television|tv)s?\b"
    r"|\bstyle of\b",
    re.IGNORECASE,
)

# (system, user) -> model answer. Injected so tests and adapters choose the client.
SceneModelCall = Callable[[str, str], str]


class ScenePromptError(RuntimeError):
    """Raised when no acceptable scene description could be written for a stretch."""


def scene_prompt_problem(prompt: str) -> str | None:
    """Return why a scene description is refused, or None when it is acceptable."""
    words = sum(1 for token in prompt.split() if any(char.isalnum() for char in token))
    if not SCENE_PROMPT_WORD_RANGE[0] <= words <= SCENE_PROMPT_WORD_RANGE[1]:
        return (
            f"has {words} words; it must have between {SCENE_PROMPT_WORD_RANGE[0]} and "
            f"{SCENE_PROMPT_WORD_RANGE[1]}"
        )
    if not prompt.isascii():
        return "is not written in plain English"
    banned = _BANNED_RE.search(prompt)
    if banned is not None:
        return f"mentions '{banned.group(0).lower()}', which must not be mentioned at all"
    return None


def scene_prompt_request(
    *,
    topic: str,
    narration: str,
    index: int,
    stretch: str,
    previous: Sequence[str],
    refused: tuple[str, str] | None = None,
) -> tuple[str, str]:
    """Build the request for one scene; in English because the answer is in English."""
    system = (
        # Qwen3 token that disables thinking mode; other models ignore the line.
        "/no_think\n"
        "You write prompts for a text-to-image model. You receive the narration of a "
        "short video in Brazilian Portuguese and ONE stretch of it. Describe, in "
        "English, one concrete scene that shows what that stretch says, so that a "
        "viewer connects the image to the speech.\n"
        "Rules:\n"
        "- One sentence, 15 to 40 words: objects, setting and framing only. Do not "
        "mention light, colours, mood, camera or art style: the visual style is added "
        "separately.\n"
        "- Describe only what is visible. Never say what is absent or hidden.\n"
        "- Nothing with readable content or a display: only blank paper and closed or "
        "blank notebooks, and no devices.\n"
        "- No real person, public figure, company or product name. If someone is "
        "needed, show only hands or an anonymous figure from behind.\n"
        "- Do not repeat a scene already used for another stretch.\n"
        "Answer with the prompt only, without quotes or comments."
    )
    used = "\n".join(f"- {prompt}" for prompt in previous) or "- (none yet)"
    user = (
        f"Video topic: {topic[:_MAX_TOPIC_CHARS]}\n\n"
        f"Full narration:\n{narration[:_MAX_NARRATION_CHARS]}\n\n"
        f"Scenes already used:\n{used}\n\n"
        f"Stretch to illustrate (number {index}):\n{stretch}"
    )
    if refused is not None:
        user += (
            f"\n\nYour previous answer was refused because it {refused[1]}: "
            f"«{refused[0]}». Write a different prompt."
        )
    return system, user


def write_scene_prompts(
    call: SceneModelCall,
    *,
    topic: str,
    narration: str,
    stretches: Sequence[str],
) -> tuple[str, ...]:
    """Write one checked scene description per stretch of narration.

    A refused description is asked again with the reason, up to the attempt
    ceiling; a stretch that still has no acceptable description raises
    ``ScenePromptError`` instead of sending an unchecked prompt to the image model.
    """
    prompts: list[str] = []
    for index, stretch in enumerate(stretches, start=1):
        refused: tuple[str, str] | None = None
        for _attempt in range(SCENE_PROMPT_ATTEMPTS):
            system, user = scene_prompt_request(
                topic=topic,
                narration=narration,
                index=index,
                stretch=stretch,
                previous=prompts,
                refused=refused,
            )
            prompt = " ".join(call(system, user).split()).strip("\"“”")
            problem = scene_prompt_problem(prompt)
            if problem is None:
                prompts.append(prompt)
                break
            refused = (prompt, problem)
        else:
            raise ScenePromptError(f"no acceptable scene description for stretch {index}")
    return tuple(prompts)


def styled_prompt(scene: str, style: VisualStyle) -> str:
    """Join a scene description and the chosen visual style into the final prompt."""
    return f"{scene}, {VISUAL_STYLES[style]}"
