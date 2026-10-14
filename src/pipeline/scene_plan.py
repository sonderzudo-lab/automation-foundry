"""Contract for the scene plan of one video (A3).

Instead of describing each image on its own, the text model reads the whole
approved narration once and answers a plan: the recurring characters and
settings, and one scene per stretch of narration that refers to them. The plan
is what keeps images varied and consistent with each other.

The model decides only what is shown. The code decides how many scenes there
are (one per visual slot of the narration), which stretch each scene covers and
every timing; none of that is read from the answer. Every description passes the
same deterministic checks as before, so a plan never sends text, screen content,
logos or named people to the image model. The scenes must use at least three
different settings, so a video does not stay in one place. Human review of the
images stays mandatory.
"""

from __future__ import annotations

import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from src.pipeline.visual_prompts import scene_prompt_problem

SCENE_PLAN_SCHEMA_VERSION = 1
MAX_PLANNED_SCENES = 60
MAX_PLANNED_CHARACTERS = 4
MAX_PLANNED_SETTINGS = 8
MAX_CHARACTERS_PER_SCENE = 3
# Fewer scenes than this need one setting each.
MIN_SETTINGS_USED = 3

Shot = Literal["wide", "medium", "close_up", "detail"]

_ID = re.compile(r"^[a-z][a-z0-9_]{0,23}$")


class ScenePlanError(ValueError):
    """Raised when an answer is not an acceptable scene plan; the message says why."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlannedCharacter(_Strict):
    """A recurring, anonymous figure described by appearance only."""

    id: str = Field(pattern=_ID.pattern)
    description: str


class PlannedSetting(_Strict):
    """A recurring place described by its objects and layout."""

    id: str = Field(pattern=_ID.pattern)
    description: str


class PlannedScene(_Strict):
    """What one image shows, for the stretch of narration with the same index."""

    index: int = Field(ge=1, le=MAX_PLANNED_SCENES)
    description: str
    setting_id: str
    character_ids: list[str] = Field(max_length=MAX_CHARACTERS_PER_SCENE)
    shot: Shot


class ScenePlan(_Strict):
    """The visual plan of one whole video, as answered by the text model."""

    schema_version: Literal[1]
    characters: list[PlannedCharacter] = Field(max_length=MAX_PLANNED_CHARACTERS)
    settings: list[PlannedSetting] = Field(min_length=1, max_length=MAX_PLANNED_SETTINGS)
    scenes: list[PlannedScene] = Field(min_length=1, max_length=MAX_PLANNED_SCENES)

    @model_validator(mode="after")
    def _check_plan(self) -> ScenePlan:
        problem = _plan_problem(self)
        if problem is not None:
            raise ValueError(problem)
        return self


def scene_plan_response_schema() -> dict[str, Any]:
    """JSON Schema of the answer, passed to providers that support structured output."""
    return ScenePlan.model_json_schema()


def parse_scene_plan(answer: str, *, scene_count: int) -> ScenePlan:
    """Read a model answer as a plan with exactly ``scene_count`` scenes.

    Raises ``ScenePlanError`` with a short English reason that can be sent back
    to the model when asking again.
    """
    if not 1 <= scene_count <= MAX_PLANNED_SCENES:
        raise ValueError("scene_count is out of range")
    try:
        data = json.loads(_strip_code_fence(answer))
    except json.JSONDecodeError as exc:
        raise ScenePlanError("is not a single valid JSON object") from exc
    try:
        plan = ScenePlan.model_validate(data)
    except ValidationError as exc:
        raise ScenePlanError(_first_reason(exc)) from exc
    if len(plan.scenes) != scene_count:
        raise ScenePlanError(
            f"has {len(plan.scenes)} scenes; it must have exactly {scene_count}"
        )
    return plan


def scene_image_prompt(plan: ScenePlan, index: int) -> str:
    """The full scene sent to the image model, before the visual style is appended.

    The recurring character and setting descriptions are repeated in every
    prompt, which is what keeps the same figure and place recognisable across
    images generated independently.
    """
    scene = next((item for item in plan.scenes if item.index == index), None)
    if scene is None:
        raise KeyError(index)
    characters = {item.id: item.description for item in plan.characters}
    settings = {item.id: item.description for item in plan.settings}
    parts = [scene.description.rstrip(". ")]
    parts.extend(characters[character_id].rstrip(". ") for character_id in scene.character_ids)
    parts.append(settings[scene.setting_id].rstrip(". "))
    parts.append(f"{scene.shot.replace('_', ' ')} shot")
    return ". ".join(parts)


def _plan_problem(plan: ScenePlan) -> str | None:
    character_ids = [item.id for item in plan.characters]
    setting_ids = [item.id for item in plan.settings]
    if len(set(character_ids)) != len(character_ids):
        return "repeats a character id"
    if len(set(setting_ids)) != len(setting_ids):
        return "repeats a setting id"
    for kind, items in (("character", plan.characters), ("setting", plan.settings)):
        for item in items:
            problem = scene_prompt_problem(item.description)
            if problem is not None:
                return f"has a {kind} description that {problem}"
    if [scene.index for scene in plan.scenes] != list(range(1, len(plan.scenes) + 1)):
        return "does not number its scenes 1, 2, 3 and so on in order"
    seen: set[str] = set()
    previous: tuple[str, str] | None = None
    for scene in plan.scenes:
        problem = scene_prompt_problem(scene.description)
        if problem is not None:
            return f"has a description for scene {scene.index} that {problem}"
        if scene.setting_id not in setting_ids:
            return f"uses an unknown setting in scene {scene.index}"
        if any(character_id not in character_ids for character_id in scene.character_ids):
            return f"uses an unknown character in scene {scene.index}"
        if len(set(scene.character_ids)) != len(scene.character_ids):
            return f"repeats a character in scene {scene.index}"
        normalized = " ".join(scene.description.lower().split())
        if normalized in seen:
            return f"repeats the description of an earlier scene in scene {scene.index}"
        seen.add(normalized)
        current = (scene.setting_id, scene.shot)
        if current == previous:
            return (
                f"uses the same setting and shot in scenes {scene.index - 1} and "
                f"{scene.index}; consecutive scenes must differ"
            )
        previous = current
    used = len({scene.setting_id for scene in plan.scenes})
    needed = min(MIN_SETTINGS_USED, len(plan.scenes))
    if used < needed:
        return (
            f"places its scenes in {used} setting(s); the scenes must use at least "
            f"{needed} different settings"
        )
    return None


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
