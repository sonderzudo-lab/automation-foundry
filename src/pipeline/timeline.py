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
"""

from __future__ import annotations

import json
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Literal

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

FORMAT_DIMENSIONS: dict[VideoFormat, tuple[int, int]] = {
    "short": (1080, 1920),
    "long": (1920, 1080),
}

_SHA256 = r"^[0-9a-f]{64}$"
_IMAGE_NAME = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.png$"


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


def timeline_problem(timeline: Timeline) -> str | None:
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


def timeline_to_json(timeline: Timeline) -> str:
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
