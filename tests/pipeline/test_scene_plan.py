"""Contract of the whole-video scene plan answered by the text model."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from src.pipeline.scene_plan import (
    ScenePlanError,
    parse_scene_plan,
    scene_image_prompt,
    scene_plan_response_schema,
)

_PLAN: dict[str, Any] = {
    "schema_version": 1,
    "characters": [
        {
            "id": "student",
            "description": "A young adult with short curly hair wearing a grey hoodie and jeans",
        }
    ],
    "settings": [
        {
            "id": "bedroom_desk",
            "description": "A small bedroom with a wooden desk under a window and a single shelf",
        },
        {
            "id": "kitchen",
            "description": "A compact kitchen with a round table, two chairs and a fruit bowl",
        },
    ],
    "scenes": [
        {
            "index": 1,
            "description": "A person drops a pile of loose pens into a ceramic cup on the desk",
            "setting_id": "bedroom_desk",
            "character_ids": ["student"],
            "shot": "medium",
        },
        {
            "index": 2,
            "description": "Hands place a closed notebook and a single pencil side by side",
            "setting_id": "bedroom_desk",
            "character_ids": [],
            "shot": "detail",
        },
        {
            "index": 3,
            "description": "A person sits at the round table with a mug and a blank sheet of paper",
            "setting_id": "kitchen",
            "character_ids": ["student"],
            "shot": "wide",
        },
    ],
}


def _answer(plan: dict[str, Any]) -> str:
    return json.dumps(plan)


def test_a_consistent_plan_is_accepted() -> None:
    plan = parse_scene_plan(_answer(_PLAN), scene_count=3)

    assert [scene.index for scene in plan.scenes] == [1, 2, 3]


def test_a_plan_inside_a_code_fence_is_accepted() -> None:
    plan = parse_scene_plan(f"```json\n{_answer(_PLAN)}\n```", scene_count=3)

    assert len(plan.scenes) == 3


def test_image_prompt_repeats_the_character_and_setting() -> None:
    plan = parse_scene_plan(_answer(_PLAN), scene_count=3)

    prompt = scene_image_prompt(plan, 3)

    assert prompt.startswith("A person sits at the round table")
    assert "grey hoodie" in prompt
    assert "compact kitchen" in prompt
    assert prompt.endswith("wide shot")


def _broken(path: tuple[Any, ...], value: Any) -> dict[str, Any]:
    plan = copy.deepcopy(_PLAN)
    target: Any = plan
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return plan


@pytest.mark.parametrize(
    ("answer", "scene_count", "fragment"),
    [
        ("not json", 3, "valid JSON"),
        (_answer(_PLAN), 4, "exactly 4"),
        (_answer(_broken(("scenes", 1, "index"), 3)), 3, "in order"),
        (_answer(_broken(("scenes", 1, "setting_id"), "garage")), 3, "unknown setting"),
        (_answer(_broken(("scenes", 0, "character_ids"), ["ghost"])), 3, "unknown character"),
        (
            _answer(_broken(("scenes", 1, "description"), _PLAN["scenes"][0]["description"])),
            3,
            "repeats the description",
        ),
        (_answer(_broken(("scenes", 1, "shot"), "medium")), 3, "consecutive scenes must differ"),
        (
            _answer(_broken(("scenes", 0, "description"), "A desk with a laptop and pens on it")),
            3,
            "'laptop'",
        ),
        (
            _answer(_broken(("characters", 0, "description"), "A famous celebrity at a desk")),
            3,
            "character description",
        ),
        (_answer(_broken(("scenes", 0, "shot"), "drone")), 3, "shot"),
        (_answer({**_PLAN, "extra": True}), 3, "extra"),
    ],
)
def test_an_unacceptable_plan_says_why(answer: str, scene_count: int, fragment: str) -> None:
    with pytest.raises(ScenePlanError, match=fragment):
        parse_scene_plan(answer, scene_count=scene_count)


def test_response_schema_forbids_extra_fields() -> None:
    schema = scene_plan_response_schema()

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"schema_version", "characters", "settings", "scenes"}
