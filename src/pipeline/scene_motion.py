"""Contract for the motion of each animated scene, answered by the text model.

The image of a scene is already approved; the video model only needs to know
what moves in it. The text model writes one short English phrase per scene
("the adult slowly lifts the shirt while the camera pushes in"), and the code
checks it here before any clip is generated.

The phrase is content, never timing: how long each clip lasts comes from the
A4 words, so numbers are refused. It follows the same on-screen rules as the
scene descriptions (no text, logos or visible screens) and three rules of its
own: nobody speaks, because the narration is the only voice and a moving mouth
would not match it; nobody new enters, because the person must stay the one in
the approved image; and the shot is continuous, without cuts or transformations.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.pipeline.scene_plan import MAX_PLANNED_SCENES
from src.pipeline.visual_prompts import on_screen_problem

SCENE_MOTION_SCHEMA_VERSION = 1
MOTION_WORD_RANGE = (6, 50)
MOTION_ATTEMPTS = 3
# Used for a scene whose motion was refused MOTION_ATTEMPTS times.
NEUTRAL_MOTION = "subtle natural movement in the scene, slow and steady camera push-in"

_SPEECH_RE = re.compile(
    r"\b(talk|speak|say|sing|shout|whisper|chat|argue)(s|ing|ed)?\b"
    r"|\b(said|spoke|spoken|sang)\b"
    r"|\blip[- ]?sync\w*|\bmouth (moves|opens|moving)\b",
    re.IGNORECASE,
)
_NEW_PERSON_RE = re.compile(
    r"\b(another|second|other|new|more) (person|people|man|men|woman|women|figure|adult|child"
    r"|kid|friend)s?\b"
    r"|\b(someone|somebody|stranger|strangers|people|a crowd) (enters?|appears?|joins?|walks? in)\b"
    r"|\bcrowd\b",
    re.IGNORECASE,
)
_CUT_RE = re.compile(
    r"\b(cut|cuts) to\b|\btransition\w*|\bfade\w*|\bmorph\w*|\btransform\w*"
    r"|\bscene (changes?|switches?)\b|\bflashback\b",
    re.IGNORECASE,
)


class SceneMotionError(ValueError):
    """Raised when an answer is not an acceptable motion plan; the message says why."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlannedMotion(_Strict):
    """What moves in the scene with the same index, and how the camera moves."""

    index: int = Field(ge=1, le=MAX_PLANNED_SCENES)
    motion: str = Field(min_length=1, max_length=300)


class MotionPlan(_Strict):
    """The motion of every scene of one video, as answered by the text model."""

    schema_version: Literal[1]
    scenes: list[PlannedMotion] = Field(min_length=1, max_length=MAX_PLANNED_SCENES)


def motion_plan_response_schema() -> dict[str, Any]:
    """JSON Schema of the answer, passed to providers that support structured output."""
    return MotionPlan.model_json_schema()


def motion_problem(motion: str) -> str | None:
    """Return why the motion of one scene is refused, or None when it is acceptable."""
    words = sum(1 for token in motion.split() if any(char.isalnum() for char in token))
    if not MOTION_WORD_RANGE[0] <= words <= MOTION_WORD_RANGE[1]:
        return (
            f"has {words} words; it must have between {MOTION_WORD_RANGE[0]} and "
            f"{MOTION_WORD_RANGE[1]}"
        )
    if not motion.isascii():
        return "is not written in plain English"
    if any(char.isdigit() for char in motion):
        return "contains a number; how long the scene lasts comes from the narration"
    for pattern, rule in (
        (_SPEECH_RE, "makes someone speak; the narration is the only voice"),
        (_NEW_PERSON_RE, "brings in someone new; only the people in the image may appear"),
        (_CUT_RE, "describes a cut or a transformation; each scene is one continuous shot"),
    ):
        found = pattern.search(motion)
        if found is not None:
            return f"mentions '{found.group(0).lower()}', which {rule}"
    return on_screen_problem(motion)


def parse_motion_plan(answer: str, *, scene_count: int) -> MotionPlan:
    """Read a model answer as the motion of exactly ``scene_count`` scenes.

    Raises ``SceneMotionError`` with a short English reason that can be sent back
    to the model when asking again.
    """
    if not 1 <= scene_count <= MAX_PLANNED_SCENES:
        raise ValueError("scene_count is out of range")
    try:
        data = json.loads(_strip_code_fence(answer))
    except json.JSONDecodeError as exc:
        raise SceneMotionError("is not a single valid JSON object") from exc
    try:
        plan = MotionPlan.model_validate(data)
    except ValidationError as exc:
        raise SceneMotionError(_first_reason(exc)) from exc
    if len(plan.scenes) != scene_count:
        raise SceneMotionError(f"has {len(plan.scenes)} scenes; it must have exactly {scene_count}")
    if [scene.index for scene in plan.scenes] != list(range(1, scene_count + 1)):
        raise SceneMotionError("does not number its scenes 1, 2, 3 and so on in order")
    for scene in plan.scenes:
        problem = motion_problem(scene.motion)
        if problem is not None:
            raise SceneMotionError(f"has a motion for scene {scene.index} that {problem}")
    return plan


def _strip_code_fence(answer: str) -> str:
    text = answer.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    return text.strip()


def _first_reason(exc: ValidationError) -> str:
    error = exc.errors(include_url=False)[0]
    message = str(error.get("msg", "is invalid")).removeprefix("Value error, ")
    location = ".".join(str(part) for part in error.get("loc", ()))
    return f"is invalid at '{location}': {message}" if location else message
