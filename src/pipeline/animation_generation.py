"""Ask the configured text model for the animation plan of one video.

The model reads the approved narration and answers one ``AnimationPlan`` (see
``animation_plan``). Every answer is checked against the narration; a refused
answer is sent back with the reason, as for the scene plan, up to a fixed number
of attempts. A video that still has no acceptable plan fails closed: nothing
unchecked reaches the screen.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import structlog

from src.pipeline.animation_plan import (
    AnimationPlan,
    AnimationPlanError,
    animation_plan_response_schema,
    parse_animation_plan,
)
from src.pipeline.text_model import TextModel, TextModelRequest, TextModelResult
from src.pipeline.text_model_factory import configured_text_model

log = structlog.get_logger(__name__)

ANIMATION_PLAN_ATTEMPTS = 4
ANIMATION_MODEL_TIMEOUT_SECONDS = 120.0
_MAX_REFUSED_PLAN_CHARS = 4_000

_SYSTEM = (
    # Qwen3 token that disables thinking mode; other models ignore the line.
    "/no_think\n"
    "You choose what to animate on screen in a short narrated video. You receive the "
    "approved narration in Brazilian Portuguese. Answer one JSON object with this shape:\n"
    '{"schema_version": 1, '
    '"steps": {"title": "...", "items": [{"label": "...", "anchor": "..."}]}, '
    '"highlights": [{"text": "..."}], '
    '"closing_sentence": "..."}\n'
    "Rules:\n"
    "- Never write new words for the screen. Every label, anchor, highlight and the "
    "closing sentence is copied from the narration exactly as written, with its "
    "accents. Only the list title is yours.\n"
    "- steps: only when the narration announces a sequence of 2 to 5 items (for "
    'example "primeiro, ... segundo, ..."); otherwise null. title: 1 to 4 words in '
    "Brazilian Portuguese, without digits. Each item has an anchor, 2 to 12 "
    "consecutive words of the narration that introduce it, and a label, 1 to 6 "
    "consecutive words found inside its anchor. Anchors follow the order of the "
    "narration and labels do not repeat.\n"
    "- highlights: up to 3 phrases of 2 to 8 consecutive words of the narration that "
    "carry the main idea, without repeating one. Use an empty list when nothing "
    "deserves it.\n"
    "- closing_sentence: the last or the second to last sentence of the narration, "
    "copied whole, or null.\n"
    "Answer with the JSON object only."
)


@dataclass(frozen=True, slots=True)
class WrittenAnimationPlan:
    """An accepted plan, the answer that carried it and how many attempts it took."""

    plan: AnimationPlan
    result: TextModelResult
    attempts: int


def animation_text_model() -> TextModel:
    """The configured text model (Ollama, or the ChatGPT plan with Ollama in reserve)."""
    return configured_text_model(ollama_timeout_seconds=ANIMATION_MODEL_TIMEOUT_SECONDS)


def animation_plan_request(
    *,
    narration: str,
    refused: tuple[str, str] | None = None,
) -> TextModelRequest:
    """The request for one plan; a refused answer is sent back with the reason."""
    user = f"Narration:\n{narration.strip()}"
    if refused is not None:
        user += (
            f"\n\nYour previous plan was refused because it {refused[1]}:\n"
            f"{refused[0][:_MAX_REFUSED_PLAN_CHARS]}\n"
            "Answer the whole plan again, fixing that."
        )
    return TextModelRequest(
        purpose="animation_plan",
        system=_SYSTEM,
        user=user,
        response_schema=animation_plan_response_schema(),
    )


def write_animation_plan(
    model: TextModel,
    *,
    narration: str,
    check: Callable[[AnimationPlan], str | None] | None = None,
) -> WrittenAnimationPlan:
    """Return the first plan the checks accept, asking again with each refusal.

    ``check`` may refuse a plan that matches the narration for a reason of its
    own, such as animations that do not fit the spoken timings; it returns that
    reason, phrased to follow "it", or None. Raises ``AnimationPlanError`` when the narration is empty or too long for one
    request, or when no answer is accepted within ``ANIMATION_PLAN_ATTEMPTS``.
    Text model failures (unreachable, not signed in) propagate unchanged.
    """
    if not narration.strip():
        raise AnimationPlanError("the narration is empty")
    refused: tuple[str, str] | None = None
    for attempt in range(1, ANIMATION_PLAN_ATTEMPTS + 1):
        try:
            request = animation_plan_request(narration=narration, refused=refused)
        except ValueError as exc:
            raise AnimationPlanError("the narration is too long for one request") from exc
        result = model.complete(request)
        try:
            plan = parse_animation_plan(result.text, narration=narration)
        except AnimationPlanError as exc:
            problem: str | None = str(exc)
        else:
            problem = check(plan) if check is not None else None
            if problem is None:
                return WrittenAnimationPlan(plan=plan, result=result, attempts=attempt)
        # The reason is written by the checks, never copied from the answer.
        log.warning("animation_plan_refused", attempt=attempt, reason=problem)
        refused = (result.text, str(problem))
    raise AnimationPlanError(f"no acceptable plan in {ANIMATION_PLAN_ATTEMPTS} attempts")
