"""Scene descriptions and visual styles for locally generated A3 visuals.

A generating visual backend needs, for each stretch of narration, an English
description of one scene. The scenes say what is shown and are written by the
text model from the approved narration, all at once as a scene plan (see
``scene_plan``) or one at a time when the reviewer sends an image back; the
visual style says how it looks, is chosen per video and is appended to every
scene unchanged.

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
_MAX_NOTE_CHARS = 300
_MAX_REFUSED_PLAN_CHARS = 8_000

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


def scene_plan_request(
    *,
    topic: str,
    stretches: Sequence[str],
    refused: tuple[str, str] | None = None,
) -> tuple[str, str]:
    """Ask for the visual plan of the whole video in one answer (see ``scene_plan``).

    The stretches are the approved narration in order, so the narration is not
    sent twice. A refused plan is sent back with the reason, as for one scene.
    """
    count = len(stretches)
    system = (
        # Qwen3 token that disables thinking mode; other models ignore the line.
        "/no_think\n"
        "You plan the images of a short video for a text-to-image model. You receive "
        f"the narration in Brazilian Portuguese, split into {count} numbered stretches; "
        "one image is shown while each stretch is spoken. Answer, in English, one JSON "
        "object with this shape:\n"
        '{"schema_version": 1, '
        '"characters": [{"id": "student", "description": "..."}], '
        '"settings": [{"id": "bedroom_desk", "description": "..."}], '
        '"scenes": [{"index": 1, "description": "...", "setting_id": "bedroom_desk", '
        '"character_ids": ["student"], "shot": "medium"}]}\n'
        "Rules:\n"
        f"- Exactly {count} scenes, numbered 1 to {count} in order. Scene N shows what "
        "stretch N says, so that a viewer connects the image to the speech.\n"
        "- characters: up to 4 recurring anonymous people, each described only by "
        "visible appearance (age range, hair, clothing) in 10 to 25 words. Leave the "
        "list empty when no person is needed. Never a real person or public figure.\n"
        "- settings: 1 to 8 recurring places, each described by its objects and layout "
        "in 10 to 25 words.\n"
        "- ids: lowercase letters, digits and underscores, starting with a letter.\n"
        "- Each scene description: one sentence of 15 to 40 words with what is visible "
        "and happening. Refer to people and places through setting_id and "
        "character_ids (at most 3 people per scene) instead of describing them again.\n"
        '- shot is one of "wide", "medium", "close_up" or "detail". Two consecutive '
        "scenes never share both the same setting and the same shot, and no scene "
        "repeats another.\n"
        "- Do not mention light, colours, mood, camera or art style: the visual style is "
        "added separately.\n"
        "- Describe only what is visible. Never say what is absent or hidden.\n"
        "- Nothing with readable content or a display: only blank paper and closed or "
        "blank notebooks, and no devices. No company or product name.\n"
        "- Never use these words: text, caption, headline, logo, brand, chart, graph, "
        "infographic, screenshot, watermark, poster, document, celebrity, screen, "
        "monitor, laptop, phone, smartphone, tablet, television, tv, style of.\n"
        "- Plain English only, without accents or special characters.\n"
        "Answer with the JSON object only."
    )
    numbered = "\n".join(
        f"{index}. {' '.join(stretch.split())}" for index, stretch in enumerate(stretches, 1)
    )
    user = (
        f"Video topic: {topic[:_MAX_TOPIC_CHARS]}\n\n"
        f"Narration in {count} stretches:\n{numbered}"
    )
    if refused is not None:
        user += (
            f"\n\nYour previous plan was refused because it {refused[1]}:\n"
            f"{refused[0][:_MAX_REFUSED_PLAN_CHARS]}\n"
            "Answer the whole plan again, fixing that."
        )
    return system, user


def revise_scene_prompt_request(
    *,
    topic: str,
    narration: str,
    stretch: str,
    previous_scene: str,
    note: str,
    other_scenes: Sequence[str],
    refused: tuple[str, str] | None = None,
) -> tuple[str, str]:
    """Ask for a new description of one image the reviewer sent back."""
    system, _user = scene_prompt_request(
        topic=topic, narration=narration, index=1, stretch=stretch, previous=other_scenes
    )
    used = "\n".join(f"- {scene}" for scene in other_scenes) or "- (none)"
    user = (
        f"Video topic: {topic[:_MAX_TOPIC_CHARS]}\n\n"
        f"Full narration:\n{narration[:_MAX_NARRATION_CHARS]}\n\n"
        f"Scenes of the other images, which stay as they are:\n{used}\n\n"
        f"Stretch to illustrate:\n{stretch}\n\n"
        "The image made from this description was sent back by the reviewer:\n"
        f"«{previous_scene}»\n"
        f"Reviewer's note (in Portuguese): «{note[:_MAX_NOTE_CHARS]}»\n"
        "Write a new description that follows the note while keeping every rule."
    )
    if refused is not None:
        user += (
            f"\n\nYour previous answer was refused because it {refused[1]}: "
            f"«{refused[0]}». Write a different prompt."
        )
    return system, user


def revise_scene_prompt(
    call: SceneModelCall,
    *,
    topic: str,
    narration: str,
    stretch: str,
    previous_scene: str,
    note: str,
    other_scenes: Sequence[str],
) -> str:
    """Rewrite one description from the reviewer's note, with the same checks."""
    refused: tuple[str, str] | None = None
    for _attempt in range(SCENE_PROMPT_ATTEMPTS):
        system, user = revise_scene_prompt_request(
            topic=topic,
            narration=narration,
            stretch=stretch,
            previous_scene=previous_scene,
            note=note,
            other_scenes=other_scenes,
            refused=refused,
        )
        prompt = " ".join(call(system, user).split()).strip("\"“”")
        problem = scene_prompt_problem(prompt)
        if problem is None:
            return prompt
        refused = (prompt, problem)
    raise ScenePromptError("no acceptable revised scene description")


def scene_from_prompt(prompt: str, style: VisualStyle) -> str:
    """Recover the scene description from a final prompt built by ``styled_prompt``."""
    return prompt.removesuffix(f", {VISUAL_STYLES[style]}")


def styled_prompt(scene: str, style: VisualStyle) -> str:
    """Join a scene description and the chosen visual style into the final prompt."""
    return f"{scene}, {VISUAL_STYLES[style]}"
