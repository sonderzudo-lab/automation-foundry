"""Ask the configured text model for the motion of every animated scene (A5a).

The image of each scene is already approved in A3; the model only says what
moves in it, from the description used to draw the image and the stretch of
narration it covers. The answer is one ``MotionPlan`` (see ``scene_motion``).

A refused answer is sent back with the reason, as for the scene plan. The plan is
checked as a whole by ``parse_motion_plan``; when it is refused, every scene whose
own phrase passes ``motion_problem`` is kept, so one bad phrase does not throw the
others away. After ``MOTION_ATTEMPTS`` answers, a scene that never got an
acceptable phrase uses ``NEUTRAL_MOTION``: the video still moves, gently, and
nothing unchecked reaches the video model.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass

import structlog
from pydantic import ValidationError

from src.pipeline.scene_motion import (
    MOTION_ATTEMPTS,
    NEUTRAL_MOTION,
    MotionPlan,
    SceneMotionError,
    motion_plan_response_schema,
    motion_problem,
    parse_motion_plan,
)
from src.pipeline.text_model import TextModel, TextModelRequest, TextModelResult
from src.pipeline.text_model_factory import configured_text_model

log = structlog.get_logger(__name__)

MOTION_MODEL_TIMEOUT_SECONDS = 120.0
_MAX_DESCRIPTION_CHARS = 1_500
_MAX_REFUSED_PLAN_CHARS = 4_000
_NO_DESCRIPTION = "(no written description; the operator chose this image)"

_SYSTEM = (
    # Qwen3 token that disables thinking mode; other models ignore the line.
    "/no_think\n"
    "You direct the motion of short video clips made from still images. Each image "
    "is already approved and becomes the first frame of its clip. You receive, for "
    "every scene, the description used to draw the image and the stretch of narration "
    "(in Brazilian Portuguese) heard while it is on screen. Answer one JSON object "
    "with this shape:\n"
    '{"schema_version": 1, "scenes": [{"index": 1, "motion": "..."}]}\n'
    "Rules for each motion:\n"
    "- One sentence in plain English, 6 to 50 words, saying what moves in the image "
    "and how the camera moves (for example a slow push-in or a gentle pan).\n"
    "- Only the people and objects already in the image. Nobody new enters.\n"
    "- Nobody talks, sings or moves their lips: the narration is the only voice.\n"
    "- One continuous shot: no cuts, fades, transitions or transformations.\n"
    "- No numbers or durations, no text, letters, logos, charts or visible screens.\n"
    "- Calm, natural and small movements that fit the narrated stretch.\n"
    "A scene may come with the motion it had and a note from the operator, in any "
    "language, saying what to change; write a new motion that follows the note and "
    "still obeys every rule above.\n"
    "Write exactly one motion per scene, numbered 1, 2, 3 in order. Answer with the "
    "JSON object only."
)


class MotionTimeError(TimeoutError):
    """The time for the motion plan ran out before every scene had an answer."""


@dataclass(frozen=True, slots=True)
class MotionScene:
    """What the model reads about one scene."""

    index: int
    description: str
    narration_text: str
    # Set when the operator sent the scene back: the motion it had and what to change.
    previous_motion: str = ""
    note: str = ""


@dataclass(frozen=True, slots=True)
class WrittenMotions:
    """The motion of every scene, in order, and how it was obtained.

    ``neutral`` lists the scenes that use ``NEUTRAL_MOTION`` because no answer
    gave them an acceptable phrase. ``result`` is the last answer of the model.
    """

    motions: tuple[str, ...]
    neutral: tuple[int, ...]
    attempts: int
    result: TextModelResult | None


def motion_text_model() -> TextModel:
    """The configured text model (Ollama, or the ChatGPT plan with Ollama in reserve)."""
    return configured_text_model(ollama_timeout_seconds=MOTION_MODEL_TIMEOUT_SECONDS)


def motion_plan_request(
    scenes: Sequence[MotionScene],
    *,
    refused: tuple[str, str] | None = None,
) -> TextModelRequest:
    """The request for the motion of every scene; a refused answer is sent back."""
    blocks = [
        f"Scene {scene.index}\n"
        f"Image: {(scene.description.strip() or _NO_DESCRIPTION)[:_MAX_DESCRIPTION_CHARS]}\n"
        f"Narration: {scene.narration_text.strip()}"
        + (
            f"\nPrevious motion: {scene.previous_motion.strip()}"
            f"\nNote from the operator on that motion (change it accordingly): "
            f"{scene.note.strip()}"
            if scene.note.strip()
            else ""
        )
        for scene in scenes
    ]
    user = "\n\n".join(blocks)
    if refused is not None:
        user += (
            f"\n\nYour previous answer was refused because it {refused[1]}:\n"
            f"{refused[0][:_MAX_REFUSED_PLAN_CHARS]}\n"
            "Answer the motion of every scene again, fixing that."
        )
    return TextModelRequest(
        purpose="scene_motion",
        system=_SYSTEM,
        user=user,
        response_schema=motion_plan_response_schema(),
    )


def write_scene_motions(
    model: TextModel,
    scenes: Sequence[MotionScene],
    *,
    deadline: float | None = None,
) -> WrittenMotions:
    """Return one checked motion per scene, asking again with each refusal.

    No attempt starts after ``deadline`` (a ``time.monotonic()`` value): when the
    first one cannot start, ``MotionTimeError``; after that, the scenes still
    without a phrase use ``NEUTRAL_MOTION``. Raises ``ValueError`` when the scenes
    are not numbered 1, 2, 3 or a request would be too long. Text model failures
    (unreachable, not signed in) propagate unchanged.
    """
    count = len(scenes)
    if not count or [scene.index for scene in scenes] != list(range(1, count + 1)):
        raise ValueError("scenes must be numbered 1, 2, 3 and so on in order")
    accepted: dict[int, str] = {}
    refused: tuple[str, str] | None = None
    result: TextModelResult | None = None
    attempts = 0
    for attempt in range(1, MOTION_ATTEMPTS + 1):
        if deadline is not None and time.monotonic() >= deadline:
            if attempt == 1:
                raise MotionTimeError("no time left to ask for the motion plan")
            break
        request = motion_plan_request(scenes, refused=refused)
        result = model.complete(request)
        attempts = attempt
        try:
            plan = parse_motion_plan(result.text, scene_count=count)
        except SceneMotionError as exc:
            # Keep every phrase that passes on its own; the first accepted one wins.
            for index, motion in _acceptable_motions(result.text, scene_count=count).items():
                accepted.setdefault(index, motion)
            if len(accepted) == count:
                break
            # The reason is written by the checks, never copied from the answer.
            log.warning("scene_motion_refused", attempt=attempt, reason=str(exc))
            refused = (result.text, str(exc))
            continue
        for scene in plan.scenes:
            accepted.setdefault(scene.index, scene.motion)
        break
    neutral = tuple(index for index in range(1, count + 1) if index not in accepted)
    if neutral:
        log.warning("scene_motion_neutral", scenes=list(neutral), attempts=attempts)
    return WrittenMotions(
        motions=tuple(accepted.get(index, NEUTRAL_MOTION) for index in range(1, count + 1)),
        neutral=neutral,
        attempts=attempts,
        result=result,
    )


def _acceptable_motions(answer: str, *, scene_count: int) -> dict[int, str]:
    """The phrases of a refused answer that pass ``motion_problem`` one by one."""
    text = answer.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    try:
        plan = MotionPlan.model_validate(json.loads(text))
    except (ValueError, ValidationError):
        return {}
    found: dict[int, str] = {}
    for scene in plan.scenes:
        if scene.index > scene_count or scene.index in found:
            continue
        if motion_problem(scene.motion) is None:
            found[scene.index] = scene.motion
    return found
