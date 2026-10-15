"""Contract for the edit of one video (A5), independent of the render engine.

A timeline lists, in milliseconds, which image is on screen when, with which
movement and transition, and every spoken word for the captions. The code builds
it from verified evidence: the A3 manifest (images and their SHA-256), the A4
captions and the scene plan. The text model never writes timings.

Any render engine (the current FFmpeg edit, or Remotion) receives the same
timeline. The burned-in captions are drawn from ``words``, which come from the
verified A4 ASS file whose SHA-256 the timeline carries, so the alignment gate
and the final review keep checking the same evidence.

Times are whole milliseconds so Python and the JavaScript renderer read exactly
the same values without decimal or floating point conversions.

Version 2 (``TimelineV2``) adds animated overlays drawn by a versioned template
style: list cards, highlighted phrases and the closing sentence. Their texts
come from the approved narration through the animation plan (see
``animation_plan``) and their timings from the words.
"""

from __future__ import annotations

import json
from decimal import ROUND_HALF_EVEN, Decimal
from fractions import Fraction
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

TIMELINE_SCHEMA_VERSION = 1
TIMELINE_FPS = 30
MIN_CLIP_MS = 1_000
MAX_TRANSITION_MS = 1_000
MAX_TIMELINE_MS = 4 * 60 * 60 * 1_000
MAX_TIMELINE_CLIPS = 600
MAX_TIMELINE_WORDS = 60_000

VideoFormat = Literal["short", "long"]
ClipEffect = Literal["none", "zoom_in", "zoom_out", "pan_left", "pan_right"]
ClipTransition = Literal["cut", "crossfade"]
# phrase_blocks: the current A4 look, one short phrase at a time.
# word_pop: one large word at a time, popping in as it is spoken.
CaptionTemplate = Literal["phrase_blocks", "word_pop"]
CAPTION_TEMPLATES: tuple[CaptionTemplate, ...] = ("phrase_blocks", "word_pop")
# Set of animated templates a v2 timeline is drawn with; a new look is a new version.
AnimationStyle = Literal["explainer_v1"]
MAX_OVERLAYS = 20
MAX_LIST_ITEMS = 6
# A generated video may be played at most this much slower to fill its clip.
MAX_VIDEO_STRETCH = Fraction(5, 4)

FORMAT_DIMENSIONS: dict[VideoFormat, tuple[int, int]] = {
    "short": (1080, 1920),
    "long": (1920, 1080),
}

_SHA256 = r"^[0-9a-f]{64}$"
_IMAGE_NAME = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.png$"
_VIDEO_NAME = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.mp4$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TimelineClip(_Strict):
    """One image on screen from ``start_ms`` to ``end_ms``."""

    index: int = Field(ge=1, le=MAX_TIMELINE_CLIPS)
    image: str = Field(pattern=_IMAGE_NAME)
    image_sha256: str = Field(pattern=_SHA256)
    start_ms: int = Field(ge=0, le=MAX_TIMELINE_MS)
    end_ms: int = Field(ge=1, le=MAX_TIMELINE_MS)
    effect: ClipEffect
    # How this clip replaces the previous one; the first clip always starts with a cut.
    transition_in: ClipTransition


class TimelineWord(_Strict):
    """One spoken word and the interval in which it is heard."""

    text: str = Field(min_length=1, max_length=64)
    start_ms: int = Field(ge=0, le=MAX_TIMELINE_MS)
    end_ms: int = Field(ge=0, le=MAX_TIMELINE_MS)


class Timeline(_Strict):
    """Everything a render engine needs to draw one video."""

    schema_version: Literal[1]
    format: VideoFormat
    width: int
    height: int
    fps: Literal[30]
    duration_ms: int = Field(ge=MIN_CLIP_MS, le=MAX_TIMELINE_MS)
    audio_sha256: str = Field(pattern=_SHA256)
    caption_ass_sha256: str = Field(pattern=_SHA256)
    caption_template: CaptionTemplate
    # Length of every crossfade, centred on the cut.
    transition_ms: int = Field(ge=0, le=MAX_TRANSITION_MS)
    clips: list[TimelineClip] = Field(min_length=1, max_length=MAX_TIMELINE_CLIPS)
    words: list[TimelineWord] = Field(max_length=MAX_TIMELINE_WORDS)

    @model_validator(mode="after")
    def _check_timeline(self) -> Timeline:
        problem = timeline_problem(self)
        if problem is not None:
            raise ValueError(problem)
        return self


class ListOverlay(_Strict):
    """One card of an animated list: the items so far, with ``active`` entering.

    A list is shown as one card per item, in order. In the ``explainer_v1``
    style the number of the active item leaves the card when it closes, waits
    in a corner with its label while the item is explained and flies back to
    its row, ticked, when the next card opens.
    """

    kind: Literal["list"]
    start_ms: int = Field(ge=0, le=MAX_TIMELINE_MS)
    end_ms: int = Field(ge=1, le=MAX_TIMELINE_MS)
    title: str = Field(min_length=1, max_length=40)
    items: list[str] = Field(min_length=2, max_length=MAX_LIST_ITEMS)
    active: int = Field(ge=0, lt=MAX_LIST_ITEMS)
    # When the active item is spoken: it enters at the start and is complete at the end.
    item_start_ms: int = Field(ge=0, le=MAX_TIMELINE_MS)
    item_end_ms: int = Field(ge=0, le=MAX_TIMELINE_MS)


class HighlightOverlay(_Strict):
    """A short phrase of the narration shown large while it is spoken."""

    kind: Literal["highlight"]
    start_ms: int = Field(ge=0, le=MAX_TIMELINE_MS)
    end_ms: int = Field(ge=1, le=MAX_TIMELINE_MS)
    text: str = Field(min_length=1, max_length=80)


class ClosingOverlay(_Strict):
    """The closing sentence, word by word as spoken, from ``word_from`` to the end."""

    kind: Literal["closing_sentence"]
    start_ms: int = Field(ge=0, le=MAX_TIMELINE_MS)
    end_ms: int = Field(ge=1, le=MAX_TIMELINE_MS)
    word_from: int = Field(ge=0, lt=MAX_TIMELINE_WORDS)
    # Words from here to the end are drawn in the accent colour.
    highlight_from: int = Field(ge=0, lt=MAX_TIMELINE_WORDS)


Overlay = Annotated[
    ListOverlay | HighlightOverlay | ClosingOverlay, Field(discriminator="kind")
]


class TimelineV2(_Strict):
    """Timeline v1 plus animated overlays drawn over the images by a template style.

    Clips, words and every v1 rule are unchanged. ``overlays`` never carry
    timings written by a text model: the code derives them from the A4 words.
    """

    schema_version: Literal[2]
    format: VideoFormat
    width: int
    height: int
    fps: Literal[30]
    duration_ms: int = Field(ge=MIN_CLIP_MS, le=MAX_TIMELINE_MS)
    audio_sha256: str = Field(pattern=_SHA256)
    caption_ass_sha256: str = Field(pattern=_SHA256)
    caption_template: CaptionTemplate
    transition_ms: int = Field(ge=0, le=MAX_TRANSITION_MS)
    clips: list[TimelineClip] = Field(min_length=1, max_length=MAX_TIMELINE_CLIPS)
    words: list[TimelineWord] = Field(max_length=MAX_TIMELINE_WORDS)
    animation_style: AnimationStyle
    overlays: list[Overlay] = Field(max_length=MAX_OVERLAYS)

    @model_validator(mode="after")
    def _check_timeline(self) -> TimelineV2:
        problem = timeline_problem(self) or overlay_problem(self)
        if problem is not None:
            raise ValueError(problem)
        return self


def overlay_problem(timeline: TimelineV2 | TimelineV3) -> str | None:
    """Return why the overlays of a v2 or v3 timeline cannot be drawn, or None."""
    previous_end = 0
    for position, overlay in enumerate(timeline.overlays, start=1):
        if overlay.end_ms - overlay.start_ms < MIN_CLIP_MS:
            return f"overlay {position} is shorter than {MIN_CLIP_MS} ms"
        if overlay.end_ms > timeline.duration_ms:
            return f"overlay {position} ends after the audio"
        if overlay.start_ms < previous_end:
            return f"overlay {position} starts before the previous overlay ends"
        previous_end = overlay.end_ms
        if isinstance(overlay, HighlightOverlay) and len(overlay.text.split()) > 8:
            return f"highlight {position} has more than 8 words"
        if isinstance(overlay, ListOverlay) and not (
            overlay.start_ms <= overlay.item_start_ms <= overlay.item_end_ms <= overlay.end_ms
        ):
            return f"list card {position} does not speak its item inside the card"
        if isinstance(overlay, ClosingOverlay):
            if position != len(timeline.overlays) or overlay.end_ms != timeline.duration_ms:
                return "the closing sentence must be the last overlay and end with the audio"
            if not overlay.word_from <= overlay.highlight_from < len(timeline.words):
                return "the closing sentence points outside the words"
            if timeline.words[overlay.word_from].start_ms < overlay.start_ms:
                return "the closing sentence starts after its first word is spoken"
    lists = [overlay for overlay in timeline.overlays if isinstance(overlay, ListOverlay)]
    if lists:
        first = lists[0]
        if any(card.items != first.items or card.title != first.title for card in lists):
            return "every list card must show the same title and items"
        if [card.active for card in lists] != list(range(len(first.items))):
            return "a list needs one card per item, in order"
    return None


def timeline_v2_json_schema() -> dict[str, Any]:
    """JSON Schema of the v2 timeline shared with the JavaScript render engine."""
    return TimelineV2.model_json_schema()


class TimelineClipV3(TimelineClip):
    """A v2 clip that may play a video generated from its image (step A5a).

    ``video``, ``video_sha256`` and ``video_ms`` are all set or all null. With a
    video the clip has no zoom (``effect`` is ``none``). The video covers the
    clip's window (``clip_window``: the clip and half of each crossfade around
    it): it starts at the window start from its first frame and plays at
    ``min(1, video_ms / window length)`` speed; a longer video is cut at the end
    of the window. A video may be at most ``MAX_VIDEO_STRETCH`` times slower.
    """

    video: str | None = Field(pattern=_VIDEO_NAME)
    video_sha256: str | None = Field(pattern=_SHA256)
    video_ms: int | None = Field(ge=1, le=MAX_TIMELINE_MS)


class TimelineV3(_Strict):
    """Timeline v2 whose clips may play generated videos instead of zoomed images.

    Overlays, words and every v2 rule are unchanged.
    """

    schema_version: Literal[3]
    format: VideoFormat
    width: int
    height: int
    fps: Literal[30]
    duration_ms: int = Field(ge=MIN_CLIP_MS, le=MAX_TIMELINE_MS)
    audio_sha256: str = Field(pattern=_SHA256)
    caption_ass_sha256: str = Field(pattern=_SHA256)
    caption_template: CaptionTemplate
    transition_ms: int = Field(ge=0, le=MAX_TRANSITION_MS)
    clips: list[TimelineClipV3] = Field(min_length=1, max_length=MAX_TIMELINE_CLIPS)
    words: list[TimelineWord] = Field(max_length=MAX_TIMELINE_WORDS)
    animation_style: AnimationStyle
    overlays: list[Overlay] = Field(max_length=MAX_OVERLAYS)

    @model_validator(mode="after")
    def _check_timeline(self) -> TimelineV3:
        problem = timeline_problem(self) or overlay_problem(self) or clip_video_problem(self)
        if problem is not None:
            raise ValueError(problem)
        return self


def timeline_v3_json_schema() -> dict[str, Any]:
    """JSON Schema of the v3 timeline shared with the JavaScript render engine."""
    return TimelineV3.model_json_schema()


def clip_window(
    start_ms: int,
    end_ms: int,
    *,
    duration_ms: int,
    transition_ms: int,
) -> tuple[int, int]:
    """The interval a clip is visible in: itself and half of each crossfade around it."""
    half = (transition_ms + 1) // 2
    return max(0, start_ms - half), min(duration_ms, end_ms + half)


def clip_video_problem(timeline: TimelineV3) -> str | None:
    """Return why the videos of a v3 timeline cannot be played, or None."""
    for clip in timeline.clips:
        fields = (clip.video, clip.video_sha256, clip.video_ms)
        if all(value is None for value in fields):
            continue
        if any(value is None for value in fields):
            return f"clip {clip.index} must set video, video_sha256 and video_ms together"
        if clip.effect != "none":
            return f"clip {clip.index} plays a video and must have the effect none"
        start, end = clip_window(
            clip.start_ms,
            clip.end_ms,
            duration_ms=timeline.duration_ms,
            transition_ms=timeline.transition_ms,
        )
        if clip.video_ms is not None and clip.video_ms * MAX_VIDEO_STRETCH < end - start:
            return f"clip {clip.index} has a video too short for its window"
    videos = [clip.video for clip in timeline.clips if clip.video is not None]
    if len(set(videos)) != len(videos):
        return "two clips play the same video"
    return None


def timeline_problem(timeline: Timeline | TimelineV2 | TimelineV3) -> str | None:
    """Return why a timeline cannot be rendered, or None when it is consistent."""
    if (timeline.width, timeline.height) != FORMAT_DIMENSIONS[timeline.format]:
        return "dimensions do not match the format"
    clips = timeline.clips
    if [clip.index for clip in clips] != list(range(1, len(clips) + 1)):
        return "clips are not numbered 1, 2, 3 and so on in order"
    if clips[0].start_ms != 0:
        return "the first clip does not start at zero"
    if clips[0].transition_in != "cut":
        return "the first clip must start with a cut"
    if clips[-1].end_ms != timeline.duration_ms:
        return "the last clip does not end with the audio"
    for previous, clip in zip([None, *clips[:-1]], clips, strict=True):
        if clip.end_ms - clip.start_ms < MIN_CLIP_MS:
            return f"clip {clip.index} is shorter than {MIN_CLIP_MS} ms"
        if previous is None:
            continue
        if clip.start_ms != previous.end_ms:
            return f"clip {clip.index} does not start where clip {previous.index} ends"
        if clip.transition_in == "crossfade":
            if timeline.transition_ms == 0:
                return "a crossfade needs a transition length"
            shortest = min(
                previous.end_ms - previous.start_ms,
                clip.end_ms - clip.start_ms,
            )
            if timeline.transition_ms * 2 > shortest:
                return f"the crossfade into clip {clip.index} is longer than half a clip"
    previous_end = 0
    for position, word in enumerate(timeline.words, start=1):
        if word.end_ms < word.start_ms:
            return f"word {position} ends before it starts"
        if word.start_ms < previous_end:
            return f"word {position} starts before the previous word ends"
        if word.end_ms > timeline.duration_ms:
            return f"word {position} ends after the audio"
        previous_end = word.end_ms
    return None


def timeline_json_schema() -> dict[str, Any]:
    """JSON Schema shared with the JavaScript render engine."""
    return Timeline.model_json_schema()


def timeline_to_json(timeline: Timeline | TimelineV2 | TimelineV3) -> str:
    """Canonical JSON text: same timeline, same bytes, so it can be hashed."""
    return json.dumps(
        timeline.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def milliseconds(seconds: Any) -> int:
    """Round a time in seconds (``Decimal``, int or str) to whole milliseconds."""
    value = Decimal(str(seconds)) * 1_000
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
