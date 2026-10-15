"""Animations v3: a library of animated moments and an art direction per video.

``explainer_v1`` drew every video the same way. Here the text model composes each
video from a catalogue of moments (lists, comparisons, questions, keywords,
numbers, quotes, highlights, an opening title and the closing sentence), each with
a few visual variants, and chooses an art direction (palette, typography,
captions, motion and transitions) from closed lists. The render code owns how
every name looks, so the result stays testable while two videos rarely look alike.

The rules of the earlier contracts still hold: every on-screen word is copied
from the approved narration (only a list title and comparison headers are short
free text without digits), and no timing comes from the model. The code will
find each moment in the A4 words and build a ``TimelineV4``: timeline v3 (images
or generated videos) plus the art direction and the v4 overlays. This module
holds the contract only; the plan, the builder and the render are not written yet.

A channel may lock part of the art direction so its videos keep an identity; the
rest varies. ``variety_problem`` refuses a plan that repeats the look or the
sequence of moments of the channel's recent videos.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.pipeline.timeline import (
    MAX_LIST_ITEMS,
    MAX_TIMELINE_CLIPS,
    MAX_TIMELINE_MS,
    MAX_TIMELINE_WORDS,
    MAX_TRANSITION_MS,
    MIN_CLIP_MS,
    TimelineClipV3,
    TimelineWord,
    VideoFormat,
    clip_video_problem,
    timeline_problem,
)

_SHA256 = r"^[0-9a-f]{64}$"

# ── Art direction ────────────────────────────────────────────────────────────

Palette = Literal["sunny", "night", "mint", "coral", "ink", "candy", "sand"]
Typography = Literal["rounded_bold", "condensed_impact", "clean_sans", "serif_editorial"]
CaptionStyle = Literal["word_pop", "phrase_blocks", "karaoke_highlight"]
CaptionPosition = Literal["lower", "middle"]
Motion = Literal["calm", "lively", "punchy"]
TransitionStyle = Literal["crossfade", "slide", "wipe"]
Corners = Literal["rounded", "square"]

ART_DIRECTION_FIELDS = (
    "palette",
    "typography",
    "caption_style",
    "caption_position",
    "motion",
    "transition",
    "corners",
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ArtDirection(_Strict):
    """How one video looks; every value is a name the render code knows."""

    palette: Palette
    typography: Typography
    caption_style: CaptionStyle
    caption_position: CaptionPosition
    motion: Motion
    transition: TransitionStyle
    corners: Corners


class ArtDirectionLocks(_Strict):
    """Values a channel fixes for all its videos; None leaves the choice to the plan."""

    palette: Palette | None = None
    typography: Typography | None = None
    caption_style: CaptionStyle | None = None
    caption_position: CaptionPosition | None = None
    motion: Motion | None = None
    transition: TransitionStyle | None = None
    corners: Corners | None = None


def locks_problem(art: ArtDirection, locks: ArtDirectionLocks) -> str | None:
    """Return why an art direction breaks the channel's locks, or None."""
    for name in ART_DIRECTION_FIELDS:
        locked = getattr(locks, name)
        if locked is not None and getattr(art, name) != locked:
            return f"uses {name} '{getattr(art, name)}' but the channel fixes '{locked}'"
    return None


# ── Catalogue of moments ─────────────────────────────────────────────────────

TitleVariant = Literal["slam", "slide"]
ListVariant = Literal["checklist", "cards_stack", "path"]
ComparisonVariant = Literal["split", "versus", "myth_fact"]
QuestionVariant = Literal["bubble", "full_screen"]
KeywordsVariant = Literal["chips", "stack"]
NumberVariant = Literal["count_up", "badge"]
HighlightVariant = Literal["big_center", "lower_third", "marker"]
QuoteVariant = Literal["card", "minimal"]
ClosingVariant = Literal["word_by_word", "typewriter"]

MOMENT_VARIANTS: dict[str, tuple[str, ...]] = {
    "title_card": ("slam", "slide"),
    "list": ("checklist", "cards_stack", "path"),
    "comparison": ("split", "versus", "myth_fact"),
    "question": ("bubble", "full_screen"),
    "keywords": ("chips", "stack"),
    "number": ("count_up", "badge"),
    "highlight": ("big_center", "lower_third", "marker"),
    "quote": ("card", "minimal"),
    "closing_sentence": ("word_by_word", "typewriter"),
}
MAX_KEYWORDS = 4
# Share of the video that may be covered by overlays; the images must be seen.
MAX_OVERLAY_SHARE = 0.6

_START = Field(ge=0, le=MAX_TIMELINE_MS)
_END = Field(ge=1, le=MAX_TIMELINE_MS)


class TitleCardOverlay(_Strict):
    """An opening phrase of the narration, shown as the video starts."""

    kind: Literal["title_card"]
    variant: TitleVariant
    start_ms: int = _START
    end_ms: int = _END
    text: str = Field(min_length=1, max_length=60)


class ListOverlayV4(_Strict):
    """One card per item, as in v2; ``variant`` picks checklist, stacked cards or a path."""

    kind: Literal["list"]
    variant: ListVariant
    start_ms: int = _START
    end_ms: int = _END
    title: str = Field(min_length=1, max_length=40)
    items: list[str] = Field(min_length=2, max_length=MAX_LIST_ITEMS)
    active: int = Field(ge=0, lt=MAX_LIST_ITEMS)
    item_start_ms: int = _START
    item_end_ms: int = _START


class ComparisonOverlay(_Strict):
    """Two sides shown as the narration reaches each one (myth and fact, before and after)."""

    kind: Literal["comparison"]
    variant: ComparisonVariant
    start_ms: int = _START
    end_ms: int = _END
    left_header: str = Field(min_length=1, max_length=24)
    left_text: str = Field(min_length=1, max_length=60)
    right_header: str = Field(min_length=1, max_length=24)
    right_text: str = Field(min_length=1, max_length=60)
    left_start_ms: int = _START
    right_start_ms: int = _START


class QuestionOverlay(_Strict):
    kind: Literal["question"]
    variant: QuestionVariant
    start_ms: int = _START
    end_ms: int = _END
    text: str = Field(min_length=2, max_length=120)


class KeywordItem(_Strict):
    text: str = Field(min_length=1, max_length=40)
    appear_ms: int = _START


class KeywordsOverlay(_Strict):
    kind: Literal["keywords"]
    variant: KeywordsVariant
    start_ms: int = _START
    end_ms: int = _END
    items: list[KeywordItem] = Field(min_length=2, max_length=MAX_KEYWORDS)


class NumberOverlay(_Strict):
    """A number the narration says (digits or words), with a short label of its own words."""

    kind: Literal["number"]
    variant: NumberVariant
    start_ms: int = _START
    end_ms: int = _END
    value_text: str = Field(min_length=1, max_length=30)
    label: str = Field(min_length=1, max_length=40)


class HighlightOverlayV4(_Strict):
    kind: Literal["highlight"]
    variant: HighlightVariant
    start_ms: int = _START
    end_ms: int = _END
    text: str = Field(min_length=1, max_length=80)


class QuoteOverlay(_Strict):
    kind: Literal["quote"]
    variant: QuoteVariant
    start_ms: int = _START
    end_ms: int = _END
    text: str = Field(min_length=1, max_length=160)


class ClosingOverlayV4(_Strict):
    kind: Literal["closing_sentence"]
    variant: ClosingVariant
    start_ms: int = _START
    end_ms: int = _END
    word_from: int = Field(ge=0, lt=MAX_TIMELINE_WORDS)
    highlight_from: int = Field(ge=0, lt=MAX_TIMELINE_WORDS)


OverlayV4 = Annotated[
    TitleCardOverlay
    | ListOverlayV4
    | ComparisonOverlay
    | QuestionOverlay
    | KeywordsOverlay
    | NumberOverlay
    | HighlightOverlayV4
    | QuoteOverlay
    | ClosingOverlayV4,
    Field(discriminator="kind"),
]

AnimationStyleV4 = Literal["library_v1"]


class TimelineV4(_Strict):
    """Timeline v3 drawn with the moment library and one art direction.

    Captions follow ``art_direction.caption_style``; clips, words, generated
    videos and every v3 rule are unchanged.
    """

    schema_version: Literal[4]
    format: VideoFormat
    width: int
    height: int
    fps: Literal[30]
    duration_ms: int = Field(ge=MIN_CLIP_MS, le=MAX_TIMELINE_MS)
    audio_sha256: str = Field(pattern=_SHA256)
    caption_ass_sha256: str = Field(pattern=_SHA256)
    transition_ms: int = Field(ge=0, le=MAX_TRANSITION_MS)
    clips: list[TimelineClipV3] = Field(min_length=1, max_length=MAX_TIMELINE_CLIPS)
    words: list[TimelineWord] = Field(max_length=MAX_TIMELINE_WORDS)
    animation_style: AnimationStyleV4
    art_direction: ArtDirection
    overlays: list[OverlayV4] = Field(max_length=40)

    @model_validator(mode="after")
    def _check_timeline(self) -> TimelineV4:
        problem = (
            timeline_problem(self)  # type: ignore[arg-type]
            or clip_video_problem(self)  # type: ignore[arg-type]
            or overlay_v4_problem(self)
        )
        if problem is not None:
            raise ValueError(problem)
        return self


def overlay_v4_problem(timeline: TimelineV4) -> str | None:
    """Return why the v4 overlays cannot be drawn, or None when they are consistent."""
    previous_end = 0
    covered = 0
    for position, overlay in enumerate(timeline.overlays, start=1):
        if overlay.end_ms - overlay.start_ms < MIN_CLIP_MS:
            return f"overlay {position} is shorter than {MIN_CLIP_MS} ms"
        if overlay.end_ms > timeline.duration_ms:
            return f"overlay {position} ends after the audio"
        if overlay.start_ms < previous_end:
            return f"overlay {position} starts before the previous overlay ends"
        previous_end = overlay.end_ms
        covered += overlay.end_ms - overlay.start_ms
        inner = _inner_times(overlay)
        if any(not overlay.start_ms <= value <= overlay.end_ms for value in inner):
            return f"overlay {position} times something outside itself"
        if inner != sorted(inner):
            return f"overlay {position} shows its parts out of order"
        if isinstance(overlay, HighlightOverlayV4) and len(overlay.text.split()) > 8:
            return f"highlight {position} has more than 8 words"
        if isinstance(overlay, QuestionOverlay) and not overlay.text.rstrip().endswith("?"):
            return f"question {position} does not end with a question mark"
        if isinstance(overlay, TitleCardOverlay) and position != 1:
            return "the title card must be the first overlay"
        if isinstance(overlay, ClosingOverlayV4):
            if position != len(timeline.overlays) or overlay.end_ms != timeline.duration_ms:
                return "the closing sentence must be the last overlay and end with the audio"
            if not overlay.word_from <= overlay.highlight_from < len(timeline.words):
                return "the closing sentence points outside the words"
            if timeline.words[overlay.word_from].start_ms < overlay.start_ms:
                return "the closing sentence starts after its first word is spoken"
    if covered > timeline.duration_ms * MAX_OVERLAY_SHARE:
        return "overlays cover more than 60% of the video"
    lists = [overlay for overlay in timeline.overlays if isinstance(overlay, ListOverlayV4)]
    if lists:
        first = lists[0]
        if any(
            card.items != first.items or card.title != first.title or card.variant != first.variant
            for card in lists
        ):
            return "every list card must show the same title, items and variant"
        if [card.active for card in lists] != list(range(len(first.items))):
            return "a list needs one card per item, in order"
    return None


def _inner_times(overlay: Any) -> list[int]:
    if isinstance(overlay, ListOverlayV4):
        return [overlay.item_start_ms, overlay.item_end_ms]
    if isinstance(overlay, ComparisonOverlay):
        return [overlay.left_start_ms, overlay.right_start_ms]
    if isinstance(overlay, KeywordsOverlay):
        return [item.appear_ms for item in overlay.items]
    return []


def timeline_v4_json_schema() -> dict[str, Any]:
    """JSON Schema of the v4 timeline shared with the JavaScript render engine."""
    return TimelineV4.model_json_schema()


# ── Variety across videos ────────────────────────────────────────────────────


def moment_signature(kinds_and_variants: Sequence[tuple[str, str]]) -> tuple[str, ...]:
    """The sequence of moments of a video, compared across videos for repetition."""
    return tuple(f"{kind}:{variant}" for kind, variant in kinds_and_variants)


def variety_problem(
    art: ArtDirection,
    moments: Sequence[tuple[str, str]],
    *,
    previous_art: Sequence[ArtDirection],
    previous_moments: Sequence[Sequence[tuple[str, str]]],
    locks: ArtDirectionLocks | None = None,
) -> str | None:
    """Refuse a plan that looks like the channel's last videos.

    ``previous_*`` are the most recent videos first. The palette and typography
    pair must differ from the last video unless the channel locks both, and the
    sequence of moments must differ from each of the last three videos.
    """
    if previous_art:
        last = previous_art[0]
        both_locked = locks is not None and locks.palette is not None and (
            locks.typography is not None
        )
        if not both_locked and (art.palette, art.typography) == (last.palette, last.typography):
            return "repeats the palette and typography of the previous video"
    signature = moment_signature(moments)
    for earlier in previous_moments[:3]:
        if len(signature) > 1 and signature == moment_signature(earlier):
            return "repeats the sequence of moments of a recent video"
    return None
