"""The motion of each animated scene, asked to the text model and checked in code."""

from __future__ import annotations

import json
import time
from decimal import Decimal

import pytest

from src.pipeline.motion_generation import (
    MotionScene,
    MotionTimeError,
    motion_plan_request,
    write_scene_motions,
)
from src.pipeline.scene_motion import MOTION_ATTEMPTS, NEUTRAL_MOTION
from src.pipeline.text_model import TextModelProviderName, TextModelRequest, TextModelResult

_GOOD = [
    "The adult slowly lifts the folded shirt from the bed while the camera pushes in",
    "Steam rises gently from the cup on the table as the camera pans to the left",
]
_SCENES = [
    MotionScene(index=1, description="An adult folding a shirt on a bed", narration_text="Dobre"),
    MotionScene(index=2, description="A cup of tea on a wooden table", narration_text="Beba"),
]


class _TextModel:
    provider: TextModelProviderName = "ollama"
    model_id = "qwen3:14b"

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.requests: list[TextModelRequest] = []

    def complete(self, request: TextModelRequest) -> TextModelResult:
        self.requests.append(request)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return TextModelResult(
            text=answer,
            provider=self.provider,
            model_id=self.model_id,
            duration_seconds=Decimal("1"),
        )


def _plan(*motions: str) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "scenes": [{"index": i, "motion": m} for i, m in enumerate(motions, start=1)],
        }
    )


def test_the_request_carries_each_description_and_stretch_with_the_schema() -> None:
    request = motion_plan_request(_SCENES)

    assert request.purpose == "scene_motion"
    assert "Scene 1\nImage: An adult folding a shirt on a bed\nNarration: Dobre" in request.user
    assert "Scene 2\nImage: A cup of tea" in request.user
    assert request.response_schema is not None
    assert "Nobody talks" in request.system


def test_an_accepted_plan_is_used_as_written() -> None:
    model = _TextModel(_plan(*_GOOD))

    written = write_scene_motions(model, _SCENES)

    assert written.motions == tuple(_GOOD)
    assert written.neutral == ()
    assert written.attempts == 1
    assert len(model.requests) == 1


def test_a_refusal_is_sent_back_and_accepted_scenes_are_kept() -> None:
    speaking = "The adult talks to the camera while folding the shirt slowly on the bed"
    model = _TextModel(_plan(speaking, _GOOD[1]), _plan(_GOOD[0], "short"))

    written = write_scene_motions(model, _SCENES)

    assert written.motions == tuple(_GOOD)
    assert written.neutral == ()
    retry = model.requests[1].user
    assert "was refused because it has a motion for scene 1" in retry
    assert "the narration is the only voice" in retry


def test_a_scene_never_accepted_gets_the_neutral_motion() -> None:
    crowd = "A crowd walks into the room behind the adult who keeps folding the shirt"
    model = _TextModel(_plan(crowd, _GOOD[1]))

    written = write_scene_motions(model, _SCENES)

    assert written.motions == (NEUTRAL_MOTION, _GOOD[1])
    assert written.neutral == (1,)
    assert written.attempts == MOTION_ATTEMPTS
    assert len(model.requests) == MOTION_ATTEMPTS


def test_an_answer_that_is_not_json_leaves_every_scene_neutral() -> None:
    written = write_scene_motions(_TextModel("not json at all"), _SCENES)

    assert written.motions == (NEUTRAL_MOTION, NEUTRAL_MOTION)
    assert written.neutral == (1, 2)


def test_no_request_starts_after_the_deadline() -> None:
    model = _TextModel(_plan(*_GOOD))

    with pytest.raises(MotionTimeError):
        write_scene_motions(model, _SCENES, deadline=time.monotonic() - 1)

    assert model.requests == []


def test_scenes_must_be_numbered_in_order() -> None:
    with pytest.raises(ValueError, match="numbered"):
        write_scene_motions(_TextModel(_plan(*_GOOD)), list(reversed(_SCENES)))
