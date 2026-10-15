"""Contract of the v2 timeline: v1 plus animated overlays."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from src.pipeline.timeline import TimelineV2, timeline_to_json, timeline_v2_json_schema

_SCHEMAS = Path(__file__).resolve().parents[2] / "docs" / "reference" / "schemas"
_ITEMS = ["A pausa", "A distância"]


def _timeline() -> dict[str, Any]:
    return {
        "schema_version": 2,
        "format": "short",
        "width": 1080,
        "height": 1920,
        "fps": 30,
        "duration_ms": 12_000,
        "audio_sha256": "a" * 64,
        "caption_ass_sha256": "b" * 64,
        "caption_template": "word_pop",
        "transition_ms": 400,
        "clips": [
            {"index": 1, "image": "visual-001.png", "image_sha256": "c" * 64,
             "start_ms": 0, "end_ms": 6_000, "effect": "zoom_in", "transition_in": "cut"},
            {"index": 2, "image": "visual-002.png", "image_sha256": "d" * 64,
             "start_ms": 6_000, "end_ms": 12_000, "effect": "zoom_out",
             "transition_in": "crossfade"},
        ],
        "words": [
            {"text": "Primeiro,", "start_ms": 500, "end_ms": 1_000},
            {"text": "a", "start_ms": 1_000, "end_ms": 1_100},
            {"text": "pausa.", "start_ms": 1_100, "end_ms": 1_700},
            {"text": "Segundo,", "start_ms": 4_000, "end_ms": 4_500},
            {"text": "a", "start_ms": 4_500, "end_ms": 4_600},
            {"text": "distância.", "start_ms": 4_600, "end_ms": 5_300},
            {"text": "A", "start_ms": 6_300, "end_ms": 6_400},
            {"text": "pausa", "start_ms": 6_400, "end_ms": 6_900},
            {"text": "ajuda.", "start_ms": 6_900, "end_ms": 7_400},
            {"text": "Deixar", "start_ms": 9_000, "end_ms": 9_400},
            {"text": "longe", "start_ms": 9_400, "end_ms": 9_900},
            {"text": "ajuda.", "start_ms": 9_900, "end_ms": 10_500},
        ],
        "animation_style": "explainer_v1",
        "overlays": [
            {"kind": "list", "start_ms": 300, "end_ms": 2_300, "title": "A sequência",
             "items": _ITEMS, "active": 0, "item_start_ms": 500, "item_end_ms": 1_700},
            {"kind": "list", "start_ms": 3_800, "end_ms": 5_900, "title": "A sequência",
             "items": _ITEMS, "active": 1, "item_start_ms": 4_000, "item_end_ms": 5_300},
            {"kind": "highlight", "start_ms": 6_200, "end_ms": 7_600, "text": "A pausa ajuda"},
            {"kind": "closing_sentence", "start_ms": 8_900, "end_ms": 12_000,
             "word_from": 9, "highlight_from": 10},
        ],
    }


def _with_overlay(position: int, **changes: Any) -> dict[str, Any]:
    data = copy.deepcopy(_timeline())
    data["overlays"][position].update(changes)
    return data


def test_a_consistent_v2_timeline_is_accepted() -> None:
    timeline = TimelineV2.model_validate(_timeline())

    assert [overlay.kind for overlay in timeline.overlays] == [
        "list", "list", "highlight", "closing_sentence",
    ]
    assert TimelineV2.model_validate_json(timeline_to_json(timeline)) == timeline


def test_v1_rules_still_apply_to_v2() -> None:
    data = copy.deepcopy(_timeline())
    data["clips"][1]["start_ms"] = 6_100

    with pytest.raises(ValidationError, match="does not start where"):
        TimelineV2.model_validate(data)


@pytest.mark.parametrize(
    ("data", "fragment"),
    [
        (_with_overlay(0, end_ms=900), "shorter than"),
        (_with_overlay(1, start_ms=2_000), "starts before the previous overlay ends"),
        (_with_overlay(0, item_end_ms=2_400), "does not speak its item inside the card"),
        (_with_overlay(1, items=["A pausa", "O caminho"]), "same title and items"),
        (_with_overlay(1, active=0), "one card per item"),
        (_with_overlay(2, text="uma frase de destaque longa demais para caber na tela"), "8 words"),
        (_with_overlay(3, end_ms=11_000), "must be the last overlay"),
        (_with_overlay(3, highlight_from=12), "outside the words"),
        (_with_overlay(3, start_ms=9_200), "starts after its first word"),
        (_with_overlay(2, kind="sticker"), "kind"),
    ],
)
def test_inconsistent_overlays_are_refused(data: dict[str, Any], fragment: str) -> None:
    with pytest.raises(ValidationError, match=fragment):
        TimelineV2.model_validate(data)


def test_a_list_without_its_last_card_is_refused() -> None:
    data = copy.deepcopy(_timeline())
    del data["overlays"][1]

    with pytest.raises(ValidationError, match="one card per item"):
        TimelineV2.model_validate(data)


def test_committed_v2_schema_matches_the_model() -> None:
    committed = json.loads((_SCHEMAS / "timeline-v2.schema.json").read_text(encoding="utf-8"))

    assert committed == timeline_v2_json_schema()


def test_committed_v2_example_is_valid() -> None:
    TimelineV2.model_validate_json(
        (_SCHEMAS / "timeline-v2.example.json").read_text(encoding="utf-8")
    )
