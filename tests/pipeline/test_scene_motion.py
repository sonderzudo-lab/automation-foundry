"""Contract of the motion phrase of each animated scene."""

from __future__ import annotations

import json

import pytest

from src.pipeline.scene_motion import (
    NEUTRAL_MOTION,
    SceneMotionError,
    motion_plan_response_schema,
    motion_problem,
    parse_motion_plan,
)
from src.pipeline.visual_prompts import scene_prompt_problem

# Phrases that animated run 19 well in the lab, kept as accepted examples.
_LAB_MOTIONS = [
    "The adult keeps holding the shirt firmly in one hand the whole time while the other "
    "hand picks up the phone from the bedside table, the phone screen stays dark and facing "
    "away from the viewer, slight slow push-in of the camera",
    "The adult walks forward through the bedroom doorway into the dim corridor with calm "
    "natural steps, the camera slowly follows at a distance",
    "Both hands gently push the folded shirt onto the stack inside the wardrobe and smooth "
    "its edges, fabric moves naturally, slow subtle camera push-in",
]


def _answer(*motions: str) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "scenes": [
                {"index": index, "motion": motion}
                for index, motion in enumerate(motions, start=1)
            ],
        }
    )


@pytest.mark.parametrize("motion", [*_LAB_MOTIONS, NEUTRAL_MOTION])
def test_lab_motions_and_the_neutral_motion_are_accepted(motion: str) -> None:
    assert motion_problem(motion) is None


@pytest.mark.parametrize(
    ("motion", "fragment"),
    [
        ("slow push-in", "between 6 and 50"),
        ("the adult slowly turns their head toward the window, camera pans " * 5, "between"),
        ("A pessoa caminha devagar pelo corredor escuro até a sala", "plain English"),
        ("the adult walks for 3 seconds down the corridor slowly", "contains a number"),
        ("the adult looks at the camera and says hello with a smile", "'says'"),
        ("the adult is talking quietly while folding the shirt", "'talking'"),
        ("the adult waves as another person enters the bedroom slowly", "someone new"),
        ("someone enters from the left while the adult folds the shirt", "someone new"),
        ("the room fades to black while the camera pulls back slowly", "'fades'"),
        ("the shirt morphs into a folded stack while the camera holds", "continuous shot"),
        ("the adult scrolls the phone and the bright screen lights the face", "'phone'"),
        ("the adult points at a poster on the wall behind the bed", "'poster'"),
    ],
)
def test_unacceptable_motions_are_refused_with_a_reason(motion: str, fragment: str) -> None:
    problem = motion_problem(motion)

    assert problem is not None
    assert fragment in problem


def test_scene_descriptions_keep_the_same_on_screen_rules() -> None:
    assert "'poster'" in (scene_prompt_problem("an adult points at a poster on the wall") or "")
    assert "'phone'" in (
        scene_prompt_problem("an adult holds a phone near the bright window light") or ""
    )


def test_a_valid_answer_is_read_in_scene_order() -> None:
    plan = parse_motion_plan(f"```json\n{_answer(*_LAB_MOTIONS)}\n```", scene_count=3)

    assert [scene.index for scene in plan.scenes] == [1, 2, 3]
    assert plan.scenes[1].motion == _LAB_MOTIONS[1]


@pytest.mark.parametrize(
    ("answer", "fragment"),
    [
        ("not json", "valid JSON"),
        (json.dumps({"schema_version": 2, "scenes": []}), "schema_version"),
        (_answer(*_LAB_MOTIONS[:2]), "exactly 3"),
        (
            json.dumps(
                {
                    "schema_version": 1,
                    "scenes": [
                        {"index": 2, "motion": _LAB_MOTIONS[0]},
                        {"index": 1, "motion": _LAB_MOTIONS[1]},
                        {"index": 3, "motion": _LAB_MOTIONS[2]},
                    ],
                }
            ),
            "in order",
        ),
        (_answer(_LAB_MOTIONS[0], "the adult says goodbye and leaves the room", _LAB_MOTIONS[2]),
         "scene 2"),
        (json.dumps({"schema_version": 1, "scenes": [{"index": 1, "motion": "x", "seed": 4}]}),
         "seed"),
    ],
)
def test_invalid_answers_are_refused_with_a_reason(answer: str, fragment: str) -> None:
    with pytest.raises(SceneMotionError, match=fragment):
        parse_motion_plan(answer, scene_count=3)


def test_scene_count_out_of_range_is_a_caller_error() -> None:
    with pytest.raises(ValueError, match="scene_count"):
        parse_motion_plan(_answer(*_LAB_MOTIONS), scene_count=0)


def test_response_schema_describes_the_answer() -> None:
    schema = motion_plan_response_schema()

    assert set(schema["properties"]) == {"schema_version", "scenes"}
