"""Contract of the engine-independent A5 timeline."""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from src.pipeline.timeline import (
    Timeline,
    milliseconds,
    timeline_json_schema,
    timeline_to_json,
)

_SCHEMA_FILE = (
    Path(__file__).resolve().parents[2] / "docs" / "reference" / "schemas" / "timeline-v1.schema.json"
)
_EXAMPLE_FILE = _SCHEMA_FILE.with_name("timeline-v1.example.json")
_HASH = "a" * 64


def _timeline() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "format": "short",
        "width": 1080,
        "height": 1920,
        "fps": 30,
        "duration_ms": 9_000,
        "audio_sha256": _HASH,
        "caption_ass_sha256": "b" * 64,
        "caption_template": "word_pop",
        "transition_ms": 400,
        "clips": [
            {"index": 1, "image": "visual-001.png", "image_sha256": "c" * 64,
             "start_ms": 0, "end_ms": 4_000, "effect": "zoom_in", "transition_in": "cut"},
            {"index": 2, "image": "visual-002.png", "image_sha256": "d" * 64,
             "start_ms": 4_000, "end_ms": 9_000, "effect": "pan_left",
             "transition_in": "crossfade"},
        ],
        "words": [
            {"text": "Organize", "start_ms": 100, "end_ms": 600},
            {"text": "a", "start_ms": 600, "end_ms": 700},
            {"text": "mesa.", "start_ms": 700, "end_ms": 1_200},
        ],
    }


def _with(change: dict[str, Any], clip: int | None = None) -> dict[str, Any]:
    data = copy.deepcopy(_timeline())
    target = data if clip is None else data["clips"][clip]
    target.update(change)
    return data


def test_a_consistent_timeline_is_accepted() -> None:
    timeline = Timeline.model_validate(_timeline())

    assert timeline.clips[-1].end_ms == timeline.duration_ms


def test_canonical_json_is_stable() -> None:
    timeline = Timeline.model_validate(_timeline())

    assert timeline_to_json(timeline) == timeline_to_json(Timeline.model_validate(_timeline()))
    assert Timeline.model_validate_json(timeline_to_json(timeline)) == timeline


@pytest.mark.parametrize(
    ("data", "fragment"),
    [
        (_with({"width": 1920, "height": 1080}), "dimensions"),
        (_with({"start_ms": 100}, clip=0), "first clip does not start at zero"),
        (_with({"transition_in": "crossfade"}, clip=0), "first clip must start with a cut"),
        (_with({"duration_ms": 9_500}), "last clip does not end"),
        (_with({"start_ms": 4_100}, clip=1), "does not start where"),
        (_with({"end_ms": 600}, clip=0) | {"clips": [
            {**_timeline()["clips"][0], "end_ms": 600},
            {**_timeline()["clips"][1], "start_ms": 600},
        ]}, "shorter than"),
        (_with({"transition_ms": 0}), "needs a transition length"),
        (_with({"transition_ms": 1_000, "duration_ms": 5_500}) | {"clips": [
            {**_timeline()["clips"][0], "end_ms": 1_500},
            {**_timeline()["clips"][1], "start_ms": 1_500, "end_ms": 5_500},
        ]}, "longer than half a clip"),
        (_with({"words": [{"text": "a", "start_ms": 500, "end_ms": 400}]}), "ends before"),
        (_with({"words": [
            {"text": "a", "start_ms": 0, "end_ms": 500},
            {"text": "b", "start_ms": 400, "end_ms": 600},
        ]}), "starts before the previous"),
        (_with({"words": [{"text": "a", "start_ms": 0, "end_ms": 9_001}]}), "after the audio"),
        (_with({"image": "../visual-001.png"}, clip=0), "pattern"),
        (_with({"caption_template": "karaoke"}), "caption_template"),
        (_with({"fps": 60}), "fps"),
    ],
)
def test_an_inconsistent_timeline_is_refused(data: dict[str, Any], fragment: str) -> None:
    with pytest.raises(ValidationError, match=fragment):
        Timeline.model_validate(data)


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(Decimal("1.2345"), 1234), (Decimal("1.2355"), 1236), ("0.5", 500), (2, 2000)],
)
def test_seconds_round_to_whole_milliseconds(seconds: object, expected: int) -> None:
    assert milliseconds(seconds) == expected


def test_committed_schema_matches_the_model() -> None:
    committed = json.loads(_SCHEMA_FILE.read_text(encoding="utf-8"))

    assert committed == timeline_json_schema()


def test_committed_example_is_a_valid_timeline() -> None:
    Timeline.model_validate_json(_EXAMPLE_FILE.read_text(encoding="utf-8"))
