"""Contract of the animation plan answered by the text model."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from src.pipeline.animation_plan import (
    AnimationPlanError,
    animation_plan_response_schema,
    normalize_for_match,
    parse_animation_plan,
)

_NARRATION = (
    "Você pega o celular sem pensar enquanto guarda a roupa?\n\n"
    "Repara nessa sequência: primeiro, a espiada automática. A roupa fica esperando.\n\n"
    "Segundo, a distância. Conferir exige interromper a tarefa e sair dali.\n\n"
    "Terceiro, a chance de concluir. Você pode guardar aquela camiseta que falta.\n\n"
    "Deixar o celular longe não garante terminar a tarefa, mas cria uma pausa para decidir."
)

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


def _answer(plan: dict[str, Any]) -> str:
    return json.dumps(plan, ensure_ascii=False)


def _with(path: tuple[Any, ...], value: Any) -> dict[str, Any]:
    plan = copy.deepcopy(_PLAN)
    target: Any = plan
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return plan


def test_a_plan_copied_from_the_narration_is_accepted() -> None:
    plan = parse_animation_plan(_answer(_PLAN), narration=_NARRATION)

    assert plan.steps is not None and len(plan.steps.items) == 3


def test_an_empty_plan_is_accepted() -> None:
    empty = {"schema_version": 1, "steps": None, "highlights": [], "closing_sentence": None}

    plan = parse_animation_plan(_answer(empty), narration=_NARRATION)

    assert plan.steps is None and plan.closing_sentence is None


def test_matching_ignores_case_and_punctuation_but_not_accents() -> None:
    assert normalize_for_match("Primeiro, a ESPIADA — automática!") == "primeiro a espiada automática"
    assert normalize_for_match("automatica") != normalize_for_match("automática")


@pytest.mark.parametrize(
    ("plan", "fragment"),
    [
        (_with(("steps", "title"), "Os 3 passos"), "title"),
        (_with(("steps", "title"), "Uma lista com muitas palavras"), "title"),
        (_with(("steps", "items", 0, "label"), "O olhar automático"), "does not contain its label"),
        (
            _with(("steps", "items", 0), {"label": "A pressa", "anchor": "primeiro, a pressa"}),
            "not in the narration",
        ),
        (
            _with(("steps", "items"), [_PLAN["steps"]["items"][i] for i in (1, 0, 2)]),
            "anchor for item 2 that is not in the narration",
        ),
        (_with(("steps", "items", 0, "anchor"), "automática"), "two to twelve words"),
        (_with(("highlights",), [{"text": "estudos provam que funciona"}]), "highlight 1"),
        (_with(("highlights",), [{"text": "tarefa"}]), "two to eight words"),
        (
            _with(("highlights",), [{"text": "a roupa fica esperando"}, {"text": "A roupa fica esperando."}]),
            "repeats highlight 2",
        ),
        (_with(("closing_sentence",), "Você pega o celular sem pensar enquanto guarda a roupa?"), "last two"),
        (_with(("steps", "items"), _PLAN["steps"]["items"][:1]), "steps.items"),
        ({**_PLAN, "music": "upbeat"}, "music"),
    ],
)
def test_a_plan_with_invented_or_misplaced_text_says_why(plan: dict[str, Any], fragment: str) -> None:
    with pytest.raises(AnimationPlanError, match=fragment):
        parse_animation_plan(_answer(plan), narration=_NARRATION)


def test_a_non_json_answer_is_refused() -> None:
    with pytest.raises(AnimationPlanError, match="valid JSON"):
        parse_animation_plan("Aqui está o plano: lista com três itens", narration=_NARRATION)


def test_response_schema_requires_every_field() -> None:
    schema = animation_plan_response_schema()

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"schema_version", "steps", "highlights", "closing_sentence"}
