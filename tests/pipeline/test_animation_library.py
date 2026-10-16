"""Contract of animations v3: the moment library, the art direction and timeline v4."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, get_args

import pytest
from pydantic import ValidationError

from src.pipeline.animation_library import (
    MOMENT_VARIANTS,
    ArtDirection,
    ArtDirectionLocks,
    ClosingOverlayV4,
    ComparisonOverlay,
    HighlightOverlayV4,
    KeywordsOverlay,
    ListOverlayV4,
    NumberOverlay,
    QuestionOverlay,
    QuoteOverlay,
    TimelineV4,
    TitleCardOverlay,
    locks_problem,
    moment_signature,
    timeline_v4_json_schema,
    variety_problem,
)
from src.pipeline.timeline import timeline_to_json

_SCHEMAS = Path(__file__).resolve().parents[2] / "docs" / "reference" / "schemas"

_ART: dict[str, str] = {
    "palette": "night",
    "typography": "rounded_bold",
    "caption_style": "word_pop",
    "caption_position": "lower",
    "motion": "lively",
    "transition": "slide",
    "corners": "rounded",
}

# A 1.2 s slot between the second list card and the closing sentence.
_SLOT = {"start_ms": 6_250, "end_ms": 7_450}


def _list_card(active: int, start: int, end: int, item_start: int, item_end: int) -> dict[str, Any]:
    return {
        "kind": "list",
        "variant": "checklist",
        "start_ms": start,
        "end_ms": end,
        "title": "A sequência",
        "items": ["A pausa", "A distância"],
        "active": active,
        "item_start_ms": item_start,
        "item_end_ms": item_end,
    }


def _timeline() -> dict[str, Any]:
    """The v3 example redrawn with the library; overlays cover 7.1 s of 12 s."""
    data = json.loads((_SCHEMAS / "timeline-v3.example.json").read_text(encoding="utf-8"))
    del data["caption_template"]
    data["schema_version"] = 4
    data["animation_style"] = "library_v1"
    data["art_direction"] = dict(_ART)
    data["overlays"] = [
        _list_card(0, 400, 1_750, 500, 1_700),
        _list_card(1, 3_900, 5_350, 4_000, 5_300),
        {"kind": "highlight", "variant": "marker", **_SLOT, "text": "A pausa ajuda"},
        {
            "kind": "closing_sentence",
            "variant": "typewriter",
            "start_ms": 8_900,
            "end_ms": 12_000,
            "word_from": 9,
            "highlight_from": 10,
        },
    ]
    return data


def _with_overlay(position: int, overlay: dict[str, Any]) -> dict[str, Any]:
    data = _timeline()
    data["overlays"][position] = overlay
    return data


def _changed_overlay(position: int, **changes: Any) -> dict[str, Any]:
    data = _timeline()
    data["overlays"][position].update(changes)
    return data


def test_a_consistent_timeline_is_accepted() -> None:
    timeline = TimelineV4.model_validate(_timeline())

    assert timeline.art_direction.palette == "night"
    assert [overlay.kind for overlay in timeline.overlays] == [
        "list",
        "list",
        "highlight",
        "closing_sentence",
    ]
    assert TimelineV4.model_validate_json(timeline_to_json(timeline)) == timeline  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "overlay",
    [
        {"kind": "question", "variant": "bubble", **_SLOT, "text": "Por que a pausa ajuda?"},
        {
            "kind": "keywords",
            "variant": "chips",
            **_SLOT,
            "items": [
                {"text": "pausa", "appear_ms": 6_300},
                {"text": "distância", "appear_ms": 6_900},
            ],
        },
        {"kind": "number", "variant": "count_up", **_SLOT, "value_text": "dois", "label": "passos"},
        {"kind": "quote", "variant": "minimal", **_SLOT, "text": "A pausa ajuda."},
        {
            "kind": "comparison",
            "variant": "myth_fact",
            **_SLOT,
            "left_header": "Mito",
            "left_text": "Pressa",
            "right_header": "Fato",
            "right_text": "Pausa",
            "left_start_ms": 6_300,
            "right_start_ms": 6_900,
        },
    ],
    ids=lambda overlay: overlay["kind"],
)
def test_every_moment_of_the_catalogue_can_be_drawn(overlay: dict[str, Any]) -> None:
    timeline = TimelineV4.model_validate(_with_overlay(2, overlay))

    assert timeline.overlays[2].kind == overlay["kind"]


def test_a_title_card_opens_the_video() -> None:
    data = _timeline()
    data["overlays"][0:2] = [
        {"kind": "title_card", "variant": "slam", "start_ms": 0, "end_ms": 1_750, "text": "A pausa"},
    ]

    TimelineV4.model_validate(data)


def test_the_catalogue_lists_every_variant_the_overlays_accept() -> None:
    overlays = {
        "title_card": TitleCardOverlay,
        "list": ListOverlayV4,
        "comparison": ComparisonOverlay,
        "question": QuestionOverlay,
        "keywords": KeywordsOverlay,
        "number": NumberOverlay,
        "highlight": HighlightOverlayV4,
        "quote": QuoteOverlay,
        "closing_sentence": ClosingOverlayV4,
    }

    assert set(overlays) == set(MOMENT_VARIANTS)
    for kind, model in overlays.items():
        assert get_args(model.model_fields["variant"].annotation) == MOMENT_VARIANTS[kind]


@pytest.mark.parametrize(
    ("data", "fragment"),
    [
        (_changed_overlay(2, end_ms=6_900), "shorter than"),
        (_changed_overlay(1, end_ms=6_400), "starts before the previous overlay ends"),
        (_changed_overlay(1, item_end_ms=5_400), "outside itself"),
        (_changed_overlay(1, item_start_ms=5_300, item_end_ms=5_000), "out of order"),
        (_changed_overlay(2, text="A pausa ajuda quem para e respira antes de agir"), "8 words"),
        (_changed_overlay(1, variant="path"), "same title, items and variant"),
        (_changed_overlay(1, active=0), "one card per item"),
        (_changed_overlay(3, start_ms=9_100), "after its first word is spoken"),
        (_changed_overlay(3, highlight_from=12), "outside the words"),
        (_changed_overlay(3, end_ms=11_900), "end with the audio"),
        (_changed_overlay(2, variant="spiral"), "variant"),
        (
            _with_overlay(2, {"kind": "question", "variant": "bubble", **_SLOT, "text": "A pausa"}),
            "question mark",
        ),
        (
            _with_overlay(
                2, {"kind": "title_card", "variant": "slide", **_SLOT, "text": "A pausa"}
            ),
            "title card must be the first",
        ),
        (
            _with_overlay(
                2,
                {
                    "kind": "comparison",
                    "variant": "split",
                    **_SLOT,
                    "left_header": "Antes",
                    "left_text": "Pressa",
                    "right_header": "Depois",
                    "right_text": "Pausa",
                    "left_start_ms": 7_000,
                    "right_start_ms": 6_400,
                },
            ),
            "out of order",
        ),
        (
            _with_overlay(
                2,
                {
                    "kind": "keywords",
                    "variant": "stack",
                    **_SLOT,
                    "items": [
                        {"text": "pausa", "appear_ms": 6_300},
                        {"text": "distância", "appear_ms": 7_500},
                    ],
                },
            ),
            "outside itself",
        ),
    ],
)
def test_inconsistent_overlays_are_refused(data: dict[str, Any], fragment: str) -> None:
    with pytest.raises(ValidationError, match=fragment):
        TimelineV4.model_validate(data)


def test_overlays_may_not_hide_most_of_the_images() -> None:
    data = _changed_overlay(2, start_ms=5_400, end_ms=7_500)

    with pytest.raises(ValidationError, match="more than 60%"):
        TimelineV4.model_validate(data)


def test_v3_rules_still_apply_to_v4() -> None:
    data = _timeline()
    data["clips"][0]["effect"] = "zoom_in"

    with pytest.raises(ValidationError, match="effect none"):
        TimelineV4.model_validate(data)
    data = _timeline()
    data["width"] = 1920
    with pytest.raises(ValidationError, match="dimensions"):
        TimelineV4.model_validate(data)


def test_a_v3_timeline_is_not_a_v4_timeline() -> None:
    v3 = json.loads((_SCHEMAS / "timeline-v3.example.json").read_text(encoding="utf-8"))

    with pytest.raises(ValidationError):
        TimelineV4.model_validate(v3)


def test_the_json_schema_carries_the_art_direction() -> None:
    schema = timeline_v4_json_schema()

    assert "art_direction" in schema["required"]
    assert schema["properties"]["schema_version"]["const"] == 4


def test_an_art_direction_outside_the_closed_lists_is_refused() -> None:
    with pytest.raises(ValidationError):
        ArtDirection.model_validate({**_ART, "palette": "neon"})


def test_locks_keep_the_identity_of_a_channel() -> None:
    art = ArtDirection.model_validate(_ART)

    assert locks_problem(art, ArtDirectionLocks()) is None
    assert locks_problem(art, ArtDirectionLocks(palette="night", corners="rounded")) is None
    assert locks_problem(art, ArtDirectionLocks(typography="serif_editorial")) == (
        "uses typography 'rounded_bold' but the channel fixes 'serif_editorial'"
    )


_MOMENTS = [("list", "checklist"), ("highlight", "marker"), ("closing_sentence", "typewriter")]


def test_the_look_of_the_previous_video_is_not_repeated() -> None:
    art = ArtDirection.model_validate(_ART)
    same_pair = ArtDirection.model_validate({**_ART, "motion": "calm", "corners": "square"})
    other_font = ArtDirection.model_validate({**_ART, "typography": "clean_sans"})

    assert variety_problem(art, _MOMENTS, previous_art=[same_pair], previous_moments=[]) == (
        "repeats the palette and typography of the previous video"
    )
    assert variety_problem(art, _MOMENTS, previous_art=[other_font], previous_moments=[]) is None
    # Only the last video counts.
    assert (
        variety_problem(art, _MOMENTS, previous_art=[other_font, same_pair], previous_moments=[])
        is None
    )


def test_a_channel_that_locks_palette_and_typography_may_repeat_them() -> None:
    art = ArtDirection.model_validate(_ART)
    both = ArtDirectionLocks(palette="night", typography="rounded_bold")
    one = ArtDirectionLocks(palette="night")

    assert variety_problem(art, _MOMENTS, previous_art=[art], previous_moments=[], locks=both) is None
    assert variety_problem(art, _MOMENTS, previous_art=[art], previous_moments=[], locks=one) == (
        "repeats the palette and typography of the previous video"
    )


def test_the_sequence_of_moments_differs_from_the_last_three_videos() -> None:
    art = ArtDirection.model_validate(_ART)
    other = [("question", "bubble"), ("closing_sentence", "typewriter")]
    swapped_variant = [("list", "path"), *_MOMENTS[1:]]

    assert moment_signature(_MOMENTS) == (
        "list:checklist",
        "highlight:marker",
        "closing_sentence:typewriter",
    )
    assert variety_problem(
        art, _MOMENTS, previous_art=[], previous_moments=[other, other, copy.copy(_MOMENTS)]
    ) == "repeats the sequence of moments of a recent video"
    assert variety_problem(
        art, _MOMENTS, previous_art=[], previous_moments=[other, other, other, _MOMENTS]
    ) is None
    assert variety_problem(
        art, _MOMENTS, previous_art=[], previous_moments=[swapped_variant]
    ) is None


def test_a_single_moment_is_never_a_repeated_sequence() -> None:
    art = ArtDirection.model_validate(_ART)
    closing = [("closing_sentence", "typewriter")]

    assert variety_problem(art, closing, previous_art=[], previous_moments=[closing]) is None


def test_committed_v4_schema_matches_the_model() -> None:
    committed = json.loads((_SCHEMAS / "timeline-v4.schema.json").read_text(encoding="utf-8"))

    assert committed == timeline_v4_json_schema()


def test_the_committed_v4_example_is_a_valid_timeline() -> None:
    example = json.loads((_SCHEMAS / "timeline-v4.example.json").read_text(encoding="utf-8"))

    timeline = TimelineV4.model_validate(example)

    assert {overlay.kind for overlay in timeline.overlays} == {
        "title_card",
        "list",
        "comparison",
        "number",
        "closing_sentence",
    }
