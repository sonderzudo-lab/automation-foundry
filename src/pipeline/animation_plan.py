"""Contract for the animation plan of one video, answered by the text model.

The text model reads the approved narration and says what deserves animation:
the steps of a list, up to three short phrases to highlight and the closing
sentence. It never writes new on-screen text: every label, anchor, highlight and
closing sentence must be copied from the narration, which is checked here, so the
video only shows words the owner approved. Only the list title is free text, and
it is limited to four words without digits.

The model writes no timings either. ``anchor`` is the stretch of narration that
introduces a list item (for example "primeiro, a espiada automática"); the code
finds it among the A4 words to time the card, and builds a ``TimelineV2``.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.pipeline.timeline import MAX_LIST_ITEMS

ANIMATION_PLAN_SCHEMA_VERSION = 1
MAX_HIGHLIGHTS = 3
_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")


class AnimationPlanError(ValueError):
    """Raised when an answer is not an acceptable animation plan; the message says why."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PlannedListItem(_Strict):
    """One step of the list: its short label and the narration that introduces it."""

    label: str = Field(min_length=1, max_length=60)
    anchor: str = Field(min_length=1, max_length=200)


class PlannedList(_Strict):
    title: str = Field(min_length=1, max_length=40)
    items: list[PlannedListItem] = Field(min_length=2, max_length=MAX_LIST_ITEMS)


class PlannedHighlight(_Strict):
    text: str = Field(min_length=1, max_length=80)


class AnimationPlan(_Strict):
    """What to animate in one video; empty fields mean no such animation."""

    schema_version: Literal[1]
    steps: PlannedList | None
    highlights: list[PlannedHighlight] = Field(max_length=MAX_HIGHLIGHTS)
    closing_sentence: str | None


def animation_plan_response_schema() -> dict[str, Any]:
    """JSON Schema of the answer, passed to providers that support structured output."""
    return AnimationPlan.model_json_schema()


def normalize_for_match(text: str) -> str:
    """Lower case, accents kept, punctuation and spacing ignored."""
    return " ".join(_NON_WORD.sub(" ", unicodedata.normalize("NFC", text).lower()).split())


def parse_animation_plan(answer: str, *, narration: str) -> AnimationPlan:
    """Read a model answer as a plan whose texts all come from ``narration``.

    Raises ``AnimationPlanError`` with a short English reason that can be sent
    back to the model when asking again.
    """
    try:
        data = json.loads(_strip_code_fence(answer))
    except json.JSONDecodeError as exc:
        raise AnimationPlanError("is not a single valid JSON object") from exc
    try:
        plan = AnimationPlan.model_validate(data)
    except ValidationError as exc:
        raise AnimationPlanError(_first_reason(exc)) from exc
    problem = animation_plan_problem(plan, narration=narration)
    if problem is not None:
        raise AnimationPlanError(problem)
    return plan


def animation_plan_problem(plan: AnimationPlan, *, narration: str) -> str | None:
    """Return why a plan does not match the narration, or None when it does."""
    spoken = normalize_for_match(narration)
    if plan.steps is not None:
        title_words = plan.steps.title.split()
        if not 1 <= len(title_words) <= 4 or any(char.isdigit() for char in plan.steps.title):
            return "has a list title that is not one to four words without digits"
        position = -1
        labels: set[str] = set()
        for number, item in enumerate(plan.steps.items, start=1):
            label = normalize_for_match(item.label)
            anchor = normalize_for_match(item.anchor)
            if not 1 <= len(label.split()) <= 6:
                return f"has a label for item {number} that is not one to six words"
            if not 2 <= len(anchor.split()) <= 12:
                return f"has an anchor for item {number} that is not two to twelve words"
            if label not in anchor:
                return f"has an anchor for item {number} that does not contain its label"
            found = _find_words(spoken, anchor, after=position)
            if found < 0:
                return f"has an anchor for item {number} that is not in the narration"
            if label in labels:
                return f"repeats the label of item {number}"
            labels.add(label)
            position = found
    seen: set[str] = set()
    for number, highlight in enumerate(plan.highlights, start=1):
        text = normalize_for_match(highlight.text)
        if not 2 <= len(text.split()) <= 8:
            return f"has highlight {number} that is not two to eight words"
        if _find_words(spoken, text) < 0:
            return f"has highlight {number} that is not in the narration"
        if text in seen:
            return f"repeats highlight {number}"
        seen.add(text)
    if plan.closing_sentence is not None:
        sentences = [
            normalize_for_match(sentence)
            for sentence in _SENTENCE_END.split(narration.strip())
            if normalize_for_match(sentence)
        ]
        if normalize_for_match(plan.closing_sentence) not in sentences[-2:]:
            return "has a closing sentence that is not one of the last two sentences"
    return None


def _find_words(spoken: str, excerpt: str, *, after: int = -1) -> int:
    """Position of ``excerpt`` as whole words in ``spoken`` after ``after``, or -1."""
    start = after + 1
    while True:
        found = spoken.find(excerpt, start)
        if found < 0:
            return -1
        end = found + len(excerpt)
        if (found == 0 or spoken[found - 1] == " ") and (end == len(spoken) or spoken[end] == " "):
            return found
        start = found + 1


def _strip_code_fence(answer: str) -> str:
    text = answer.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    return text.strip()


def _first_reason(exc: ValidationError) -> str:
    error = exc.errors(include_url=False)[0]
    message = str(error.get("msg", "is invalid")).removeprefix("Value error, ")
    location = ".".join(str(part) for part in error.get("loc", ()))
    return f"is invalid at '{location}': {message}" if location else message
