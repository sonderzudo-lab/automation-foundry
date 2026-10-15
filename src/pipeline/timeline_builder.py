"""Build the v2 timeline of one video from verified evidence and the animation plan.

Every time comes from the spoken words of the verified A4 captions; the text
model only said which words to animate (see ``animation_plan``):

- each image starts when the first word of its stretch of narration is spoken;
- each list card is placed where its anchor is spoken. The first card opens
  with the sentence that announces the list; an item is spoken from its anchor
  to the end of that sentence, and the card closes a moment later;
- the closing sentence starts with its first word and ends with the audio;
- a highlight is shown while its words are spoken, only where no other overlay
  is on screen. One that does not fit is left out and reported.

On-screen texts are the caption words themselves, so they keep the spelling of
the approved narration whatever capitals or punctuation the model wrote.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from src.pipeline.animation_plan import AnimationPlan, PlannedList, normalize_for_match
from src.pipeline.timeline import (
    FORMAT_DIMENSIONS,
    MIN_CLIP_MS,
    CaptionTemplate,
    ClipEffect,
    ClosingOverlay,
    HighlightOverlay,
    ListOverlay,
    Overlay,
    TimelineClip,
    TimelineV2,
    TimelineWord,
    VideoFormat,
)

TRANSITION_MS = 400
# A list card stays a moment after its item is spoken, as in the approved demo.
LIST_CARD_TAIL_MS = 600
_SENTENCE_END = re.compile(r"[.!?…][’”\"')\]]*$")
_EDGE_PUNCTUATION = re.compile(r"^[^\w]+|[^\w]+$")
_MAX_HIGHLIGHT_CHARS = 80


class TimelineBuildError(ValueError):
    """The evidence and the plan do not make a drawable timeline; the message says why."""


@dataclass(frozen=True, slots=True)
class TimelineImage:
    """One verified A3 image and the stretch of narration it covers."""

    file_name: str
    sha256: str
    narration_text: str


@dataclass(frozen=True, slots=True)
class BuiltTimeline:
    timeline: TimelineV2
    # Highlights of the plan that had no free time on screen.
    dropped_highlights: tuple[str, ...]


def timeline_images_from_manifest(manifest: Mapping[str, Any]) -> tuple[TimelineImage, ...]:
    """The images of an A3 manifest in order, each with its stretch of narration."""
    assets = manifest.get("assets")
    if not isinstance(assets, list) or not assets:
        raise TimelineBuildError("the visual manifest has no images")
    images: list[TimelineImage] = []
    for expected, asset in enumerate(
        sorted(assets, key=lambda item: item.get("index", 0) if isinstance(item, dict) else 0),
        start=1,
    ):
        if not isinstance(asset, dict) or asset.get("index") != expected:
            raise TimelineBuildError("the visual manifest images are not numbered in order")
        file_name, sha256, stretch = (
            asset.get("file_name"),
            asset.get("sha256"),
            asset.get("narration_text"),
        )
        if not isinstance(file_name, str) or not isinstance(sha256, str):
            raise TimelineBuildError(f"image {expected} has no file name or checksum")
        if not isinstance(stretch, str) or not stretch.split():
            raise TimelineBuildError(f"image {expected} has no stretch of narration")
        images.append(TimelineImage(file_name=file_name, sha256=sha256, narration_text=stretch))
    return tuple(images)


def build_timeline_v2(
    *,
    video_format: VideoFormat,
    duration_ms: int,
    audio_sha256: str,
    caption_ass_sha256: str,
    images: Sequence[TimelineImage],
    words: Sequence[TimelineWord],
    plan: AnimationPlan,
    caption_template: CaptionTemplate = "word_pop",
) -> BuiltTimeline:
    """Return the checked ``TimelineV2``; raise ``TimelineBuildError`` if it cannot be drawn."""
    if not words:
        raise TimelineBuildError("the captions have no words")
    if not images:
        raise TimelineBuildError("there are no images")
    finder = _WordFinder(words)
    overlays: list[Overlay] = []
    closing = _closing_overlay(plan, finder, duration_ms=duration_ms)
    limit = closing.start_ms if closing is not None else duration_ms
    if plan.steps is not None:
        overlays.extend(_list_cards(plan.steps, finder, limit_ms=limit))
    if closing is not None:
        overlays.append(closing)
    dropped: list[str] = []
    for highlight in plan.highlights:
        placed = _highlight_overlay(highlight.text, finder, overlays, duration_ms=duration_ms)
        if placed is None:
            dropped.append(highlight.text)
        else:
            overlays.append(placed)
            overlays.sort(key=lambda overlay: overlay.start_ms)
    width, height = FORMAT_DIMENSIONS[video_format]
    try:
        timeline = TimelineV2(
            schema_version=2,
            format=video_format,
            width=width,
            height=height,
            fps=30,
            duration_ms=duration_ms,
            audio_sha256=audio_sha256,
            caption_ass_sha256=caption_ass_sha256,
            caption_template=caption_template,
            transition_ms=TRANSITION_MS,
            clips=_clips(images, words, duration_ms=duration_ms),
            words=list(words),
            animation_style="explainer_v1",
            overlays=overlays,
        )
    except ValidationError as exc:
        error = exc.errors(include_url=False)[0]
        raise TimelineBuildError(str(error.get("msg", "is invalid"))) from exc
    return BuiltTimeline(timeline=timeline, dropped_highlights=tuple(dropped))


@dataclass(frozen=True, slots=True)
class SceneSpan:
    """When one image is on screen, between two cuts."""

    index: int
    start_ms: int
    end_ms: int


def scene_spans(
    images: Sequence[TimelineImage],
    words: Sequence[TimelineWord],
    *,
    duration_ms: int,
) -> tuple[SceneSpan, ...]:
    """Start each image when the first word of its stretch is spoken.

    The stretches split the narration word by word. When the captions hold a
    different number of words, positions are scaled, as in the approved demo.
    Every image keeps at least ``MIN_CLIP_MS`` on screen. The animated scenes
    (A5a) are generated for these spans and A5 cuts the video at the same ones,
    which ``scene_cuts_sha256`` lets both sides compare.
    """
    if not words:
        raise TimelineBuildError("the captions have no words")
    if not images:
        raise TimelineBuildError("there are no images")
    counts = [len(image.narration_text.split()) for image in images]
    scale = len(words) / sum(counts)
    starts: list[int] = []
    spoken = 0
    for position, count in enumerate(counts):
        if position == 0:
            starts.append(0)
        else:
            index = min(round(spoken * scale), len(words) - 1)
            starts.append(max(words[index].start_ms, starts[-1] + MIN_CLIP_MS))
        spoken += count
    if duration_ms - starts[-1] < MIN_CLIP_MS:
        raise TimelineBuildError("the images do not fit in the audio for one second each")
    ends = [*starts[1:], duration_ms]
    return tuple(
        SceneSpan(index=position, start_ms=start, end_ms=end)
        for position, (start, end) in enumerate(zip(starts, ends, strict=True), start=1)
    )


def scene_cuts_sha256(spans: Sequence[SceneSpan]) -> str:
    """Digest of the cuts, so the animated scenes and the montage agree on them."""
    canonical = json.dumps(
        [[span.index, span.start_ms, span.end_ms] for span in spans],
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _clips(
    images: Sequence[TimelineImage],
    words: Sequence[TimelineWord],
    *,
    duration_ms: int,
) -> list[TimelineClip]:
    spans = scene_spans(images, words, duration_ms=duration_ms)
    return [
        TimelineClip(
            index=span.index,
            image=image.file_name,
            image_sha256=image.sha256,
            start_ms=span.start_ms,
            end_ms=span.end_ms,
            effect=_effect(span.index),
            transition_in="cut" if span.index == 1 else "crossfade",
        )
        for image, span in zip(images, spans, strict=True)
    ]


def _effect(position: int) -> ClipEffect:
    return "zoom_in" if position % 2 else "zoom_out"


def _list_cards(steps: PlannedList, finder: _WordFinder, *, limit_ms: int) -> list[ListOverlay]:
    spans: list[tuple[int, int]] = []
    labels: list[str] = []
    after = -1
    for number, item in enumerate(steps.items, start=1):
        anchor = finder.find(item.anchor, after_token=after)
        if anchor is None:
            raise TimelineBuildError(f"the anchor of item {number} is not among the spoken words")
        label = finder.find(item.label, after_token=anchor.first_token - 1)
        if label is None or label.last_token > anchor.last_token:
            raise TimelineBuildError(f"the label of item {number} is not inside its anchor")
        labels.append(_screen_text(finder.words[label.first_word : label.last_word + 1]))
        spans.append((anchor.first_word, finder.sentence_end(anchor.last_word)))
        after = anchor.last_token
    words = finder.words
    cards: list[ListOverlay] = []
    for active, (first, last) in enumerate(spans):
        item_start = words[first].start_ms
        item_end = words[last].end_ms
        start = words[finder.sentence_start(first)].start_ms if active == 0 else item_start
        following = words[spans[active + 1][0]].start_ms if active + 1 < len(spans) else limit_ms
        end = min(item_end + LIST_CARD_TAIL_MS, following, limit_ms)
        if end - start < MIN_CLIP_MS or item_end > end:
            raise TimelineBuildError(
                f"the card of item {active + 1} has no room before the next animation"
            )
        cards.append(
            ListOverlay(
                kind="list",
                start_ms=start,
                end_ms=end,
                title=steps.title.strip(),
                items=labels,
                active=active,
                item_start_ms=item_start,
                item_end_ms=item_end,
            )
        )
    return cards


def _closing_overlay(
    plan: AnimationPlan,
    finder: _WordFinder,
    *,
    duration_ms: int,
) -> ClosingOverlay | None:
    if plan.closing_sentence is None:
        return None
    found = finder.find_last(plan.closing_sentence)
    if found is None:
        raise TimelineBuildError("the closing sentence is not among the spoken words")
    words = finder.words
    start = words[found.first_word].start_ms
    if duration_ms - start < MIN_CLIP_MS:
        raise TimelineBuildError("the closing sentence is spoken in less than one second")
    # As in the approved demo, the words after the last comma carry the takeaway.
    highlight_from = found.first_word
    for index in range(found.first_word, found.last_word):
        if words[index].text.endswith(","):
            highlight_from = index + 1
    return ClosingOverlay(
        kind="closing_sentence",
        start_ms=start,
        end_ms=duration_ms,
        word_from=found.first_word,
        highlight_from=highlight_from,
    )


def _highlight_overlay(
    text: str,
    finder: _WordFinder,
    overlays: Sequence[Overlay],
    *,
    duration_ms: int,
) -> HighlightOverlay | None:
    """The first time the phrase is spoken with the screen free, or None."""
    words = finder.words
    for found in finder.find_all(text):
        start = words[found.first_word].start_ms
        end = min(max(words[found.last_word].end_ms, start + MIN_CLIP_MS), duration_ms)
        screen = _screen_text(words[found.first_word : found.last_word + 1])
        if end - start < MIN_CLIP_MS or len(screen) > _MAX_HIGHLIGHT_CHARS:
            continue
        if all(end <= other.start_ms or start >= other.end_ms for other in overlays):
            return HighlightOverlay(kind="highlight", start_ms=start, end_ms=end, text=screen)
    return None


def _screen_text(words: Sequence[TimelineWord]) -> str:
    """The spoken words as shown on screen: punctuation trimmed at both ends, first capital."""
    text = _EDGE_PUNCTUATION.sub("", " ".join(word.text for word in words))
    return text[:1].upper() + text[1:]


@dataclass(frozen=True, slots=True)
class _Found:
    first_token: int
    last_token: int
    first_word: int
    last_word: int


class _WordFinder:
    """Finds a stretch of narration among the caption words, as whole words."""

    def __init__(self, words: Sequence[TimelineWord]) -> None:
        self.words = words
        self._tokens: list[str] = []
        self._owners: list[int] = []
        for index, word in enumerate(words):
            for token in normalize_for_match(word.text).split():
                self._tokens.append(token)
                self._owners.append(index)

    def find_all(self, excerpt: str, *, after_token: int = -1) -> list[_Found]:
        wanted = normalize_for_match(excerpt).split()
        if not wanted:
            return []
        found: list[_Found] = []
        for start in range(after_token + 1, len(self._tokens) - len(wanted) + 1):
            if self._tokens[start : start + len(wanted)] == wanted:
                last = start + len(wanted) - 1
                found.append(_Found(start, last, self._owners[start], self._owners[last]))
        return found

    def find(self, excerpt: str, *, after_token: int = -1) -> _Found | None:
        found = self.find_all(excerpt, after_token=after_token)
        return found[0] if found else None

    def find_last(self, excerpt: str) -> _Found | None:
        found = self.find_all(excerpt)
        return found[-1] if found else None

    def sentence_end(self, index: int) -> int:
        """Index of the word that ends the sentence holding word ``index``."""
        for position in range(index, len(self.words)):
            if _SENTENCE_END.search(self.words[position].text):
                return position
        return len(self.words) - 1

    def sentence_start(self, index: int) -> int:
        """Index of the first word of the sentence holding word ``index``."""
        while index > 0 and not _SENTENCE_END.search(self.words[index - 1].text):
            index -= 1
        return index
