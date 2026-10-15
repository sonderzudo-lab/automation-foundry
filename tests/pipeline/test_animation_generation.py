"""Asking the text model for the animation plan, with a scripted fake model."""

from __future__ import annotations

import copy
import json
from decimal import Decimal
from typing import Any

import pytest

from src.pipeline import animation_generation
from src.pipeline.animation_generation import (
    ANIMATION_PLAN_ATTEMPTS,
    animation_plan_request,
    write_animation_plan,
)
from src.pipeline.animation_plan import AnimationPlanError, animation_plan_response_schema
from src.pipeline.text_model import (
    TextModelPermanentError,
    TextModelProviderName,
    TextModelRequest,
    TextModelResult,
)

_NARRATION = (
    "Você pega o celular sem pensar enquanto guarda a roupa?\n\n"
    "Repara nessa sequência: primeiro, a espiada automática. A roupa fica esperando.\n\n"
    "Segundo, a distância. Conferir exige interromper a tarefa e sair dali.\n\n"
    "Deixar o celular longe não garante terminar a tarefa, mas cria uma pausa para decidir."
)

_PLAN: dict[str, Any] = {
    "schema_version": 1,
    "steps": {
        "title": "A sequência",
        "items": [
            {"label": "A espiada automática", "anchor": "primeiro, a espiada automática"},
            {"label": "A distância", "anchor": "Segundo, a distância"},
        ],
    },
    "highlights": [{"text": "interromper a tarefa"}],
    "closing_sentence": (
        "Deixar o celular longe não garante terminar a tarefa, mas cria uma pausa para decidir."
    ),
}


def _answer(plan: dict[str, Any]) -> str:
    return json.dumps(plan, ensure_ascii=False)


def _invented_label() -> str:
    plan = copy.deepcopy(_PLAN)
    plan["steps"]["items"][0]["label"] = "A olhada rápida"
    return _answer(plan)


class _TextModel:
    """Fake text model: answers or raises the scripted outcomes, repeating the last."""

    provider: TextModelProviderName = "codex_exec"
    model_id = "codex-default"

    def __init__(self, *outcomes: str | Exception) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[TextModelRequest] = []

    def complete(self, request: TextModelRequest) -> TextModelResult:
        self.requests.append(request)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return TextModelResult(
            text=outcome,
            provider=self.provider,
            model_id=self.model_id,
            duration_seconds=Decimal("1.5"),
        )


def test_an_accepted_answer_is_returned_with_who_wrote_it() -> None:
    model = _TextModel(_answer(_PLAN))

    written = write_animation_plan(model, narration=_NARRATION)

    assert written.attempts == 1
    assert written.plan.steps is not None
    assert [item.label for item in written.plan.steps.items] == [
        "A espiada automática",
        "A distância",
    ]
    assert (written.result.provider, written.result.model_id) == ("codex_exec", "codex-default")
    (request,) = model.requests
    assert request.purpose == "animation_plan"
    assert request.response_schema == animation_plan_response_schema()
    assert _NARRATION.strip() in request.user
    assert "refused" not in request.user


def test_a_refused_answer_is_asked_again_with_the_reason() -> None:
    model = _TextModel(_invented_label(), _answer(_PLAN))

    written = write_animation_plan(model, narration=_NARRATION)

    assert written.attempts == 2
    retry = model.requests[1].user
    assert "refused because it has an anchor for item 1 that does not contain its label" in retry
    assert "A olhada rápida" in retry


def test_no_acceptable_answer_fails_closed_after_the_attempt_ceiling() -> None:
    model = _TextModel("not json", _invented_label())

    with pytest.raises(AnimationPlanError, match="no acceptable plan"):
        write_animation_plan(model, narration=_NARRATION)

    assert len(model.requests) == ANIMATION_PLAN_ATTEMPTS
    assert "is not a single valid JSON object" in model.requests[1].user


def test_a_text_model_failure_is_not_turned_into_a_retry() -> None:
    model = _TextModel(TextModelPermanentError("codex is not signed in"))

    with pytest.raises(TextModelPermanentError):
        write_animation_plan(model, narration=_NARRATION)

    assert len(model.requests) == 1


def test_an_empty_or_oversized_narration_is_refused_before_any_call() -> None:
    model = _TextModel(_answer(_PLAN))

    with pytest.raises(AnimationPlanError, match="empty"):
        write_animation_plan(model, narration="   ")
    with pytest.raises(AnimationPlanError, match="too long"):
        write_animation_plan(model, narration="palavra " * 10_000)

    assert model.requests == []


def test_the_refused_answer_sent_back_is_bounded() -> None:
    request = animation_plan_request(
        narration=_NARRATION,
        refused=("x" * 50_000, "is not a single valid JSON object"),
    )

    assert request.user.count("x") < 5_000


def test_the_configured_text_model_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[float | None] = []
    sentinel = _TextModel(_answer(_PLAN))

    def fake_configured(*, ollama_timeout_seconds: float | None = None) -> _TextModel:
        calls.append(ollama_timeout_seconds)
        return sentinel

    monkeypatch.setattr(animation_generation, "configured_text_model", fake_configured)

    assert animation_generation.animation_text_model() is sentinel
    assert calls == [animation_generation.ANIMATION_MODEL_TIMEOUT_SECONDS]
