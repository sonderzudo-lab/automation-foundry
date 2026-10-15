"""Building the v2 timeline from synthetic words, images and animation plans."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from src.pipeline.animation_plan import AnimationPlan, parse_animation_plan
from src.pipeline.timeline import (
    ClosingOverlay,
    HighlightOverlay,
    ListOverlay,
    TimelineV2,
    TimelineWord,
    timeline_to_json,
)
from src.pipeline.timeline_builder import (
    LIST_CARD_TAIL_MS,
    SceneSpan,
    TimelineBuildError,
    TimelineImage,
    build_timeline_v2,
    scene_cuts_sha256,
    scene_spans,
    timeline_images_from_manifest,
)

_STRETCHES = (
    "Você pega o celular sem pensar enquanto guarda a roupa? "
    "Repara nessa sequência: primeiro, a espiada automática. A roupa fica esperando.",
    "Segundo, a distância. Conferir exige interromper a tarefa e sair dali. "
    "Terceiro, a chance de concluir. Você pode guardar aquela camiseta que falta.",
    "Deixar o celular longe não garante terminar a tarefa, mas cria uma pausa para decidir.",
)
_NARRATION = "\n\n".join(_STRETCHES)
_PLAN: dict[str, Any] = {
    "schema_version": 1,
    "steps": {
        "title": "A sequência",
        "items": [
            {"label": "A espiada automática", "anchor": "primeiro, a espiada automática"},
            {"label": "A distância", "anchor": "Segundo, a distância"},
            {"label": "A chance de concluir", "anchor": "Terceiro, a chance de concluir"},
        ],
    },
    "highlights": [{"text": "interromper a tarefa"}],
    "closing_sentence": (
        "Deixar o celular longe não garante terminar a tarefa, mas cria uma pausa para decidir."
    ),
}
_WORD_MS = 300
_SENTENCE_GAP_MS = 700


def _words(narration: str = _NARRATION) -> list[TimelineWord]:
    """Caption words spoken one after another, with a pause after each sentence."""
    words: list[TimelineWord] = []
    cursor = 200
    for text in narration.split():
        words.append(TimelineWord(text=text, start_ms=cursor, end_ms=cursor + _WORD_MS))
        cursor += _WORD_MS + (_SENTENCE_GAP_MS if text[-1] in ".?!" else 0)
    return words


def _images(stretches: tuple[str, ...] = _STRETCHES) -> list[TimelineImage]:
    return [
        TimelineImage(
            file_name=f"visual-{index:03d}.png",
            sha256=f"{index:x}" * 64,
            narration_text=stretch,
        )
        for index, stretch in enumerate(stretches, start=1)
    ]


def _plan(changes: dict[str, Any] | None = None) -> AnimationPlan:
    plan = copy.deepcopy(_PLAN)
    plan.update(changes or {})
    return parse_animation_plan(json.dumps(plan, ensure_ascii=False), narration=_NARRATION)


def _build(
    plan: AnimationPlan | None = None,
    *,
    words: list[TimelineWord] | None = None,
    images: list[TimelineImage] | None = None,
    duration_ms: int | None = None,
) -> tuple[TimelineV2, tuple[str, ...], list[TimelineWord]]:
    words = _words() if words is None else words
    built = build_timeline_v2(
        video_format="short",
        duration_ms=words[-1].end_ms + 500 if duration_ms is None else duration_ms,
        audio_sha256="a" * 64,
        caption_ass_sha256="b" * 64,
        images=_images() if images is None else images,
        words=words,
        plan=_plan() if plan is None else plan,
    )
    return built.timeline, built.dropped_highlights, words


def _index(words: list[TimelineWord], text: str, *, after: int = -1) -> int:
    return next(i for i, word in enumerate(words) if i > after and word.text == text)


def test_each_image_starts_when_its_stretch_starts_being_spoken() -> None:
    timeline, _dropped, words = _build()

    second = len(_STRETCHES[0].split())
    third = second + len(_STRETCHES[1].split())
    assert [(clip.start_ms, clip.end_ms) for clip in timeline.clips] == [
        (0, words[second].start_ms),
        (words[second].start_ms, words[third].start_ms),
        (words[third].start_ms, timeline.duration_ms),
    ]
    assert [clip.image for clip in timeline.clips] == [
        "visual-001.png", "visual-002.png", "visual-003.png",
    ]
    assert [clip.transition_in for clip in timeline.clips] == ["cut", "crossfade", "crossfade"]
    assert timeline.words == words
    assert (timeline.width, timeline.height, timeline.animation_style) == (
        1080, 1920, "explainer_v1",
    )


def test_scene_spans_are_the_cuts_the_timeline_uses() -> None:
    timeline, _dropped, words = _build()

    spans = scene_spans(_images(), words, duration_ms=timeline.duration_ms)

    assert [(span.index, span.start_ms, span.end_ms) for span in spans] == [
        (clip.index, clip.start_ms, clip.end_ms) for clip in timeline.clips
    ]
    assert scene_cuts_sha256(spans) == scene_cuts_sha256(
        scene_spans(_images(), words, duration_ms=timeline.duration_ms)
    )
    moved = [*spans[:-1], SceneSpan(index=3, start_ms=spans[2].start_ms + 1, end_ms=spans[2].end_ms)]
    assert scene_cuts_sha256(moved) != scene_cuts_sha256(spans)


def test_scene_spans_need_words_and_images() -> None:
    with pytest.raises(TimelineBuildError, match="no words"):
        scene_spans(_images(), [], duration_ms=10_000)
    with pytest.raises(TimelineBuildError, match="no images"):
        scene_spans([], _words(), duration_ms=10_000)


def test_list_cards_follow_the_spoken_anchors() -> None:
    timeline, _dropped, words = _build()

    cards = [overlay for overlay in timeline.overlays if isinstance(overlay, ListOverlay)]
    assert [card.active for card in cards] == [0, 1, 2]
    assert cards[0].items == ["A espiada automática", "A distância", "A chance de concluir"]
    assert cards[0].title == "A sequência"
    # The first card opens with the sentence that announces the list.
    assert cards[0].start_ms == words[_index(words, "Repara")].start_ms
    assert cards[0].item_start_ms == words[_index(words, "primeiro,")].start_ms
    assert cards[0].item_end_ms == words[_index(words, "automática.")].end_ms
    assert cards[0].end_ms == cards[0].item_end_ms + LIST_CARD_TAIL_MS
    second = _index(words, "Segundo,")
    assert cards[1].start_ms == cards[1].item_start_ms == words[second].start_ms
    assert cards[1].item_end_ms == words[_index(words, "distância.")].end_ms


def test_the_closing_sentence_ends_with_the_audio_and_highlights_after_the_comma() -> None:
    timeline, _dropped, words = _build()

    closing = timeline.overlays[-1]
    assert isinstance(closing, ClosingOverlay)
    assert closing.word_from == _index(words, "Deixar")
    assert closing.highlight_from == _index(words, "mas")
    assert closing.start_ms == words[closing.word_from].start_ms
    assert closing.end_ms == timeline.duration_ms


def test_a_highlight_shows_the_spoken_words_while_the_screen_is_free() -> None:
    timeline, dropped, words = _build()

    (highlight,) = [o for o in timeline.overlays if isinstance(o, HighlightOverlay)]
    first = _index(words, "interromper")
    assert highlight.text == "Interromper a tarefa"
    assert highlight.start_ms == words[first].start_ms
    # Shown for at least one second, even when the words are said faster.
    assert highlight.end_ms == max(words[first + 2].end_ms, highlight.start_ms + 1_000)
    assert dropped == ()
    assert [o.start_ms for o in timeline.overlays] == sorted(o.start_ms for o in timeline.overlays)


def test_screen_texts_come_from_the_captions_not_from_the_model_spelling() -> None:
    plan = copy.deepcopy(_PLAN)
    plan["steps"]["items"][0] = {
        "label": "a ESPIADA automática",
        "anchor": "PRIMEIRO a espiada automática",
    }

    timeline, _dropped, _words_ = _build(_plan({"steps": plan["steps"]}))

    first = timeline.overlays[0]
    assert isinstance(first, ListOverlay)
    assert first.items[0] == "A espiada automática"


def test_a_highlight_without_free_time_is_left_out_and_reported() -> None:
    timeline, dropped, _words_ = _build(_plan({"highlights": [{"text": "a distância"}]}))

    assert dropped == ("a distância",)
    assert not any(isinstance(o, HighlightOverlay) for o in timeline.overlays)


def test_a_plan_without_animations_keeps_only_the_images() -> None:
    plan = _plan({"steps": None, "highlights": [], "closing_sentence": None})

    timeline, dropped, _words_ = _build(plan)

    assert timeline.overlays == []
    assert dropped == ()
    assert len(timeline.clips) == 3


def test_an_anchor_that_was_not_spoken_fails_closed() -> None:
    words = _words()
    words[_index(words, "Segundo,")] = words[_index(words, "Segundo,")].model_copy(
        update={"text": "Depois,"}
    )

    with pytest.raises(TimelineBuildError, match="anchor of item 2"):
        _build(words=words)


def test_a_list_item_spoken_after_the_closing_sentence_fails_closed() -> None:
    stretches = (
        _STRETCHES[0],
        "Segundo, a distância. Conferir exige interromper a tarefa e sair dali.",
        f"{_STRETCHES[2]} Terceiro, a chance de concluir.",
    )
    narration = "\n\n".join(stretches)
    words = _words(narration)
    plan = parse_animation_plan(json.dumps(_PLAN, ensure_ascii=False), narration=narration)

    with pytest.raises(TimelineBuildError, match="card of item 3 has no room"):
        build_timeline_v2(
            video_format="short",
            duration_ms=words[-1].end_ms + 500,
            audio_sha256="a" * 64,
            caption_ass_sha256="b" * 64,
            images=_images(stretches),
            words=words,
            plan=plan,
        )


def test_caption_word_counts_that_differ_from_the_stretches_are_scaled() -> None:
    words = _words()
    # Two caption words joined into one: the positions are scaled to the captions.
    merged = [*words[:3], words[3].model_copy(update={"end_ms": words[4].end_ms}), *words[5:]]

    timeline, _dropped, _words_ = _build(words=merged)

    second = round(len(_STRETCHES[0].split()) * len(merged) / len(words))
    assert timeline.clips[1].start_ms == merged[second].start_ms


def test_images_that_do_not_fit_one_second_each_fail_closed() -> None:
    with pytest.raises(TimelineBuildError, match="do not fit"):
        _build(duration_ms=_words()[-1].end_ms + 500, images=_images(("Uma palavra.",) * 40))


def test_the_timeline_is_canonical_json_accepted_by_the_contract() -> None:
    timeline, _dropped, _words_ = _build()

    assert TimelineV2.model_validate_json(timeline_to_json(timeline)) == timeline


def test_manifest_images_come_in_order_with_their_stretches() -> None:
    manifest = {
        "assets": [
            {"index": 2, "file_name": "visual-002.png", "sha256": "2" * 64, "narration_text": "B."},
            {"index": 1, "file_name": "visual-001.png", "sha256": "1" * 64, "narration_text": "A."},
        ]
    }

    images = timeline_images_from_manifest(manifest)

    assert [image.file_name for image in images] == ["visual-001.png", "visual-002.png"]
    assert images[0] == TimelineImage("visual-001.png", "1" * 64, "A.")


@pytest.mark.parametrize(
    "assets",
    [
        [],
        [{"index": 1, "file_name": "visual-001.png", "sha256": "1" * 64}],
        [{"index": 2, "file_name": "visual-002.png", "sha256": "2" * 64, "narration_text": "B."}],
    ],
)
def test_a_manifest_without_usable_stretches_fails_closed(assets: list[dict[str, Any]]) -> None:
    with pytest.raises(TimelineBuildError):
        timeline_images_from_manifest({"assets": assets})
