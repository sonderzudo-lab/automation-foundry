"""Contract of the v3 timeline: v2 whose clips may play generated videos."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from src.pipeline.timeline import (
    TimelineV3,
    clip_window,
    timeline_to_json,
    timeline_v3_json_schema,
)

_SCHEMAS = Path(__file__).resolve().parents[2] / "docs" / "reference" / "schemas"


def _timeline() -> dict[str, Any]:
    return json.loads((_SCHEMAS / "timeline-v3.example.json").read_text(encoding="utf-8"))


def _with_clip(position: int, **changes: Any) -> dict[str, Any]:
    data = copy.deepcopy(_timeline())
    data["clips"][position].update(changes)
    return data


def test_a_timeline_mixing_a_video_and_an_image_is_accepted() -> None:
    timeline = TimelineV3.model_validate(_timeline())

    assert [clip.video for clip in timeline.clips] == ["scene-001.mp4", None]
    assert TimelineV3.model_validate_json(timeline_to_json(timeline)) == timeline


def test_v2_rules_still_apply_to_v3() -> None:
    data = _with_clip(1, start_ms=6_100)

    with pytest.raises(ValidationError, match="does not start where"):
        TimelineV3.model_validate(data)
    overlays = copy.deepcopy(_timeline())
    overlays["overlays"][0]["end_ms"] = overlays["overlays"][0]["start_ms"] + 500
    with pytest.raises(ValidationError, match="shorter than"):
        TimelineV3.model_validate(overlays)


def test_a_video_may_be_played_up_to_a_quarter_slower() -> None:
    start, end = clip_window(0, 6_000, duration_ms=12_000, transition_ms=400)
    assert (start, end) == (0, 6_200)

    TimelineV3.model_validate(_with_clip(0, video_ms=4_960))
    with pytest.raises(ValidationError, match="too short for its window"):
        TimelineV3.model_validate(_with_clip(0, video_ms=4_959))


@pytest.mark.parametrize(
    ("data", "fragment"),
    [
        (_with_clip(0, video_sha256=None), "together"),
        (_with_clip(1, video_ms=6_000), "together"),
        (_with_clip(0, effect="zoom_in"), "effect none"),
        (_with_clip(0, video="scene-001.webm"), "video"),
        (_with_clip(1, video="scene-001.mp4", video_sha256="e" * 64, video_ms=6_440,
                    effect="none"), "same video"),
    ],
)
def test_inconsistent_videos_are_refused(data: dict[str, Any], fragment: str) -> None:
    with pytest.raises(ValidationError, match=fragment):
        TimelineV3.model_validate(data)


def test_a_v2_timeline_is_not_a_v3_timeline() -> None:
    v2 = json.loads((_SCHEMAS / "timeline-v2.example.json").read_text(encoding="utf-8"))

    with pytest.raises(ValidationError):
        TimelineV3.model_validate(v2)


def test_committed_v3_schema_matches_the_model() -> None:
    committed = json.loads((_SCHEMAS / "timeline-v3.schema.json").read_text(encoding="utf-8"))

    assert committed == timeline_v3_json_schema()
